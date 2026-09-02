"""追番表服务。

对应上游 「/追番」 「/弃坑」 「/放送时间」 的会话侧数据，外加本插件新增的
「/追番列表」 「/看到」。追番数据一律落本地 SQLite（「StarTools.get_data_dir()」 下），
不写 Bangumi 账号 —— 免得逼用户交 token 才能用最基本的功能。

模块末尾两个函数是给后台链路（RSS 轮询、下载器 Webhook、ani-rss 同步）共用的：
「ensure_watch」 负责「表里还没有就补一条」，「backfill_progress」 负责「有了就把进度
往前推」。两件事分开写是因为开关也是分开的 —— 有人只想要自动记账，有人连清单都不想
被动过。
"""

from __future__ import annotations

import time
from collections.abc import Sequence

from ..constants import MAX_WATCHLIST_PER_SESSION, WATCH_STATUS_CN
from ..models import MatchResult, Subject, WatchItem
from ..render import build_notice_card, build_watchlist_card
from ..titles import MATCH_THRESHOLD, similarity
from .base import Deps, Reply, cover_map, cover_uri, make_card, numeric, style_for
from .search import SearchService

STATUS_WATCHING = "watching"
STATUS_PLANNED = "planned"
STATUS_FINISHED = "finished"
STATUS_DROPPED = "dropped"

#: 允许「上游集数超出总集数」的宽容额度。SP/OVA 被编成第 13 集这种情况差 1~2 集，
#: 封顶到全话数是对的；差得更多（年番第三季给出连续编号 29）说明上游用的是
#: 另一套编号体系，硬封顶会把 12/12 写成假完结，这种只能拒绝回填。
NUMBERING_SLACK = 2


