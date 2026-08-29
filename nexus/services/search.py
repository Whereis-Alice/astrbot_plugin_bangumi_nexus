"""查番/日历相关的服务。

这一层负责「把关键词变成一张卡」：查 Bangumi、跨源补全、日文简介翻译、
版式选择，最后交出 「Reply」。上游 「astrbot_plugin_bangumi」 的
「/bgm」 「/calendar」 「/today」 「/放送时间」 与
「astrbot_plugin_anime_gacha」 的 「/查番」 「/萌娘百科」 都汇进这里。
"""

from __future__ import annotations

from collections.abc import Sequence

from ..constants import WEEKDAY_CN
from ..models import CalendarDay, MatchResult, Subject
from ..render import (
    build_calendar_card,
    build_episode_card,
    build_recommend_card,
    build_search_card,
    build_season_card,
    build_subject_card,
    build_today_card,
    clip,
    flatten,
)
from ..sources.bangumi import TYPE_ANIME, TYPE_BOOK, is_movie
from ..titles import season_code, season_label
from .base import (
    Deps,
    Reply,
    cover_map,
    cover_uri,
    llm_text,
    looks_japanese,
    make_card,
    numeric,
    style_for,
    template_for,
)

TRANSLATE_PROMPT = "把下面的日文动画简介翻译成简体中文，只输出译文，不要解释：\n\n"
SEASON_TYPES = {"tv", "web"}


