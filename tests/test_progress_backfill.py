"""RSS 链回填追番进度，以及「播报只播我追的番」的回归锁。

为什么单独一套：这两件事都是「悄悄改用户数据 / 悄悄少发内容」，做错了不会报错，
只会让用户在某天发现进度被打回去、或者某天播报整条消失。所以把边界写死在这里：

* 回填只往前推、被总集数封顶、一部番只动最像的那一条、只影响收到通知的会话；
* 「只播我追的番」 关着就是全量、开着只留追番表里的、追番表空了就整条不发；
* 自动补追番只在指定名单里建、模糊命中不重复建、弃坑的不复活、搜不到也要建。
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from nexus.config import NexusConfig
from nexus.models import CalendarDay, FeedItem, Notification, Subject, WatchItem
from nexus.services.scheduler import Scheduler
from nexus.services.search import SearchService
from nexus.services.subscriptions import _peak_episode
from nexus.services.watchlist import (
    MATCH_THRESHOLD,
    WatchlistService,
    _auto_item,
    backfill_progress,
    ensure_watch,
)
from nexus.store import Store

UMO_A = "aiocqhttp:GroupMessage:100"
UMO_B = "aiocqhttp:GroupMessage:200"


class _Activity:
    """只记文本，方便断言降级路径确实被走到。"""

    def __init__(self) -> None:
        self.notes: list[str] = []

    def info(self, scope: str, text: str) -> None:
        self.notes.append(f"i:{scope}:{text}")

    def warn(self, scope: str, text: str) -> None:
        self.notes.append(f"w:{scope}:{text}")

    def error(self, scope: str, text: str) -> None:
        self.notes.append(f"e:{scope}:{text}")


class _Http:
    """封面一律取不到 —— 这几条用例不关心图，省掉一次网络。"""

    async def data_uris(self, urls: Any, *, max_edge: int = 0) -> dict[str, str]:
        return {}


class _Bangumi:
    def __init__(self, days: list[CalendarDay]) -> None:
        self._days = days

    async def calendar(self) -> list[CalendarDay]:
        return self._days


class _BangumiData:
    """年番那一栏直接抛错，等效于「bangumi-data 抓不到」，卡片会优雅少一栏。"""

    async def warm(self, *, span: int = 0) -> None:
        raise RuntimeError("离线")

    async def long_running(self, **kwargs: Any) -> tuple[Any, ...]:
        raise RuntimeError("离线")


def _deps(store: Store, conf: NexusConfig, *, days: list[CalendarDay] | None = None) -> Any:
    hub = SimpleNamespace(bangumi=_Bangumi(days or []), bangumi_data=_BangumiData())
    return SimpleNamespace(conf=conf, store=store, hub=hub, http=_Http(), activity=_Activity())


def _watchlist(deps: Any) -> WatchlistService:
    """回填只用到 「store」，「SearchService」 拿同一份假 deps 就够。"""

    return WatchlistService(cast(Any, deps), SearchService(cast(Any, deps)))


async def _watch(
    store: Store,
    umo: str,
    title: str,
    *,
    progress: int = 0,
    total: int = 0,
    status: str = "watching",
) -> WatchItem:
    return await store.upsert_watch(
        WatchItem(
            id=0,
            umo=umo,
            subject_id=0,
            title=title,
            status=status,
            progress=progress,
            total=total,
        )
    )


class _BangumiSearch(_Bangumi):
    """会搜出条目的 Bangumi —— 自动建条目要靠它借封面、总集数、评分。"""

    def __init__(self, subject: Subject | None) -> None:
        super().__init__([])
        self._subject = subject

    async def search(
        self, keyword: str, *, limit: int = 1, subject_type: int | None = None
    ) -> list[Subject]:
        return [self._subject] if self._subject else []


def _deps_search(store: Store, subject: Subject | None) -> Any:
    """带 Bangumi 搜索能力的假 deps；「_deps」 那份故意没有 「search」，等效于搜索炸掉。"""

    hub = SimpleNamespace(bangumi=_BangumiSearch(subject), bangumi_data=_BangumiData())
    return SimpleNamespace(
        conf=NexusConfig(), store=store, hub=hub, http=_Http(), activity=_Activity()
    )


def _subject(**kwargs: Any) -> Subject:
    base: dict[str, Any] = {
        "id": 302286,
        "name": "薬屋のひとりごと",
        "name_cn": "药屋少女的呢喃",
        "image": "https://example.invalid/cover.jpg",
        "total_episodes": 24,
        "score": 8.4,
        "air_weekday": 6,
    }
    base.update(kwargs)
    return Subject(**base)


@pytest.fixture
async def store(tmp_path: Path):
    inst = Store(tmp_path / "nexus.db")
    await inst.initialize()
    yield inst
    await inst.close()


class Test最大集号:
    """一轮发布里取哪一集，决定进度会跳到哪。"""

    @staticmethod
    def _items(*titles: str) -> list[FeedItem]:
        return [FeedItem(uid=title, title=title) for title in titles]

    def test_一轮补档取最大集(self) -> None:
        """字幕组补档常一次发 05、06、07，进度理应跟到 07 而不是 05。"""

        items = self._items(
            "[组] 某番 - 05 [Baha][1080p]",
            "[组] 某番 - 07 [Baha][1080p]",
            "[组] 某番 - 06 [Baha][1080p]",
        )
        assert _peak_episode(items) == 7

    def test_前导零照样认(self) -> None:
        assert _peak_episode(self._items("[组] 某番 - 09 [Baha]")) == 9

    def test_sp与ova不算集数(self) -> None:
        """把 SP 当正片集号写进去，进度条会莫名超过 100%。"""

        assert _peak_episode(self._items("[组] 某番 - SP [Baha]", "[组] 某番 OVA [Baha]")) == 0

    def test_空批次是零(self) -> None:
        assert _peak_episode([]) == 0


class Test回填只往前推:
    async def test_新集数推进进度(self, store: Store) -> None:
        deps = _deps(store, NexusConfig())
        await _watch(store, UMO_A, "碧蓝之海 3", progress=8)
        changed = await backfill_progress(
            deps, _watchlist(deps), title="碧蓝之海 3", episode=9, targets=(UMO_A,)
        )
        assert changed == 1
        rows = await store.list_watch(UMO_A)
        assert rows[0].progress == 9

    async def test_补发老集不把进度打回去(self, store: Store) -> None:
        """字幕组补发前几集是常事，回填绝不能让用户的进度倒退。"""

        deps = _deps(store, NexusConfig())
        await _watch(store, UMO_A, "碧蓝之海 3", progress=8)
        changed = await backfill_progress(
            deps, _watchlist(deps), title="碧蓝之海 3", episode=3, targets=(UMO_A,)
        )
        assert changed == 0
        rows = await store.list_watch(UMO_A)
        assert rows[0].progress == 8

    async def test_同集重复到达只改一次(self, store: Store) -> None:
        deps = _deps(store, NexusConfig())
        await _watch(store, UMO_A, "碧蓝之海 3", progress=8)
        wl = _watchlist(deps)
        first = await backfill_progress(deps, wl, title="碧蓝之海 3", episode=9, targets=(UMO_A,))
        second = await backfill_progress(deps, wl, title="碧蓝之海 3", episode=9, targets=(UMO_A,))
        assert (first, second) == (1, 0)


class Test回填被总集数封顶:
    async def test_超出总集数时只推到总集数(self, store: Store) -> None:
        """有的源把 SP 编成「13」，硬写会让进度条超过 100%。"""

        deps = _deps(store, NexusConfig())
        await _watch(store, UMO_A, "某番", progress=11, total=12)
        changed = await backfill_progress(
            deps, _watchlist(deps), title="某番", episode=13, targets=(UMO_A,)
        )
        assert changed == 1
        assert (await store.list_watch(UMO_A))[0].progress == 12

    async def test_已经看完时不再改(self, store: Store) -> None:
        deps = _deps(store, NexusConfig())
        await _watch(store, UMO_A, "某番", progress=12, total=12)
        changed = await backfill_progress(
            deps, _watchlist(deps), title="某番", episode=13, targets=(UMO_A,)
        )
        assert changed == 0

    async def test_没填总集数时不封顶(self, store: Store) -> None:
        deps = _deps(store, NexusConfig())
        await _watch(store, UMO_A, "某番", progress=1, total=0)
        await backfill_progress(deps, _watchlist(deps), title="某番", episode=30, targets=(UMO_A,))
        assert (await store.list_watch(UMO_A))[0].progress == 30


class Test回填不串台:
    async def test_一部番只动最像的那一条(self, store: Store) -> None:
        """「进击的巨人」 和 「进击的巨人 最终季」 会同时命中阈值，全改就串台了。"""

        deps = _deps(store, NexusConfig())
        await _watch(store, UMO_A, "进击的巨人", progress=1)
        await _watch(store, UMO_A, "进击的巨人 最终季", progress=1)
        changed = await backfill_progress(
            deps, _watchlist(deps), title="进击的巨人 最终季", episode=5, targets=(UMO_A,)
        )
        assert changed == 1
        moved = {item.title: item.progress for item in await store.list_watch(UMO_A)}
        assert moved["进击的巨人 最终季"] == 5
        assert moved["进击的巨人"] == 1

    async def test_只影响收到通知的会话(self, store: Store) -> None:
        """订阅是按会话建的，A 群订的番不该把 B 群的进度也推动。"""

        deps = _deps(store, NexusConfig())
        await _watch(store, UMO_A, "某番", progress=1)
        await _watch(store, UMO_B, "某番", progress=1)
        changed = await backfill_progress(
            deps, _watchlist(deps), title="某番", episode=6, targets=(UMO_A,)
        )
        assert changed == 1
        assert (await store.list_watch(UMO_A))[0].progress == 6
        assert (await store.list_watch(UMO_B))[0].progress == 1

    async def test_名字差太远就不认(self, store: Store) -> None:
        deps = _deps(store, NexusConfig())
        await _watch(store, UMO_A, "碧蓝之海 3", progress=1)
        changed = await backfill_progress(
            deps, _watchlist(deps), title="孤独摇滚", episode=4, targets=(UMO_A,)
        )
        assert changed == 0

    @pytest.mark.parametrize(("title", "episode"), [("某番", 0), ("某番", -1), ("", 5)])
    async def test_集号或标题不合法时直接放弃(self, store: Store, title: str, episode: int) -> None:
        deps = _deps(store, NexusConfig())
        await _watch(store, UMO_A, "某番", progress=1)
        changed = await backfill_progress(
            deps, _watchlist(deps), title=title, episode=episode, targets=(UMO_A,)
        )
        assert changed == 0

    async def test_空会话名跳过(self, store: Store) -> None:
        deps = _deps(store, NexusConfig())
        await _watch(store, UMO_A, "某番", progress=1)
        changed = await backfill_progress(
            deps, _watchlist(deps), title="某番", episode=6, targets=("", UMO_A)
        )
        assert changed == 1


def _scheduler(deps: Any, *, with_watchlist: bool = True) -> Scheduler:
    """只跑 「_backfill」 这一条路径，其余协作者给占位对象。"""

    return Scheduler(
        cast(Any, deps),
        search=cast(Any, SimpleNamespace()),
        subscriptions=cast(Any, SimpleNamespace()),
        notifier=cast(Any, SimpleNamespace()),
        watchlist=_watchlist(deps) if with_watchlist else None,
    )


def _notice(title: str, episode: Any) -> Notification:
    payload: dict[str, Any] = {"episode": episode} if episode is not None else {}
    return Notification(kind="rss", title=title, payload=payload)


class Test调度器把rss更新接到回填:
    async def test_默认开着就会回填(self, store: Store) -> None:
        deps = _deps(store, NexusConfig())
        assert deps.conf.rss_auto_progress is True
        await _watch(store, UMO_A, "某番", progress=2)
        await _scheduler(deps)._backfill(UMO_A, _notice("某番", 5))
        assert (await store.list_watch(UMO_A))[0].progress == 5

    async def test_关掉开关就不动数据(self, store: Store) -> None:
        deps = _deps(store, NexusConfig(rss_auto_progress=False))
        await _watch(store, UMO_A, "某番", progress=2)
        await _scheduler(deps)._backfill(UMO_A, _notice("某番", 5))
        assert (await store.list_watch(UMO_A))[0].progress == 2

    async def test_没装追番服务时安静跳过(self, store: Store) -> None:
        deps = _deps(store, NexusConfig())
        await _watch(store, UMO_A, "某番", progress=2)
        await _scheduler(deps, with_watchlist=False)._backfill(UMO_A, _notice("某番", 5))
        assert (await store.list_watch(UMO_A))[0].progress == 2

    @pytest.mark.parametrize("episode", [None, 0, "", "SP", "abc"])
    async def test_通知里没有可用集号时不动数据(self, store: Store, episode: Any) -> None:
        """「episode」 是 1.2.0 才加的字段，老通知或非数字集号都要能兜住。"""

        deps = _deps(store, NexusConfig())
        await _watch(store, UMO_A, "某番", progress=2)
        await _scheduler(deps)._backfill(UMO_A, _notice("某番", episode))
        assert (await store.list_watch(UMO_A))[0].progress == 2


def _day(*names: str) -> list[CalendarDay]:
    items = tuple(
        Subject(
            id=index + 1,
            name=name,
            name_cn=name,
            score=9 - index * 0.1,
            collection={"doing": 100},
        )
        for index, name in enumerate(names)
    )
    return [CalendarDay(weekday=3, label="周三", items=items)]


class Test播报只播我追的番:
    async def test_关着开关时全量播报(self, store: Store) -> None:
        deps = _deps(store, NexusConfig(), days=_day("甲番", "乙番"))
        reply = await SearchService(cast(Any, deps)).digest(UMO_A, weekday=3)
        assert "甲番" in reply.text
        assert "乙番" in reply.text

    async def test_开着开关时只留追番表里的(self, store: Store) -> None:
        deps = _deps(store, NexusConfig(push_only_watchlist=True), days=_day("甲番", "乙番"))
        await _watch(store, UMO_A, "甲番")
        reply = await SearchService(cast(Any, deps)).digest(UMO_A, weekday=3)
        assert "甲番" in reply.text
        assert "乙番" not in reply.text

    async def test_追番表为空时整条不发(self, store: Store) -> None:
        """一部都没追却照旧刷全量，等于开关没生效。"""

        deps = _deps(store, NexusConfig(push_only_watchlist=True), days=_day("甲番", "乙番"))
        reply = await SearchService(cast(Any, deps)).digest(UMO_A, weekday=3)
        assert reply.empty

    async def test_追的番今天都不播时也不发(self, store: Store) -> None:
        deps = _deps(store, NexusConfig(push_only_watchlist=True), days=_day("甲番"))
        await _watch(store, UMO_A, "丙番")
        reply = await SearchService(cast(Any, deps)).digest(UMO_A, weekday=3)
        assert reply.empty

    async def test_已弃坑的不算在追(self, store: Store) -> None:
        deps = _deps(store, NexusConfig(push_only_watchlist=True), days=_day("甲番"))
        await _watch(store, UMO_A, "甲番", status="dropped")
        reply = await SearchService(cast(Any, deps)).digest(UMO_A, weekday=3)
        assert reply.empty

    async def test_别的会话的追番表不算(self, store: Store) -> None:
        """追番表是按会话存的，B 群追的番不该决定 A 群播什么。"""

        deps = _deps(store, NexusConfig(push_only_watchlist=True), days=_day("甲番"))
        await _watch(store, UMO_B, "甲番")
        reply = await SearchService(cast(Any, deps)).digest(UMO_A, weekday=3)
        assert reply.empty


class Test认领关系不会插出重复行:
    async def test_按id更新时连标题一起改(self, store: Store) -> None:
        """1.2.0 之前 「upsert_watch」 只按 (会话, 标题) 认行 —— 外部改过标题后
        同一部番会再插一条。ani-rss 同步把日文名换成中文名就会踩到。
        """

        row = await _watch(store, UMO_A, "キミとアイドルプリキュア", progress=3)
        row.title = "名侦探光之美少女"
        row.progress = 4
        saved = await store.upsert_watch(row)
        assert saved.id == row.id
        rows = await store.list_watch(UMO_A)
        assert [(item.id, item.title, item.progress) for item in rows] == [
            (row.id, "名侦探光之美少女", 4)
        ]

    async def test_标题撞上另一行时合并成一条(self, store: Store) -> None:
        """唯一索引不允许同名并存，撞车时保留带 id 的那行，别抛给用户。"""

        keep = await _watch(store, UMO_A, "甲番", progress=5)
        await _watch(store, UMO_A, "乙番", progress=1)
        keep.title = "乙番"
        saved = await store.upsert_watch(keep)
        assert saved.id == keep.id
        rows = await store.list_watch(UMO_A)
        assert [(item.id, item.title, item.progress) for item in rows] == [(keep.id, "乙番", 5)]

    async def test_id指向别的会话时不误改(self, store: Store) -> None:
        """id 只在本会话内有效，跨会话必须当成新行。"""

        other = await _watch(store, UMO_B, "甲番", progress=9)
        mine = WatchItem(id=other.id, umo=UMO_A, subject_id=0, title="甲番", progress=1)
        saved = await store.upsert_watch(mine)
        assert saved.id != other.id
        assert (await store.list_watch(UMO_B))[0].progress == 9

    async def test_id已经被删掉时退回新增(self, store: Store) -> None:
        row = await _watch(store, UMO_A, "甲番", progress=5)
        await store.delete_watch(row.id)
        row.updated_at = time.time()
        saved = await store.upsert_watch(row)
        assert saved.id > 0
        assert len(await store.list_watch(UMO_A)) == 1


class Test首推自动加入追番表:
    """下载器第一次推某部番过来时，追番表该自己长出这一条。

    这条链最容易「静默地半坏」：通知照发、进度永远停在 0，用户只会觉得回填是坏的。
    所以把「什么时候建、什么时候绝对不建」全部钉在这里。
    """

    async def test_表里没有就补一条(self, store: Store) -> None:
        deps = _deps_search(store, _subject())
        created = await ensure_watch(
            deps, _watchlist(deps), title="药屋少女的呢喃", targets=(UMO_A,)
        )
        assert created == (UMO_A,)
        rows = await store.list_watch(UMO_A)
        assert [(item.title, item.status, item.progress) for item in rows] == [
            ("药屋少女的呢喃", "watching", 0)
        ]

    async def test_搜到条目时借用元数据(self, store: Store) -> None:
        """封面、总集数是卡片和进度条的原料，能借就借。"""

        deps = _deps_search(store, _subject())
        await ensure_watch(deps, _watchlist(deps), title="药屋少女的呢喃", targets=(UMO_A,))
        row = (await store.list_watch(UMO_A))[0]
        assert (row.subject_id, row.total, row.cover) == (
            302286,
            24,
            "https://example.invalid/cover.jpg",
        )

    async def test_搜不到也要建(self, store: Store) -> None:
        """冷门番、网络抖动都可能搜不到；静默什么都不做正是这个功能要消灭的体验。"""

        deps = _deps_search(store, None)
        created = await ensure_watch(deps, _watchlist(deps), title="测试番剧甲", targets=(UMO_A,))
        assert created == (UMO_A,)
        row = (await store.list_watch(UMO_A))[0]
        assert (row.title, row.subject_id, row.total) == ("测试番剧甲", 0, 0)

    async def test_相似标题不重复添加(self, store: Store) -> None:
        """表里已有本篇时，「第二季」 这类写法不该再插一条 —— 一部番出现两次最难发现。"""

        await _watch(store, UMO_A, "药屋少女的呢喃", progress=7)
        deps = _deps_search(store, _subject())
        created = await ensure_watch(
            deps, _watchlist(deps), title="药屋少女的呢喃 第二季", targets=(UMO_A,)
        )
        assert created == ()
        assert len(await store.list_watch(UMO_A)) == 1

    async def test_弃坑的不复活也不重建(self, store: Store) -> None:
        """用户主动弃掉的番，不该被下载器一条通知拉回来，也不该旁边多一条新的。"""

        await _watch(store, UMO_A, "药屋少女的呢喃", status="dropped")
        deps = _deps_search(store, _subject())
        created = await ensure_watch(
            deps, _watchlist(deps), title="药屋少女的呢喃", targets=(UMO_A,)
        )
        assert created == ()
        rows = await store.list_watch(UMO_A)
        assert [(item.title, item.status) for item in rows] == [("药屋少女的呢喃", "dropped")]

    async def test_只在传入名单里建(self, store: Store) -> None:
        """往没点名的会话里塞数据是越权，哪怕它也订阅了同一部番。"""

        deps = _deps_search(store, _subject())
        await ensure_watch(deps, _watchlist(deps), title="药屋少女的呢喃", targets=(UMO_A,))
        assert await store.list_watch(UMO_B) == []

    async def test_名单里重复会话只建一条(self, store: Store) -> None:
        """配置里手抖写重、或空串混进来，都不该变成两条记录。"""

        deps = _deps_search(store, _subject())
        created = await ensure_watch(
            deps, _watchlist(deps), title="药屋少女的呢喃", targets=(UMO_A, UMO_A, "")
        )
        assert created == (UMO_A,)
        assert len(await store.list_watch(UMO_A)) == 1

    async def test_空标题什么都不做(self, store: Store) -> None:
        deps = _deps_search(store, _subject())
        assert await ensure_watch(deps, _watchlist(deps), title="", targets=(UMO_A,)) == ()
        assert await store.list_watch(UMO_A) == []

    async def test_补条目后同一轮就能回填进度(self, store: Store) -> None:
        """先建再回填的顺序是有意的：新建那条在同一次请求里就该拿到进度。"""

        deps = _deps_search(store, _subject())
        watchlist = _watchlist(deps)
        await ensure_watch(deps, watchlist, title="药屋少女的呢喃", targets=(UMO_A,))
        changed = await backfill_progress(
            deps, watchlist, title="药屋少女的呢喃", episode=3, targets=(UMO_A,)
        )
        assert changed == 1
        assert (await store.list_watch(UMO_A))[0].progress == 3

    async def test_建条目会留下运行记录(self, store: Store) -> None:
        """悄悄改用户数据必须留痕，出问题时能在运行记录里对账。"""

        deps = _deps_search(store, _subject())
        await ensure_watch(deps, _watchlist(deps), title="药屋少女的呢喃", targets=(UMO_A,))
        assert any("自动加入追番表" in note for note in deps.activity.notes)


class Test自动条目怎么挑标题:
    """条目标题必须和推送用的名字足够像，否则下一次推送既匹配不到、又会再插一条。"""

    def test_规范名足够像时采用规范名(self) -> None:
        item = _auto_item(UMO_A, "药屋少女的呢喃", _subject())
        assert (item.title, item.subject_id, item.total) == ("药屋少女的呢喃", 302286, 24)
        assert item.status == "watching"

    def test_只有原名对得上时保留推送名(self) -> None:
        """推送给日文原名、Bangumi 返回中文译名：借元数据，但标题跟着推送走。"""

        subject = _subject(name="キミとアイドルプリキュア", name_cn="名侦探光之美少女")
        item = _auto_item(UMO_A, "キミとアイドルプリキュア", subject)
        assert item.title == "キミとアイドルプリキュア"
        assert item.subject_id == 302286

    def test_完全不像时退回只有名字的条目(self) -> None:
        """搜歪了比搜不到更糟 —— 挂错封面和集数会一直错下去。"""

        subject = _subject(name="完全不同的作品", name_cn="")
        item = _auto_item(UMO_A, "测试番剧甲", subject, cover="https://example.invalid/x.jpg")
        assert (item.title, item.subject_id, item.total) == ("测试番剧甲", 0, 0)
        assert item.cover == "https://example.invalid/x.jpg"

    def test_搜不到时退回推送给的元数据(self) -> None:
        item = _auto_item(UMO_A, "测试番剧甲", None, total=12)
        assert (item.title, item.subject_id, item.total) == ("测试番剧甲", 0, 12)

    def test_门槛用的是共享常量(self) -> None:
        """四处判定必须同一个数，各写一个字面量就会出现「匹配得上却不认」。"""

        assert pytest.approx(0.72) == MATCH_THRESHOLD