class WatchlistService:
    """每个会话一份追番表：加入、弃坑、推进度、看全表。"""

    def __init__(self, deps: Deps, search: SearchService) -> None:
        self._deps = deps
        self._search = search

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    async def add(self, umo: str, query: str) -> tuple[Reply, MatchResult | None]:
        """加入追番表。

        返回值第二项是跨源匹配结果：main.py 拿它顺手推荐可一键订阅的
        Mikan / RSSHub 源，省得用户自己去翻 RSS 地址。
        """
        deps = self._deps
        query = query.strip()
        if not query:
            return Reply.plain("要追哪部？例：/追番 药屋少女的呢喃"), None

        existing = await deps.store.find_watch(umo, query)
        if existing is not None and existing.status == STATUS_DROPPED:
            await deps.store.update_watch(
                existing.id, status=STATUS_WATCHING, updated_at=time.time()
            )
            return Reply.plain(f"「{existing.title}」已从弃坑堆里捞回来，继续追。"), None
        if existing is not None:
            return Reply.plain(
                f"「{existing.title}」已经在追番表里了（{existing.progress_label}）。"
            ), None

        current = await deps.store.list_watch(umo)
        if len(current) >= MAX_WATCHLIST_PER_SESSION:
            return Reply.plain(
                f"这个会话的追番表已经有 {len(current)} 条，先 /弃坑 清理一些再加。"
            ), None

        subject = await self._search.resolve(query)
        if subject is None:
            return Reply.plain(f"没找到「{query}」，换个名字或者给条目 ID。"), None

        match = await deps.matcher.enrich(subject)
        item = WatchItem(
            id=0,
            umo=umo,
            subject_id=subject.id,
            title=subject.display_name,
            status=STATUS_WATCHING,
            progress=0,
            total=subject.total_episodes or subject.eps,
            score=subject.score,
            cover=subject.image,
            weekday=subject.air_weekday,
        )
        stored = await deps.store.upsert_watch(item)
        deps.activity.info("watchlist", f"{umo} 追番 {item.title}")
        return await self._added_card(umo, stored, subject, match), match

    async def _added_card(
        self, umo: str, item: WatchItem, subject: Subject, match: MatchResult
    ) -> Reply:
        """「已加入追番表」结果卡。

        上游这一步只回一行纯文本，字数一超阈值就被 t2i 转成一张灰底文字图 ——
        既不好看，也看不出加进来的到底是哪部番。所以这里走正经通知卡：
        带封面、带放送与集数，用户一眼能确认「加对了没」。
        """
        deps = self._deps
        conf = deps.conf
        theme, _ = await style_for(deps, umo)
        next_air = deps.matcher.next_air_label(match)

        chips = [
            chip
            for chip in (
                f"评分 {subject.score_label}" if subject.score else "",
                subject.weekday_label,
                f"全 {item.total} 话" if item.total else "",
                subject.type_label,
            )
            if chip
        ]
        lines = [f"名称：{item.title}"]
        if subject.alt_name:
            lines.append(f"原名：{subject.alt_name}")
        if next_air:
            lines.append(f"下一集：{next_air}")
        elif subject.air_date:
            lines.append(f"开播：{subject.air_date}")
        if item.total:
            lines.append(f"进度：0 / {item.total}")
        lines.append("记进度：/看到 " + item.title + " +1")
        lines.append("不想追了：/弃坑 " + item.title)

        html = build_notice_card(
            theme,
            eyebrow="WATCHLIST",
            title="已加入追番表",
            subtitle=item.title,
            lines=lines,
            chips=chips,
            cover=await cover_uri(deps, item.cover),
            width=conf.card_width,
            stamp="WATCH",
        )
        plain = "\n".join([f"已加入追番表：{item.title}", *lines[1:]])
        return Reply(
            text=plain,
            card=make_card(
                html,
                plain=plain,
                title="已加入追番表",
                eyebrow="WATCHLIST",
                subtitle=item.title,
                chips=chips,
                theme=theme,
                width=conf.card_width,
            ),
        )

    async def drop(self, umo: str, query: str) -> Reply:
        """标记弃坑。记录保留，方便以后 「/追番」 复活。"""
        deps = self._deps
        query = query.strip()
        if not query:
            return Reply.plain("要弃哪部？例：/弃坑 某部番")
        item = await deps.store.find_watch(umo, query)
        if item is None:
            return Reply.plain(f"追番表里没有「{query}」。")
        if item.status == STATUS_DROPPED:
            return Reply.plain(f"「{item.title}」早就弃了。")
        await deps.store.update_watch(item.id, status=STATUS_DROPPED, updated_at=time.time())
        return Reply.plain(f"「{item.title}」已标记为弃坑，记录还留着，想回来再 /追番 一次。")

    async def progress(self, umo: str, query: str, episode: str) -> Reply:
        """更新观看进度。「+1」 表示往前推一集，纯数字表示直接跳到某集。"""
        deps = self._deps
        item = await deps.store.find_watch(umo, query.strip())
        if item is None:
            return Reply.plain(f"追番表里没有「{query.strip()}」，先 /追番 一下。")

        token = episode.strip()
        if token in {"", "+1", "+"}:
            target = item.progress + 1
        elif token.startswith("+") and token[1:].isdigit():
            target = item.progress + int(token[1:])
        elif token.startswith("-") and token[1:].isdigit():
            target = item.progress - int(token[1:])
        else:
            value = numeric(token)
            if not value and token != "0":
                return Reply.plain("集数看不懂，写 /看到 名称 7 或 /看到 名称 +1。")
            target = value
        target = max(0, target)
        if item.total:
            target = min(target, item.total)

        fields: dict[str, object] = {"progress": target, "updated_at": time.time()}
        finished = bool(item.total) and target >= item.total
        if finished and item.status != STATUS_FINISHED:
            fields["status"] = STATUS_FINISHED
        elif not finished and item.status in {STATUS_FINISHED, STATUS_DROPPED}:
            fields["status"] = STATUS_WATCHING
        await deps.store.update_watch(item.id, **fields)

        total = f"/{item.total}" if item.total else ""
        tail = "，全剧看完，恭喜。" if finished else "。"
        return Reply.plain(f"「{item.title}」进度更新到 {target}{total}{tail}")

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------
    async def overview(self, umo: str, *, status: str = "") -> Reply:
        """追番进度卡：进度条、放送倒计时、评分一览。"""
        deps = self._deps
        conf = deps.conf
        items = await deps.store.list_watch(umo, status=status)
        if not items:
            return Reply.plain("追番表还是空的，用 /追番 <名称> 加第一部。")

        ordered = sorted(items, key=_sort_key)
        theme, _ = await style_for(deps, umo)
        covers = await cover_map(deps, ((item.subject_id, item.cover) for item in ordered[:24]))
        airing = await self._airing_labels(ordered[:24])
        html = build_watchlist_card(
            theme,
            ordered,
            width=conf.card_width,
            covers=covers,
            airing=airing,
        )
        plain = _watchlist_plain(ordered, airing)
        return Reply(
            text=plain,
            card=make_card(
                html,
                plain=plain,
                title="我的追番",
                eyebrow="WATCHLIST",
                subtitle=f"共 {len(ordered)} 部",
                theme=theme,
                width=conf.card_width,
            ),
        )

    async def _airing_labels(self, items: Sequence[WatchItem]) -> dict[int, str]:
        """给在追的条目算「还差几天」标签。

        只对 「watching」 状态的条目做跨源查询：已看完/弃坑的算倒计时没意义，
        也能省掉一堆没必要的 HTTP 请求。
        """
        deps = self._deps
        if not deps.conf.enable_cross_match:
            return {}
        labels: dict[int, str] = {}
        for item in items:
            if item.status != STATUS_WATCHING:
                continue
            try:
                match = await deps.matcher.by_title(item.title)
                label = deps.matcher.next_air_label(match)
            except Exception:  # noqa: BLE001 - 单条失败不该毁掉整张卡
                continue
            if label:
                labels[item.subject_id] = label
        return labels

    async def find(self, umo: str, query: str) -> WatchItem | None:
        return await self._deps.store.find_watch(umo, query.strip())

    async def matching_titles(
        self, umo: str, title: str, *, threshold: float = MATCH_THRESHOLD
    ) -> list[WatchItem]:
        """按标题相似度找会话里的追番条目 —— RSS 更新回填进度时用。

        故意不过滤状态：弃坑的条目也要返回。上层拿这个结果判断「表里有没有这部」，
        漏掉弃坑条目就会把用户主动弃掉的番当成缺失、再自动补一条回来。
        """
        items = await self._deps.store.list_watch(umo)
        hits = [item for item in items if similarity(item.title, title) >= threshold]
        return sorted(hits, key=lambda item: similarity(item.title, title), reverse=True)

    async def resolve_subject(self, title: str, *, subject_id: int = 0) -> Subject | None:
        """按名字找 Bangumi 条目 —— 自动建条目时用来补封面、总集数、评分。

        单独包一层而不是让调用方直接摸 「self._search」：自动建条目跑在 Webhook /
        定时任务这类后台链路上，搜索失败只该让新条目「少几个字段」，不该把整条
        请求带崩，所以异常在这里就吞掉并留一条运行记录。

        给了 「subject_id」 就直接取那一条。上游链接里带着条目 ID 时它比任何名字
        搜索都准 —— 名字搜索认不出季度后缀，第三季的更新会拿到第一季的元数据。
        """
        try:
            if subject_id:
                return await self._deps.hub.bangumi.subject(subject_id)
            return await self._search.resolve(title)
        except Exception as error:  # noqa: BLE001 - 搜不到就退化成最简条目
            self._deps.activity.warn("watchlist", f"自动建条目查询「{title}」失败：{error}")
            return None