class SearchService:
    """条目检索与日历卡。"""

    def __init__(self, deps: Deps) -> None:
        self._deps = deps

    # ------------------------------------------------------------------
    # 条目解析
    # ------------------------------------------------------------------
    async def resolve(self, query: str, *, subject_type: int | None = TYPE_ANIME) -> Subject | None:
        """把「关键词或条目 ID」统一解析成一个条目。

        纯数字直接当 ID 走详情接口 —— 这是上游 「/bgm 302286」 的用法；
        否则搜一次取第一条。
        """
        query = query.strip()
        if not query:
            return None
        sid = numeric(query)
        if sid:
            return await self._deps.hub.bangumi.subject(sid)
        found = await self._deps.hub.bangumi.search(query, limit=1, subject_type=subject_type)
        return found[0] if found else None

    # ------------------------------------------------------------------
    # /bgm 与同族指令
    # ------------------------------------------------------------------
    async def search(
        self,
        umo: str,
        keyword: str,
        *,
        limit: int = 0,
        subject_type: int | None = TYPE_ANIME,
        movie_only: bool = False,
        tv_only: bool = False,
    ) -> Reply:
        """搜索并给出结果卡；只有一条命中时直接展开详情。"""
        deps = self._deps
        conf = deps.conf
        keyword = keyword.strip()
        if not keyword:
            return Reply.plain("要搜什么？例：/bgm 迷宫饭")

        sid = numeric(keyword)
        if sid:
            return await self.detail(umo, subject_id=sid)

        want = limit or conf.search_max_results
        want = max(1, min(want, 20))
        # 多要几条再过滤，免得剧场版/TV 过滤后只剩一两条
        raw = await deps.hub.bangumi.search(
            keyword, limit=want * 3 if (movie_only or tv_only) else want, subject_type=subject_type
        )
        subjects = list(raw)
        if movie_only:
            subjects = [item for item in subjects if is_movie(item)]
        elif tv_only:
            subjects = [item for item in subjects if not is_movie(item)]
        subjects = subjects[:want]

        if not subjects:
            return Reply.plain(f"没搜到「{keyword}」，换个说法或者试试原名。")
        if len(subjects) == 1:
            return await self.detail(umo, subject=subjects[0])

        template = await template_for(deps, umo)
        if template == "3":
            return Reply.plain(_search_plain(keyword, subjects))

        theme, _ = await style_for(deps, umo)
        covers = (
            {}
            if template == "2"
            else await cover_map(deps, ((item.id, item.image) for item in subjects))
        )
        html = build_search_card(theme, keyword, subjects, covers=covers, width=conf.card_width)
        return Reply(
            text=_search_plain(keyword, subjects),
            card=make_card(
                html,
                plain=_search_plain(keyword, subjects),
                title=keyword,
                eyebrow="SEARCH",
                subtitle=f"在 Bangumi 找到 {len(subjects)} 条结果",
                theme=theme,
                width=conf.card_width,
            ),
        )

    async def detail(
        self,
        umo: str,
        *,
        subject: Subject | None = None,
        subject_id: int = 0,
        query: str = "",
        include_moegirl: bool = False,
    ) -> Reply:
        """跨源聚合详情卡：评分、放送倒计时、制作组、声优、观看入口。"""
        deps = self._deps
        conf = deps.conf
        if subject is None and subject_id:
            subject = await deps.hub.bangumi.subject(subject_id)
        if subject is None and query:
            subject = await self.resolve(query)
        if subject is None:
            return Reply.plain("没找到这部作品，确认下名字或者直接给条目 ID。")

        match = (
            await deps.matcher.enrich(subject, include_moegirl=include_moegirl)
            if conf.enable_cross_match
            else MatchResult(subject=subject, confidence=1.0)
        )
        theme, _ = await style_for(deps, umo)
        cover = await cover_uri(deps, subject.image)
        summary = await self._summary(subject, umo)
        html = build_subject_card(
            theme,
            match,
            width=conf.card_width,
            cover=cover,
            next_air=deps.matcher.next_air_label(match),
            watch_links=deps.matcher.watch_links(match),
            summary_override=summary,
        )
        return Reply(
            text=_subject_plain(match, summary, deps.matcher.next_air_label(match)),
            card=make_card(
                html,
                plain=_subject_plain(match, summary, deps.matcher.next_air_label(match)),
                title=subject.display_name,
                eyebrow=subject.type_label,
                subtitle=subject.alt_name,
                chips=(subject.score_label, *subject.tags[:4]),
                theme=theme,
                width=conf.card_width,
            ),
        )

    async def _summary(self, subject: Subject, umo: str) -> str:
        """需要时把日文简介翻成中文。

        只在「开了开关 + 简介确实含假名」时才花一次模型调用，
        并且失败就静默用原文 —— 简介翻不了不该让整张卡失败。
        """
        conf = self._deps.conf
        text = subject.summary
        if not conf.translate_summary or not text or not looks_japanese(text):
            return text
        translated = await llm_text(
            self._deps,
            TRANSLATE_PROMPT + text,
            provider_id=conf.translate_provider_id,
            umo=umo,
            limit=600,
        )
        return translated or text

    # ------------------------------------------------------------------
    # 日历
    # ------------------------------------------------------------------
    async def calendar(self, umo: str, *, today_index: int = 0) -> Reply:
        """整周放送总览卡。"""
        deps = self._deps
        conf = deps.conf
        days = await deps.hub.bangumi.calendar()
        if not days:
            return Reply.plain("Bangumi 日历接口暂时没给数据，稍后再试。")
        theme, _ = await style_for(deps, umo)
        html = build_calendar_card(
            theme,
            days,
            width=max(conf.card_width, 1180),
            today=today_index,
            subtitle="数据来自 Bangumi 番组计划",
        )
        return Reply(
            text=_calendar_plain(days),
            card=make_card(
                html,
                plain=_calendar_plain(days),
                title="每日放送",
                eyebrow="CALENDAR",
                theme=theme,
                width=max(conf.card_width, 1180),
            ),
        )

    async def today(self, umo: str, *, weekday: int, compact: bool = False) -> Reply:
        """今日放送卡。「compact」 走精简列表，适合人多的群。"""
        deps = self._deps
        conf = deps.conf
        days = await deps.hub.bangumi.calendar()
        day = _pick_day(days, weekday)
        if day is None or not day.items:
            label = WEEKDAY_CN[(weekday - 1) % 7]
            return Reply.plain(f"{label}没有查到放送中的番。")

        items = sorted(day.items, key=lambda item: (-item.score, -item.doing))
        theme, _ = await style_for(deps, umo)
        limit = 8 if compact else 12
        covers = (
            {}
            if compact
            else await cover_map(deps, ((item.id, item.image) for item in items[:limit]))
        )
        trimmed = CalendarDay(weekday=day.weekday, label=day.label, items=tuple(items))
        html = build_today_card(theme, trimmed, width=conf.card_width, limit=limit, covers=covers)
        plain = _today_plain(trimmed, limit)
        return Reply(
            text=plain,
            card=make_card(
                html,
                plain=plain,
                title=f"{day.label}放送",
                eyebrow="TODAY",
                subtitle=f"共 {len(items)} 部",
                theme=theme,
                width=conf.card_width,
            ),
        )

    async def digest(self, umo: str, *, weekday: int) -> Reply:
        """每日播报卡：在今日放送的基础上套用推送侧的过滤与排序配置。

        「/today」 是人主动问，按热度排就够；每日播报是机器主动刷群，
        群管往往只想推评分或在看人数达标的番，所以单独吃 「push_*」 那组配置，
        而不是复用查询默认值。数据为空时返回空 「Reply」，由调度器决定跳过。
        """
        deps = self._deps
        conf = deps.conf
        days = await deps.hub.bangumi.calendar()
        day = _pick_day(days, weekday)
        if day is None or not day.items:
            return Reply()
        picked = [
            item
            for item in day.items
            if item.score >= conf.push_min_score and item.doing >= conf.push_min_doing
        ]
        if not picked:
            return Reply()
        ordered = _sort_subjects(picked, conf.push_sort_by, conf.push_sort_order)
        limit = max(1, conf.push_max_items)
        theme, _ = await style_for(deps, umo)
        covers = await cover_map(deps, ((item.id, item.image) for item in ordered[:limit]))
        trimmed = CalendarDay(weekday=day.weekday, label=day.label, items=tuple(ordered))
        html = build_today_card(theme, trimmed, width=conf.card_width, limit=limit, covers=covers)
        plain = _today_plain(trimmed, limit)
        return Reply(
            text=plain,
            card=make_card(
                html,
                plain=plain,
                title=f"{day.label}放送",
                eyebrow="DAILY",
                subtitle=f"共 {len(ordered)} 部",
                theme=theme,
                width=conf.card_width,
            ),
        )

    async def season(self, umo: str, code: str = "") -> Reply:
        """季度新番总表（長門番堂）：题材、制作组、首播时间一屏看完。"""
        deps = self._deps
        conf = deps.conf
        wanted = code.strip() or season_code()
        table = await deps.hub.yuc.season(wanted)
        if table is None or not table.entries:
            return Reply.plain(f"没抓到 {wanted} 的季度表，长门番堂可能改版或者暂时不可达。")
        theme, _ = await style_for(deps, umo)
        width = max(conf.card_width, 1180)
        html = build_season_card(theme, season_label(table.code), table.entries, width=width)
        plain = _season_plain(table.code, table.entries)
        return Reply(
            text=plain,
            card=make_card(
                html,
                plain=plain,
                title=f"{season_label(table.code)}新番",
                eyebrow="SEASON",
                subtitle=f"共 {table.total} 部 · 数据来自长门番堂",
                theme=theme,
                width=width,
            ),
        )

    # ------------------------------------------------------------------
    # 放送时间 / 在线观看 / 萌娘百科
    # ------------------------------------------------------------------
    async def air_time(self, umo: str, query: str) -> Reply:
        """下一集什么时候播 —— 带分集列表，时间按 Bot 本地时区换算。"""
        deps = self._deps
        conf = deps.conf
        subject = await self.resolve(query)
        if subject is None:
            return Reply.plain("没找到这部作品，换个名字试试。")
        match = await deps.matcher.enrich(subject)
        episodes = await deps.hub.bangumi.episodes(subject.id, limit=60)
        upcoming = await deps.hub.bangumi.next_episode(subject.id)
        theme, _ = await style_for(deps, umo)
        cover = await cover_uri(deps, subject.image)
        next_air = deps.matcher.next_air_label(match)
        html = build_episode_card(
            theme,
            subject,
            episodes,
            width=conf.card_width,
            cover=cover,
            next_air=next_air,
            highlight=upcoming.sort if upcoming else None,
        )
        plain = _episode_plain(subject, episodes, upcoming, next_air)
        return Reply(
            text=plain,
            card=make_card(
                html,
                plain=plain,
                title=subject.display_name,
                eyebrow="EPISODES",
                subtitle=next_air,
                theme=theme,
                width=conf.card_width,
            ),
        )

    async def watch_links(self, umo: str, query: str) -> Reply:
        """汇总正版平台、anime1、AGE 动漫与官网入口。"""
        deps = self._deps
        subject = await self.resolve(query)
        match = (
            await deps.matcher.enrich(subject, title=query)
            if subject is not None
            else await deps.matcher.by_title(query)
        )
        links = deps.matcher.watch_links(match)
        title = match.title or query
        if not links:
            return Reply.plain(f"没找到「{title}」的在线观看入口，可能还没上线或者各站都没收录。")
        lines = [f"{name} {url}" for name, url in links]
        return Reply.plain("「" + title + "」在线观看：\n" + "\n".join(lines))

    async def moegirl(self, umo: str, keyword: str) -> Reply:
        """萌娘百科词条摘要 —— 作品和角色都能查。"""
        deps = self._deps
        keyword = keyword.strip()
        if not keyword:
            return Reply.plain("要查什么词条？例：/萌娘百科 芙莉莲")
        hit = await deps.hub.moegirl.lookup(keyword)
        if hit is None:
            return Reply.plain(f"萌娘百科没有「{keyword}」这个词条。")
        body = hit.summary or "词条存在，但没抓到正文摘要。"
        return Reply.plain(f"【{hit.title}】\n{body}\n\n{hit.url}")

    async def recommend(self, umo: str) -> Reply:
        """AGE 动漫推荐位的热门更新。"""
        deps = self._deps
        conf = deps.conf
        items = await deps.hub.age.recommend(limit=12)
        if not items:
            return Reply.plain("AGE 动漫推荐位暂时抓不到内容。")
        theme, _ = await style_for(deps, umo)
        covers = await cover_map(deps, ((item.title, item.cover) for item in items))
        html = build_recommend_card(theme, items, width=conf.card_width, covers=covers)
        plain = "AGE 动漫推荐：\n" + "\n".join(
            f"· {item.title} {item.progress}".rstrip() for item in items
        )
        return Reply(
            text=plain,
            card=make_card(
                html,
                plain=plain,
                title="番剧推荐",
                eyebrow="RECOMMEND",
                subtitle="数据来自 AGE 动漫推荐位",
                theme=theme,
                width=conf.card_width,
            ),
        )

    # ------------------------------------------------------------------
    # 版式偏好
    # ------------------------------------------------------------------
    async def set_template(self, umo: str, value: str) -> Reply:
        """「/bgm模板 1|2|3」：1 详情卡 / 2 紧凑卡 / 3 纯文本。"""
        choice = value.strip()
        if choice not in {"1", "2", "3"}:
            current = await template_for(self._deps, umo)
            return Reply.plain(
                f"当前版式：{current}（1 详情卡 / 2 紧凑卡 / 3 纯文本）\n用法：/bgm模板 2"
            )
        await self._deps.store.set_pref(umo, "search_template", choice)
        names = {"1": "详情卡", "2": "紧凑卡", "3": "纯文本"}
        return Reply.plain(f"这个会话的搜索版式已切到 {choice} · {names[choice]}。")


