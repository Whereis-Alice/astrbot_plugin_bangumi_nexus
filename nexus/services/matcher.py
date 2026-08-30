"""跨源匹配：把同一部番在八个站点上的身份拼成一张卡。

这是整个插件的核心增量。单独看每个上游插件，用户拿到的是碎片：Bangumi 给评分、
yuc.wiki 给制作组、anime1 给在线观看、Mikan 给字幕组资源，彼此毫无关联。
这里以 「bangumi-data」 为 join key 把它们连起来：

    Bangumi 条目 ID ──► bangumi-data 条目 ──► mikan_id / 正版播放站点
                                      └────► 日文原名 + 全部译名
                                                   └──► anime1 / yuc / AGE / 萌娘百科

匹配策略遵循「宁缺毋滥」：ID 命中 > 归一化标题精确命中 > 模糊相似度且高阈值。
匹配错一部番，用户会收到完全无关的更新通知，比匹配不到糟糕得多。

Copyright (C) 2026 Whereis-Alice and AstrBot Plugin Authors.
Licensed under the GNU Affero General Public License v3.0 or later.
"""

from __future__ import annotations

import asyncio

from ..activity import ActivityLog
from ..models import MatchResult, Subject
from ..sources.bangumi_data import BangumiDataSource
from ..sources.hub import SourceHub
from ..sources.rss import mikan_bangumi_feed, mikan_search_feed
from ..titles import Broadcast, humanize_delta, parse_broadcast


class Matcher:
    """把一个标题或一个 Bangumi 条目扩展成 「MatchResult」。"""

    def __init__(
        self,
        hub: SourceHub,
        *,
        mikan_base: str = "https://mikanani.me",
        activity: ActivityLog | None = None,
    ) -> None:
        self._hub = hub
        self._mikan_base = mikan_base
        self._activity = activity

    def set_mikan_base(self, base: str) -> None:
        self._mikan_base = base or "https://mikanani.me"

    async def enrich(
        self,
        subject: Subject | None,
        *,
        title: str = "",
        include_moegirl: bool = False,
        include_age: bool = True,
    ) -> MatchResult:
        """给一个条目补齐其它源的信息。所有外部访问并发进行。"""

        query = title or (subject.display_name if subject else "")
        names = _candidate_names(subject, query)
        data_item, confidence = await self._resolve_data_item(subject, names)
        aliases = list(names)
        if data_item is not None:
            aliases.extend(data_item.titles)
            aliases.append(data_item.title)

        tasks = {
            "anime1": self._hub.anime1.match(*aliases),
            "yuc": self._hub.yuc.find(query or (aliases[0] if aliases else "")),
        }
        if include_age:
            tasks["age"] = self._hub.age.match(*aliases)
        if include_moegirl:
            tasks["moegirl"] = self._hub.moegirl.lookup(query)

        keys = list(tasks)
        outcomes = await asyncio.gather(*(tasks[key] for key in keys), return_exceptions=True)
        resolved: dict[str, object] = {}
        notes: list[str] = []
        for key, outcome in zip(keys, outcomes, strict=False):
            if isinstance(outcome, Exception):
                notes.append(f"{key} 查询失败")
                self._log(f"{key} 匹配失败：{outcome}", "warn")
                continue
            resolved[key] = outcome

        season_entry = None
        season_pair = resolved.get("yuc")
        if isinstance(season_pair, tuple):
            season_entry = season_pair[0]

        mikan_rss = ""
        mikan_id = str(data_item.mikan_id or "") if data_item is not None else ""
        if mikan_id:
            mikan_rss = mikan_bangumi_feed(self._mikan_base, mikan_id)
        elif query:
            mikan_rss = mikan_search_feed(self._mikan_base, query)
            notes.append("Mikan 用的是关键词搜索源，可能混入同名作品")

        return MatchResult(
            subject=subject,
            data_item=data_item,
            anime1=resolved.get("anime1"),  # type: ignore[arg-type]
            season=season_entry,
            age=resolved.get("age"),  # type: ignore[arg-type]
            moegirl=resolved.get("moegirl"),  # type: ignore[arg-type]
            mikan_id=mikan_id,
            mikan_rss=mikan_rss,
            confidence=confidence,
            notes=tuple(notes),
        )

    async def _resolve_data_item(
        self, subject: Subject | None, names: tuple[str, ...]
    ) -> tuple[object | None, float]:
        """先用 Bangumi ID 反查（最可靠），再退回标题匹配。"""

        data: BangumiDataSource = self._hub.bangumi_data
        if subject is not None and subject.id:
            try:
                hit = await data.by_bangumi_id(subject.id)
            except Exception as error:  # noqa: BLE001 - 跨源匹配是增强，失败只降级
                self._log(f"bangumi-data ID 反查失败：{error}", "warn")
                hit = None
            if hit is not None:
                return hit, 1.0
        for name in names:
            if not name:
                continue
            try:
                hit, score = await data.by_title(name)
            except Exception as error:  # noqa: BLE001 - 同上，标题匹配失败不影响主结果
                self._log(f"bangumi-data 标题匹配失败：{error}", "warn")
                return None, 0.0
            if hit is not None:
                return hit, score
        return None, 0.0

    async def by_title(self, title: str, *, include_moegirl: bool = False) -> MatchResult:
        """只有标题时的入口：先去 Bangumi 搜一条，再走 enrich。"""

        subjects = await self._hub.bangumi.search(title, limit=1)
        return await self.enrich(
            subjects[0] if subjects else None, title=title, include_moegirl=include_moegirl
        )

    # -- 派生展示信息 -------------------------------------------------------

    def broadcast_of(self, result: MatchResult) -> Broadcast | None:
        if result.data_item is None:
            return None
        return parse_broadcast(result.data_item.broadcast)

    def next_air_label(self, result: MatchResult) -> str:
        """「周日 23:30 · 2 天后」 这样的一行字。"""

        broadcast = self.broadcast_of(result)
        if broadcast is None:
            if result.season and result.season.broadcast:
                return result.season.broadcast
            if result.subject and result.subject.weekday_label:
                return f"{result.subject.weekday_label} 放送"
            return ""
        delta = humanize_delta(broadcast.next_after())
        return f"{broadcast.label()}" + (f" · {delta}" if delta else "")

    def watch_links(self, result: MatchResult) -> tuple[tuple[str, str], ...]:
        """所有「能看」的入口，anime1 排在正版站点之后。"""

        links = list(self._hub.bangumi_data.watch_links(result.data_item))
        if result.anime1 is not None:
            links.append(("anime1", result.anime1.watch_url))
        if result.age is not None and result.age.url:
            links.append(("AGE 动漫", result.age.url))
        if result.season is not None and result.season.official_site:
            links.append(("官网", result.season.official_site))
        seen: set[str] = set()
        unique: list[tuple[str, str]] = []
        for label, url in links:
            if url and url not in seen:
                seen.add(url)
                unique.append((label, url))
        return tuple(unique)

    def _log(self, message: str, level: str = "info") -> None:
        if self._activity is not None:
            self._activity.add("matcher", message, level=level)


def _candidate_names(subject: Subject | None, title: str) -> tuple[str, ...]:
    names: list[str] = []
    if subject is not None:
        names.extend([subject.name_cn, subject.name])
    if title:
        names.append(title)
    return tuple(dict.fromkeys(name for name in names if name))