async def backfill_progress(
    deps: Deps,
    watchlist: WatchlistService,
    *,
    title: str,
    episode: int,
    targets: Sequence[str],
    channel: str = "watch",
    total: int = 0,
) -> int:
    """把追番进度推到指定集数，返回实际改动的条目数。

    Webhook（下载完成）和 RSS（字幕组发布）两条链都要做这件事，所以抽到这里 ——
    两处各写一遍的话，「只往前推」 这条规则迟早在一边被写漏。

    三条不变量：

    * **只往前，不往后**。补种老集数、字幕组补发前几集都不该把用户已看到的进度打回去。
    * **总集数封顶，但差太多就不写**。有的源把 SP/OVA 编成 「13」，封顶到 12 是对的；
      年番第三季却可能给出「第 29 集」这种从首季数起的连续编号，封顶会把它写成
      12/12 —— 一部还在播的番凭空完结。差额超过 「NUMBERING_SLACK」 时宁可不动，
      并留一条运行记录让用户知道为什么没动。
    * **一部番只认最像的那一条**。同一会话里 「进击的巨人」 和 「进击的巨人 最终季」
      都可能匹配上，全改会串台，所以只动相似度最高的那条。
    """

    if episode <= 0 or not title:
        return 0
    changed = 0
    for session in targets:
        if not session:
            continue
        try:
            hits = await watchlist.matching_titles(session, title)
        except Exception as error:  # noqa: BLE001 - 单个会话读失败不该拖垮整轮回填
            deps.activity.warn(channel, f"匹配追番表失败：{error}")
            continue
        for item in hits[:1]:
            if item.progress >= episode:
                continue
            ceiling = total or item.total
            capped = episode
            if ceiling and episode > ceiling:
                if episode - ceiling > NUMBERING_SLACK:
                    deps.activity.warn(
                        channel,
                        f"「{item.title}」上游给的第 {episode} 集比全 {ceiling} 话超出太多，"
                        "疑似字幕组连续编号，本次不回填",
                    )
                    continue
                capped = ceiling
            if capped <= item.progress:
                continue
            fields: dict[str, object] = {"progress": capped, "updated_at": time.time()}
            if total and not item.total:
                # 顺手补上条目缺失的总集数（不覆盖已有值），下一次回填的封顶才有依据。
                fields["total"] = total
            try:
                await deps.store.update_watch(item.id, **fields)
            except Exception as error:  # noqa: BLE001
                deps.activity.warn(channel, f"回填进度失败：{error}")
                continue
            changed += 1
            deps.activity.info(channel, f"{session} 的「{item.title}」进度回填到 {capped}")
    return changed