# ---------------------------------------------------------------------------
# 纯文本兜底
# ---------------------------------------------------------------------------
def _sort_subjects(items: Sequence[Subject], key: str, order: str) -> list[Subject]:
    """按配置里的字段给条目排序。「time」 用放送星期，「name」 用显示名。"""
    keys = {
        "score": lambda item: (item.score, item.doing),
        "doing": lambda item: (item.doing, item.score),
        "time": lambda item: (item.air_weekday, item.score),
        "name": lambda item: item.display_name,
    }
    getter = keys.get(key, keys["score"])
    return sorted(items, key=getter, reverse=order != "asc")


def _pick_day(days: Sequence[CalendarDay], weekday: int) -> CalendarDay | None:
    for day in days:
        if day.weekday == weekday:
            return day
    return None


def _search_plain(keyword: str, subjects: Sequence[Subject]) -> str:
    lines = [f"「{keyword}」搜到 {len(subjects)} 条："]
    for index, item in enumerate(subjects, start=1):
        bits = [f"{index}. {item.display_name}"]
        if item.score:
            bits.append(f"评分 {item.score:g}")
        if item.air_date:
            bits.append(f"首播 {item.air_date}")
        bits.append(f"ID {item.id}")
        lines.append("  ".join(bits))
    lines.append("发送 /bgm <ID> 看详情。")
    return "\n".join(lines)


