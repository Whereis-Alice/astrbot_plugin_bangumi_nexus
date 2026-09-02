"""季度感知的一整条链：标题匹配 → 条目挑选 → 卡片文案 → 进度回填。

为什么值得单独一套：这些问题全都**不报错**，只会安静地把数据写歪 ——

* 「第三季」的更新被记进第一季的追番条目，进度条从此和现实脱节；
* 字幕组的连续编号（年番第三季的第 29 集）被封顶成 12/12，一部在播的番凭空完结；
* 卡片标题永远显示第一季，用户以为插件认错了番；
* 上游 「${message}」 整段入卡，把本机下载路径播进群里。

四件事互相牵连，所以放在同一份文件里按链路顺序锁住。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from nexus import titles
from nexus.config import NexusConfig
from nexus.models import Subject, WatchItem
from nexus.services import webhook
from nexus.services.search import RESOLVE_CANDIDATES, SearchService, pick_by_season
from nexus.services.watchlist import (
    NUMBERING_SLACK,
    WatchlistService,
    _auto_item,
    backfill_progress,
)
from nexus.store import Store

UMO = "aiocqhttp:GroupMessage:1078946249"

#: 真实数据：Bangumi 上「超超超超超喜欢你的100个女朋友」三季的条目 ID 与集数。
#: 「sort=match」 无视季度后缀，搜「第三季」返回的顺序也是 S1 → S2 → S3，
#: 所以这三条的排列顺序本身就是要复现的 bug 现场。
HUNDRED = (
    (424379, "超超超超超喜欢你的100个女朋友", "君のことが大大大大大好きな100人の彼女", 12),
    (
        471793,
        "超超超超超喜欢你的100个女朋友 第二季",
        "君のことが大大大大大好きな100人の彼女 第2期",
        12,
    ),
    (
        598058,
        "超超超超超喜欢你的100个女朋友 第三季",
        "君のことが大大大大大好きな100人の彼女 第3期",
        12,
    ),
)

#: 上游推来日文原名时的样子 —— ani-rss 的 TMDB 查询失败就会退回原名。
JP_S2 = HUNDRED[1][2]
JP_S3 = HUNDRED[2][2]


def _subjects() -> list[Subject]:
    return [
        Subject(id=sid, name=name, name_cn=cn, total_episodes=eps, image=f"https://img/{sid}.jpg")
        for sid, cn, name, eps in HUNDRED
    ]


class Test季度识别:
    """「没写季数」和「第一季」必须是两种不同的状态。"""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("超超超超超喜欢你的100个女朋友 第三季", 3),
            ("葬送的芙莉莲 第2期", 2),
            ("Blue Lock Season 2", 2),
            ("某番 S03", 3),
            ("药屋少女的呢喃", 0),
        ],
    )
    def test_读出季数(self, text: str, expected: int) -> None:
        assert titles.season_number(text) == expected

    def test_没写季数不等于第一季(self) -> None:
        """这是整条链的地基：把「没写」当成第 1 季，第三季的通知就再也匹配不上
        表里那条无季标的旧记录 —— 用户会彻底收不到推送。"""

        assert titles.season_number("药屋少女的呢喃") == 0
        assert not titles.season_conflict("药屋少女的呢喃", "药屋少女的呢喃 第二季")

    def test_双方都写明且不同才算冲突(self) -> None:
        assert titles.season_conflict("某番 第二季", "某番 第三季")
        assert not titles.season_conflict("某番 第三季", "某番 第3期")


class Test相似度惩罚季度冲突:
    """不同季的主干完全一致，光靠字符比较必然串台。"""

    def test_不同季压到阈值之下(self) -> None:
        score = titles.similarity(
            "超超超超超喜欢你的100个女朋友 第二季",
            "超超超超超喜欢你的100个女朋友 第三季",
        )
        assert score < titles.MATCH_THRESHOLD

    def test_同季照样高分(self) -> None:
        assert titles.similarity("某番 第三季", "某番 第3期") == 1.0

    def test_一侧没写季数保持宽容(self) -> None:
        """群里那条无季标的旧记录得继续收到通知，否则修 bug 反而修出更大的 bug。"""

        score = titles.similarity(
            "超超超超超喜欢你的100个女朋友",
            "超超超超超喜欢你的100个女朋友 第三季",
        )
        assert score >= titles.MATCH_THRESHOLD


class Test补季度后缀:
    """下载器把季度单独放在一个字段，标题里往往一个字都不提。"""

    def test_合成完整标题(self) -> None:
        assert (
            titles.qualify_season("超超超超超喜欢你的100个女朋友", 3)
            == "超超超超超喜欢你的100个女朋友 第三季"
        )

    def test_第一季不加后缀(self) -> None:
        """首季条目几乎都不写「第一季」，硬加会制造出显式冲突把它们排除掉。"""

        assert titles.qualify_season("药屋少女的呢喃", 1) == "药屋少女的呢喃"
        assert titles.qualify_season("药屋少女的呢喃", 0) == "药屋少女的呢喃"

    def test_标题已经写了就不重复加(self) -> None:
        assert titles.qualify_season("某番 第三季", 3) == "某番 第三季"

    def test_超出中文数字表就用阿拉伯数字(self) -> None:
        assert titles.qualify_season("某番", 11) == "某番 第11季"

    def test_空标题原样返回(self) -> None:
        assert titles.qualify_season("", 3) == ""


class Test按季度挑条目:
    """Bangumi 的相关性排序把第一季排在最前，只取第一条必然拿错。"""

    def test_挑出对得上的那一季(self) -> None:
        picked = pick_by_season("超超超超超喜欢你的100个女朋友 第三季", _subjects())
        assert picked is not None
        assert picked.id == 598058

    def test_查询没写季数时不动官方排序(self) -> None:
        """「/bgm 迷宫饭」这类日常查询本来就该跟随 Bangumi 的相关性排序。"""

        picked = pick_by_season("超超超超超喜欢你的100个女朋友", _subjects())
        assert picked is not None
        assert picked.id == 424379

    def test_日文季号也认(self) -> None:
        picked = pick_by_season(JP_S2, _subjects())
        assert picked is not None
        assert picked.id == 471793

    def test_没有对得上的季就退回主条目(self) -> None:
        """宁可退回无季标的主条目，也不要把第五季的通知记到第二季头上。"""

        picked = pick_by_season("超超超超超喜欢你的100个女朋友 第五季", _subjects())
        assert picked is not None
        assert picked.id == 424379

    def test_空候选返回空(self) -> None:
        assert pick_by_season("某番 第三季", []) is None

    def test_候选上限够装完一整个系列(self) -> None:
        """限 1 条就是这个 bug 的根因；至少要能容下常见系列的全部季度。"""

        assert len(HUNDRED) <= RESOLVE_CANDIDATES


class _Bangumi:
    """按 ID 精确返回、按名字返回全系列 —— 复现「搜索认不出季度」的现场。"""

    def __init__(self) -> None:
        self.searched: list[tuple[str, int]] = []
        self.fetched: list[int] = []

    async def search(self, keyword: str, *, limit: int = 1, **_: Any) -> list[Subject]:
        self.searched.append((keyword, limit))
        return _subjects()[:limit]

    async def subject(self, subject_id: int) -> Subject | None:
        self.fetched.append(subject_id)
        return next((item for item in _subjects() if item.id == subject_id), None)

    async def episodes(self, subject_id: int, *, limit: int = 100) -> list[Any]:
        """这一组用例都不涉及连续编号还原，给个空表挡住那条支路就够。"""

        return []


class _Activity:
    def __init__(self) -> None:
        self.notes: list[str] = []

    def info(self, scope: str, text: str) -> None:
        self.notes.append(f"i:{scope}:{text}")

    def warn(self, scope: str, text: str) -> None:
        self.notes.append(f"w:{scope}:{text}")

    def error(self, scope: str, text: str) -> None:
        self.notes.append(f"e:{scope}:{text}")


def _service(conf: NexusConfig) -> tuple[webhook.WebhookService, _Bangumi]:
    bangumi = _Bangumi()
    deps = cast(
        Any,
        SimpleNamespace(
            conf=conf,
            activity=_Activity(),
            hub=SimpleNamespace(bangumi=bangumi),
        ),
    )
    return webhook.WebhookService(deps, notifier=cast(Any, SimpleNamespace())), bangumi


@pytest.fixture
async def store(tmp_path: Path):
    inst = Store(tmp_path / "nexus.db")
    await inst.initialize()
    yield inst
    await inst.close()


def _backfill_deps(store: Store) -> Any:
    """回填只摸 「store」 和 「activity」，其余依赖给个空壳就够。"""

    return cast(
        Any,
        SimpleNamespace(
            conf=NexusConfig(),
            store=store,
            activity=_Activity(),
            hub=SimpleNamespace(bangumi=_Bangumi()),
        ),
    )


def _watchlist(deps: Any) -> WatchlistService:
    return WatchlistService(cast(Any, deps), SearchService(cast(Any, deps)))


async def _watch(
    store: Store,
    title: str,
    *,
    progress: int = 0,
    total: int = 0,
) -> WatchItem:
    return await store.upsert_watch(
        WatchItem(
            id=0,
            umo=UMO,
            subject_id=0,
            title=title,
            status="watching",
            progress=progress,
            total=total,
        )
    )


class Test卡片带上季度:
    """下载器把季度放在单独字段，卡片标题得自己合成 —— 否则第三季的通知长得和
    第一季一模一样，用户只会以为插件认错了番。"""

    async def test_标题补上第三季(self) -> None:
        service, _ = _service(NexusConfig(enable_cross_match=False))
        note = await service.build(
            {
                "event": "下载完成",
                "title": "超超超超超喜欢你的100个女朋友",
                "season": "3",
                "episode": "29",
                "currentEpisodeNumber": "9",
                "totalEpisodeNumber": "12",
            }
        )
        assert note.title == "超超超超超喜欢你的100个女朋友 第三季"
        assert note.payload["season"] == 3
        assert note.payload["episode"] == 29
        assert note.payload["current_episode"] == 9
        assert note.payload["total_episodes"] == 12

    async def test_第一季不加后缀(self) -> None:
        """首季条目几乎都不写「第一季」，硬加会和表里的旧记录制造出显式季度冲突。"""

        service, _ = _service(NexusConfig(enable_cross_match=False))
        note = await service.build(
            {"event": "下载完成", "title": "药屋少女的呢喃", "season": "1", "episode": "5"}
        )
        assert note.title == "药屋少女的呢喃"

    async def test_结构化字段在就不搬上游原文(self) -> None:
        """这是用户真实撞到的 bug：整段 「${message}」 入卡，把本机下载路径播进了群里。"""

        service, _ = _service(NexusConfig(enable_cross_match=False))
        note = await service.build(
            {
                "event": "下载完成",
                "title": "超超超超超喜欢你的100个女朋友",
                "season": "3",
                "episode": "29",
                "currentEpisodeNumber": "9",
                "totalEpisodeNumber": "12",
                "subgroup": "Kirara Fantasia",
                "message": "第 9 集下好了\nD:\\番剧\\S03E29.mp4",
            }
        )
        assert note.lines[0] == "进度：第 3 季第 09 集 · 共 12 集（源编号 S03E29）"
        assert "字幕组：Kirara Fantasia" in note.lines
        assert all("D:" not in line for line in note.lines)

    async def test_一个结构化字段都没有才回落原文(self) -> None:
        """最小化的 body 模板只给一段文本，这时宁可搬原文也不能让卡片空着。"""

        service, _ = _service(NexusConfig(enable_cross_match=False))
        note = await service.build(
            {"event": "下载完成", "title": "某番", "message": "某番 S01E05 下好了"}
        )
        assert note.lines[0] == "进度：第 1 季第 05 集"
        assert "某番 S01E05 下好了" in note.lines


class Test进度文案:
    """卡片正文第一行。连续编号和季内集数打架时两个都得写出来。"""

    def test_连续编号与季内集数都写出来(self) -> None:
        """只写 29 会让人以为这季有 29 集，只写 9 又对不上文件名。"""

        line = webhook._progress_line(3, 29, 9, 12)
        assert line == "进度：第 3 季第 09 集 · 共 12 集（源编号 S03E29）"

    def test_一致时不写源编号(self) -> None:
        assert webhook._progress_line(1, 5, 5, 12) == "进度：第 1 季第 05 集 · 共 12 集"

    def test_只有集数也能出一行(self) -> None:
        assert webhook._progress_line(0, 7, 0, 0) == "进度：第 07 集"

    def test_什么都没有就不占位(self) -> None:
        assert webhook._progress_line(0, 0, 0, 0) == ""


class Test条目ID解析:
    """条目 ID 是全链路唯一零歧义的作品标识，靠标题反查季度必然出错。"""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("https://bgm.tv/subject/598058", 598058),
            ("https://bangumi.tv/subject/424379/", 424379),
            ("598058", 598058),
            ("https://mikanani.me/Home/Episode/abcdef", 0),
            ("", 0),
        ],
    )
    def test_抠出条目ID(self, text: str, expected: int) -> None:
        assert webhook.parse_subject_id(text) == expected

    def test_多个来源按顺序试(self) -> None:
        """显式字段 → 链接 → 连 「${message}」 都翻一遍，用户的 body 模板哪种写法都不白丢。"""

        found = webhook.parse_subject_id(
            "", "https://mikanani.me/x", "详情：https://bgm.tv/subject/598058"
        )
        assert found == 598058


class Test上游原文清洗:
    """搬原文是最后的退路，即便退到这一步也得先洗一遍。"""

    def test_本机路径行被丢掉(self) -> None:
        """群里没人需要知道你的 D 盘目录结构。"""

        kept = webhook._message_fallback(
            "超超超超超喜欢你的100个女朋友\nD:\\番剧\\S03E29.mp4\n/volume1/media/anime/x.mkv"
        )
        assert kept == ["超超超超超喜欢你的100个女朋友"]

    def test_空字段行被丢掉(self) -> None:
        """「TMDB: 」 是用户模板里没填上的占位符残渣。"""

        kept = webhook._message_fallback("TMDB: \n评分：\n字幕组：Kirara Fantasia")
        assert kept == ["字幕组：Kirara Fantasia"]

    def test_纯符号行被丢掉(self) -> None:
        assert webhook._message_fallback("🎉🎉🎉\n下载完成") == ["下载完成"]

    def test_重复行只留一次(self) -> None:
        assert webhook._message_fallback("下载完成\n下载完成\n第 9 集") == [
            "下载完成",
            "第 9 集",
        ]

    def test_最多八行(self) -> None:
        """上游原文能长到二十几行，整段入卡会把卡片撑爆。"""

        text = "\n".join(f"第 {index} 行" for index in range(20))
        assert len(webhook._message_fallback(text)) == 8


class Test封面按季度取:
    """上游没给海报时补一张 —— 补错季度比不补更难看。"""

    async def test_有条目ID就直接取那一条(self) -> None:
        service, bangumi = _service(NexusConfig())
        note = await service.build(
            {
                "event": "下载完成",
                "title": "超超超超超喜欢你的100个女朋友",
                "season": "3",
                "url": "https://bgm.tv/subject/598058",
            }
        )
        assert note.cover == "https://img/598058.jpg"
        assert note.payload["subject_id"] == 598058
        assert bangumi.fetched == [598058]
        assert bangumi.searched == []

    async def test_没有条目ID就多取几条再按季度挑(self) -> None:
        """Bangumi 的 「sort=match」 无视季度后缀，只取第一条必然拿到第一季的封面。"""

        service, bangumi = _service(NexusConfig())
        note = await service.build(
            {"event": "下载完成", "title": "超超超超超喜欢你的100个女朋友", "season": "3"}
        )
        assert bangumi.searched == [
            ("超超超超超喜欢你的100个女朋友 第三季", RESOLVE_CANDIDATES),
        ]
        assert bangumi.fetched == []
        assert note.cover == "https://img/598058.jpg"

    async def test_上游给了海报就不查(self) -> None:
        service, bangumi = _service(NexusConfig())
        note = await service.build(
            {
                "event": "下载完成",
                "title": "超超超超超喜欢你的100个女朋友",
                "season": "3",
                "poster_url": "https://up/stream.jpg",
            }
        )
        assert note.cover == "https://up/stream.jpg"
        assert (bangumi.searched, bangumi.fetched) == ([], [])

    async def test_关掉跨源匹配就一次网络都不发(self) -> None:
        service, bangumi = _service(NexusConfig(enable_cross_match=False))
        note = await service.build(
            {"event": "下载完成", "title": "超超超超超喜欢你的100个女朋友", "season": "3"}
        )
        assert note.cover == ""
        assert (bangumi.searched, bangumi.fetched) == ([], [])


class Test编号体系保护:
    """年番第三季的「第 29 集」不能被封顶成 12/12 —— 一部还在播的番凭空完结。"""

    async def test_连续编号超出太多就不回填(self, store: Store) -> None:
        deps = _backfill_deps(store)
        await _watch(store, "超超超超超喜欢你的100个女朋友 第三季", progress=8, total=12)
        changed = await backfill_progress(
            deps,
            _watchlist(deps),
            title="超超超超超喜欢你的100个女朋友 第三季",
            episode=29,
            targets=(UMO,),
            total=12,
        )
        assert changed == 0
        rows = await store.list_watch(UMO)
        assert rows[0].progress == 8
        assert any("疑似字幕组连续编号" in note for note in deps.activity.notes)

    async def test_只差一两集还是照旧封顶(self, store: Store) -> None:
        """SP 被编成第 13 集是常事，这种差额封顶到 12 才对。"""

        deps = _backfill_deps(store)
        await _watch(store, "某番", progress=8, total=12)
        changed = await backfill_progress(
            deps, _watchlist(deps), title="某番", episode=13, targets=(UMO,), total=12
        )
        assert changed == 1
        rows = await store.list_watch(UMO)
        assert rows[0].progress == 12

    async def test_顺手补上条目缺的总集数(self, store: Store) -> None:
        """下一次回填的封顶要有依据，总集数得先落到条目上。"""

        deps = _backfill_deps(store)
        await _watch(store, "某番", progress=8)
        changed = await backfill_progress(
            deps, _watchlist(deps), title="某番", episode=9, targets=(UMO,), total=12
        )
        assert changed == 1
        rows = await store.list_watch(UMO)
        assert (rows[0].progress, rows[0].total) == (9, 12)

    async def test_不覆盖条目已有的总集数(self, store: Store) -> None:
        """用户手填过 24 话就按 24 算，上游的 12 不该把它改掉。"""

        deps = _backfill_deps(store)
        await _watch(store, "某番", progress=8, total=24)
        await backfill_progress(
            deps, _watchlist(deps), title="某番", episode=9, targets=(UMO,), total=12
        )
        rows = await store.list_watch(UMO)
        assert rows[0].total == 24

    def test_容忍范围写死为二(self) -> None:
        """放宽会把连续编号又放进来，收紧会挡掉常见的 SP / 总集篇编号。"""

        assert NUMBERING_SLACK == 2


class Test可信条目直接采用规范名:
    """上游给了条目 ID 时，日文原名对不上中文规范名也不算搜错。"""

    @staticmethod
    def _s3() -> Subject:
        return _subjects()[2]

    def test_有条目ID时采用规范名(self) -> None:
        """相似度体检会把这份完全正确的元数据判成搜错 —— 字符相似度接近 0。"""

        item = _auto_item(UMO, JP_S3, self._s3(), trusted=True)
        assert item.title == "超超超超超喜欢你的100个女朋友 第三季"
        assert (item.subject_id, item.total) == (598058, 12)

    def test_没有条目ID时保留推送原名(self) -> None:
        """只是原名对得上，就借元数据但留推送用的名字 —— 下次推送才匹配得到。"""

        item = _auto_item(UMO, JP_S3, self._s3())
        assert item.title == JP_S3
        assert item.subject_id == 598058

    def test_连原名都对不上就当没搜到(self) -> None:
        """宁可少几个字段，也不要挂错封面和集数。"""

        item = _auto_item(UMO, "完全无关的另一部番", self._s3())
        assert item.title == "完全无关的另一部番"
        assert (item.subject_id, item.total, item.cover) == (0, 0, "")