async def ensure_watch(
    deps: Deps,
    watchlist: WatchlistService,
    *,
    title: str,
    targets: Sequence[str],
    cover: str = "",
    total: int = 0,
    channel: str = "watch",
    subject_id: int = 0,
) -> tuple[str, ...]:
    """把还没进追番表的番自动补一条，返回真正新建了条目的会话。

    为什么需要它：下载器（ani-rss / AutoBangumi）第一次推某部番过来时，追番表里
    往往还没有这一条 —— 用户在下载器那边订阅，本身就是在表达「我要追」，却还得回
    聊天窗口再 「/追番 番名」 一遍进度才会开始动。少做这一步的后果很隐蔽：通知照
    发、进度永远停在 0，用户只会觉得「回填是坏的」。

    四条不变量：

    * **只在指定名单里建**。传进来的通常是 Webhook 的固定通知目标；追番联动派生
      出来的会话本来就有条目，往陌生会话里塞数据更是越权。
    * **模糊命中就算已有**。判定复用回填那套相似度匹配（「MATCH_THRESHOLD」），
      表里已有 「药屋少女的呢喃」 时不会再插一条 「药屋少女的呢喃 第二季」。
    * **弃坑的不复活**。「matching_titles」 连弃坑条目一起查，命中即跳过 ——
      用户主动弃掉的番不该被下载器一条通知拉回来。
    * **搜不到也要建**。下载器给的都是真实番名，Bangumi 偶尔搜不到（网络抖动、
      冷门番、只给了日文原名），这时宁可建一条只有名字的条目，也不要静默地什么都
      不做 —— 「静默什么都不做」正是这个功能要消灭的体验。
    """

    if not title:
        return ()
    subject: Subject | None = None
    looked_up = False
    created: list[str] = []
    for session in dict.fromkeys(target for target in targets if target):
        try:
            hits = await watchlist.matching_titles(session, title)
        except Exception as error:  # noqa: BLE001 - 单个会话读失败不该拖垮整轮
            deps.activity.warn(channel, f"匹配追番表失败：{error}")
            continue
        if hits:
            continue
        if not looked_up:
            # 一条事件推给多个会话时元数据是同一份，Bangumi 只查一次。
            looked_up = True
            subject = await watchlist.resolve_subject(title, subject_id=subject_id)
        item = _auto_item(
            session, title, subject, cover=cover, total=total, trusted=bool(subject_id)
        )
        try:
            stored = await deps.store.upsert_watch(item)
        except Exception as error:  # noqa: BLE001 - 超上限或写库失败只跳过这个会话
            deps.activity.warn(channel, f"自动加入追番表失败：{error}")
            continue
        created.append(session)
        deps.activity.info(channel, f"{session} 自动加入追番表：{stored.title}")
    return tuple(created)