def _subject_plain(match: MatchResult, summary: str, next_air: str) -> str:
    subject = match.subject
    if subject is None:
        return match.title or "没有可用信息"
    lines = [subject.display_name]
    if subject.alt_name:
        lines.append(subject.alt_name)
    meta = [subject.type_label]
    if subject.score:
        meta.append(f"评分 {subject.score:g}")
    if subject.air_date:
        meta.append(f"首播 {subject.air_date}")
    if subject.eps:
        meta.append(f"话数 {subject.eps}")
    lines.append(" · ".join(meta))
    if next_air:
        lines.append(f"下一集：{next_air}")
    if subject.tags:
        lines.append("标签：" + " ".join(subject.tags[:6]))
    if summary:
        lines.append("")
        lines.append(clip(flatten(summary), 220))
    if subject.url:
        lines.append(subject.url)
    return "\n".join(lines)


def _calendar_plain(days: Sequence[CalendarDay]) -> str:
    lines = ["每日放送："]
    for day in days:
        names = "、".join(item.display_name for item in day.items[:6])
        more = f" 等 {len(day.items)} 部" if len(day.items) > 6 else ""
        lines.append(f"{day.label}：{names}{more}")
    return "\n".join(lines)


def _today_plain(day: CalendarDay, limit: int) -> str:
    lines = [f"{day.label}放送（共 {len(day.items)} 部）："]
    for item in day.items[:limit]:
        score = f" {item.score:g}分" if item.score else ""
        lines.append(f"· {item.display_name}{score}")
    if len(day.items) > limit:
        lines.append(f"…还有 {len(day.items) - limit} 部")
    return "\n".join(lines)


def _season_plain(code: str, entries: Sequence) -> str:
    lines = [f"{season_label(code)}新番（共 {len(entries)} 部）："]
    for entry in entries[:30]:
        bits = [entry.display_name]
        if entry.broadcast:
            bits.append(entry.broadcast)
        if entry.studio:
            bits.append(entry.studio)
        lines.append("· " + " · ".join(bits))
    if len(entries) > 30:
        lines.append(f"…还有 {len(entries) - 30} 部")
    return "\n".join(lines)


def _episode_plain(subject: Subject, episodes: Sequence, upcoming, next_air: str) -> str:
    lines = [f"{subject.display_name} 分集"]
    if next_air:
        lines.append(f"下一集：{next_air}")
    if upcoming is not None:
        lines.append(f"即将播出：第 {upcoming.sort:g} 集 {upcoming.display_name}".rstrip())
    aired = [ep for ep in episodes if ep.airdate]
    for ep in aired[-8:]:
        lines.append(f"· 第 {ep.sort:g} 集 {ep.airdate} {ep.display_name}".rstrip())
    if not aired:
        lines.append("暂无分集信息。")
    return "\n".join(lines)


__all__ = ["SEASON_TYPES", "TYPE_ANIME", "TYPE_BOOK", "SearchService"]
