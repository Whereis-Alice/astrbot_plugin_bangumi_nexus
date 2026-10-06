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
import time
from datetime import datetime

from ..activity import ActivityLog
from ..links import Link
from ..models import DataItem, MatchResult, Subject
from ..sources.bangumi_data import BangumiDataSource
from ..sources.hub import SourceHub
from ..sources.rss import mikan_bangumi_feed, mikan_search_feed
from ..titles import (
    JST,
    Broadcast,
    humanize_delta,
    parse_broadcast,
    parse_datetime,
    season_number,
    similarity,
)

LINK_TIMEOUT = 5.0
LINK_CACHE_LIMIT = 128


def _same_season(names: tuple[str, ...], candidate: str) -> bool:
    """未标季数的正篇按第一季比较，不能把第二季入口挂在第一季下面。"""
    seasons = {season_number(name) for name in names} - {None, 0}
    return (season_number(candidate) or 1) in (seasons or {1})


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
        self._link_cache: dict[tuple[int, str], tuple[float, tuple[Link, ...]]] = {}
        self._notice_subjects: dict[tuple[int, str], tuple[float, Subject | None]] = {}

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

        for key in ("anime1", "age"):
            entry = resolved.get(key)
            if entry is not None and not _same_season(names, entry.title):
                resolved.pop(key)

        season_entry = None
        season_pair = resolved.get("yuc")
        if isinstance(season_pair, tuple):
            season_entry = season_pair[0]
            if season_entry and not _same_season(names, season_entry.display_name):
                season_entry = None

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
    ) -> tuple[DataItem | None, float]:
        """先用 Bangumi ID 反查（最可靠），再退回标题匹配。"""

        data: BangumiDataSource = self._hub.bangumi_data
        if subject is not None and subject.id:
            try:
                # 旧番不在当前季度预热范围内，先加载首播月，避免标题回退串到续作。
                if subject.air_date and len(subject.air_date) >= 7:
                    year, month = int(subject.air_date[:4]), int(subject.air_date[5:7])
                    if 1 <= month <= 12:
                        await data.month(year, month)
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
                if (
                    subject
                    and subject.id
                    and hit.bangumi_id
                    and str(hit.bangumi_id) != str(subject.id)
                ):
                    continue
                if not _same_season(names, hit.title):
                    continue
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

        end = parse_datetime(result.data_item.end) if result.data_item else None
        if end and end.astimezone(JST).date() < datetime.now(JST).date():
            return "已完结"
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

    def all_links(self, result: MatchResult) -> tuple[Link, ...]:
        """条目信息链接与观看入口一起交给消息文本，图片模板不再承担链接展示。"""
        links: list[Link] = []
        if result.subject and result.subject.id:
            links.append(("Bangumi 条目", f"https://bgm.tv/subject/{result.subject.id}"))
        links.extend(self.watch_links(result))
        official = result.data_item.official_site if result.data_item else ""
        if not official and result.subject:
            official = result.subject.infobox.get("官方网站", "")
        if official:
            links.append(("官网", official))
        if result.moegirl:
            links.append(("萌娘百科", result.moegirl.url))
        return tuple(links)

    async def notification_subject(self, subject_id: int, title: str) -> Subject | None:
        """通知资料与链接共用一次身份解析；未知季数不借用续作的资料。"""
        key = (subject_id, title)
        cached = self._notice_subjects.get(key)
        if cached and time.monotonic() < cached[0]:
            return cached[1]
        subject = None
        try:
            if subject_id:
                subject = await asyncio.wait_for(self._hub.bangumi.subject(subject_id), timeout=2)
            else:
                hits = await asyncio.wait_for(self._hub.bangumi.search(title, limit=5), timeout=2)
                candidates = [
                    s
                    for s in hits
                    if _same_season((title,), s.display_name)
                    and similarity(title, s.display_name) >= 0.82
                ]
                subject = max(
                    candidates, key=lambda s: similarity(title, s.display_name), default=None
                )
        except Exception:  # noqa: BLE001 - 已有事件仍然要发送
            pass
        if len(self._notice_subjects) >= LINK_CACHE_LIMIT:
            self._notice_subjects.pop(next(iter(self._notice_subjects)))
        self._notice_subjects[key] = (time.monotonic() + (300 if subject else 30), subject)
        return subject

    async def notification_links(self, subject_id: int, title: str) -> tuple[Link, ...]:
        """通知补入口有时限；慢站不拖住推送，多个会话复用短期缓存。"""
        key = (subject_id, title)
        cached = self._link_cache.get(key)
        if cached and time.monotonic() < cached[0]:
            return cached[1]
        subject = await self.notification_subject(subject_id, title)
        if subject is None:
            return (("Bangumi 条目", f"https://bgm.tv/subject/{subject_id}"),) if subject_id else ()
        names = _candidate_names(subject, subject.display_name)
        calls = {
            "data": self._resolve_data_item(subject, names),
            "anime1": self._hub.anime1.match(*names),
            "age": self._hub.age.match(*names),
        }
        tasks = {key: asyncio.create_task(call) for key, call in calls.items()}
        try:
            await asyncio.wait(tasks.values(), timeout=LINK_TIMEOUT)
        finally:
            for task in tasks.values():
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks.values(), return_exceptions=True)
        values = {
            key: task.result()
            for key, task in tasks.items()
            if not task.cancelled() and task.exception() is None
        }
        match = MatchResult(subject=subject)
        if isinstance(values.get("data"), tuple):
            match.data_item = values["data"][0]
        for name in ("anime1", "age"):
            hit = values.get(name)
            if hit is not None and _same_season(names, hit.title):
                setattr(match, name, hit)
        links = self.all_links(match)
        if len(self._link_cache) >= LINK_CACHE_LIMIT:
            self._link_cache.pop(next(iter(self._link_cache)))
        self._link_cache[key] = (time.monotonic() + 300, links)
        return links

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