def _auto_item(
    umo: str,
    title: str,
    subject: Subject | None,
    *,
    cover: str = "",
    total: int = 0,
    trusted: bool = False,
) -> WatchItem:
    """给「自动建条目」挑标题与元数据。

    唯一的硬约束：条目标题必须和推送用的名字足够像。够不上的话，下一次推送既匹配
    不到这一条（进度永远停在 0），又会再插一条重复记录 —— 一部番在表里出现两次。
    所以 Bangumi 的规范名只在足够接近时才采用；只是原名对得上（推送给的是日文名、
    Bangumi 返回中文名）就保留推送原名、只借元数据；连原名都对不上就当没搜到，
    宁可少几个字段，也不要挂错封面和集数。

    「trusted」 表示条目不是猜出来的（上游直接给了 Bangumi 条目 ID），这时跳过相似度
    体检直接采用规范名 —— 「君のことが大大大大大好きな100人の彼女 第3期」 和中文推送名
    的字符相似度接近 0，按名字判会被当成搜错，反而丢掉一份完全正确的元数据。
    """
    fallback = WatchItem(
        id=0,
        umo=umo,
        subject_id=0,
        title=title,
        status=STATUS_WATCHING,
        cover=cover,
        total=total,
    )
    if subject is None:
        return fallback
    canonical = trusted or similarity(subject.display_name, title) >= MATCH_THRESHOLD
    if not canonical and similarity(subject.alt_name, title) < MATCH_THRESHOLD:
        return fallback
    item = subject_to_item(umo, subject)
    item.status = STATUS_WATCHING
    if not canonical:
        item.title = title
    if not item.cover:
        item.cover = cover
    if not item.total:
        item.total = total
    return item


def _sort_key(item: WatchItem) -> tuple[int, float, str]:
    """在追的排前面，其次按最近更新时间倒序。"""
    rank = {STATUS_WATCHING: 0, STATUS_PLANNED: 1, STATUS_FINISHED: 2, STATUS_DROPPED: 3}
    return (rank.get(item.status, 4), -item.updated_at, item.title)


def _watchlist_plain(items: Sequence[WatchItem], airing: dict[int, str]) -> str:
    lines = [f"我的追番（共 {len(items)} 部）："]
    for item in items:
        bits = [item.title, WATCH_STATUS_CN.get(item.status, item.status), item.progress_label]
        label = airing.get(item.subject_id, "")
        if label:
            bits.append(label)
        lines.append("· " + " · ".join(bit for bit in bits if bit))
    return "\n".join(lines)


def subject_to_item(umo: str, subject: Subject) -> WatchItem:
    """把 Bangumi 条目转成追番记录 —— 导入/LLM 工具复用。"""
    return WatchItem(
        id=0,
        umo=umo,
        subject_id=subject.id,
        title=subject.display_name,
        total=subject.total_episodes or subject.eps,
        score=subject.score,
        cover=subject.image,
        weekday=subject.air_weekday,
    )


__all__ = [
    "MATCH_THRESHOLD",
    "NUMBERING_SLACK",
    "STATUS_DROPPED",
    "STATUS_FINISHED",
    "STATUS_PLANNED",
    "STATUS_WATCHING",
    "WatchlistService",
    "backfill_progress",
    "ensure_watch",
    "subject_to_item",
]
