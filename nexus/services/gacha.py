"""抽番。

上游 「astrbot_plugin_anime_gacha」 只从一个季度表里随机取一条。这里做了三点改进：

1. **池子可退化** —— 默认走 Bangumi 每日放送，拉不到时自动改用长门番堂，不会直接报错；
2. **题材过滤** —— 「/抽番 恋爱」 会在题材、类型、标题里找关键词，找不到时列出可选题材；
3. **不重复** —— 每个会话记住最近抽过的几部，短时间内不会连着抽到同一部。

抽完还会走一次跨源匹配，于是卡片上能直接给出「在哪看」。

Copyright (C) 2026 Whereis-Alice and AstrBot Plugin Authors.
Licensed under the GNU Affero General Public License v3.0 or later.
"""

from __future__ import annotations

import random
from collections import deque
from collections.abc import Sequence

from ..models import MatchResult, SeasonEntry, Subject
from ..render import build_gacha_card
from ..titles import season_code, season_label
from .base import Deps, Reply, cover_uri, make_card, style_for

RECENT_MEMORY = 8


class GachaService:
    """「/抽番」 的实现。"""

    def __init__(self, deps: Deps) -> None:
        self._deps = deps
        self._recent: dict[str, deque[str]] = {}
        self._draws = 0

    async def draw(self, umo: str, genre: str = "") -> Reply:
        """从当季番剧里随机抽一部。"""

        deps = self._deps
        conf = deps.conf
        genre = genre.strip()
        entries, subjects, pool_label = await self._pool()
        if not entries and not subjects:
            return Reply.plain("现在两个数据源都拿不到当季番剧，过一会儿再试试。")

        if genre:
            filtered_entries = [entry for entry in entries if _entry_matches(entry, genre)]
            filtered_subjects = [item for item in subjects if _subject_matches(item, genre)]
            if not filtered_entries and not filtered_subjects:
                # 池子走 Bangumi 时 「entries」 是空的，题材要从条目标签里数，
                # 否则提示会变成一句「可选题材：」后面什么都没有。
                options = _genres(entries) or _tags(subjects)
                hint = "、".join(options[:24]) if options else "换个更常见的词试试"
                return Reply.plain(f"这一季没找到「{genre}」题材的番。可选题材：{hint}")
            entries, subjects = filtered_entries, filtered_subjects

        pool_size = len(entries) or len(subjects)
        picked_entry = self._pick(umo, entries, key=lambda item: item.display_name)
        picked_subject = None
        if picked_entry is None:
            picked_subject = self._pick(umo, subjects, key=lambda item: item.display_name)
        if picked_entry is None and picked_subject is None:
            return Reply.plain("池子被抽空了（都在最近抽过），过一会儿再试。")

        title = (
            picked_entry.display_name if picked_entry is not None else picked_subject.display_name
        )
        match = await self._enrich(title, picked_entry, picked_subject)
        theme, _ = await style_for(deps, umo)
        cover_source = (picked_entry.cover if picked_entry is not None else "") or (
            match.subject.image if match.subject is not None else ""
        )
        cover = await cover_uri(deps, cover_source)
        reason = f"{pool_label}·随机抽取" + (f"｜题材 {genre}" if genre else "")
        html = build_gacha_card(
            theme,
            match,
            width=min(conf.card_width, 880),
            cover=cover,
            reason=reason,
            pool_size=pool_size,
            watch_links=deps.matcher.watch_links(match),
        )
        self._draws += 1
        deps.activity.info("gacha", f"抽到 {title}")
        plain = _plain(match, reason, pool_size)
        return Reply(
            text=plain,
            card=make_card(
                html,
                plain=plain,
                title=title,
                eyebrow="GACHA",
                subtitle=reason,
                theme=theme,
                width=min(conf.card_width, 880),
            ),
        )

    # ------------------------------------------------------------------
    # 池子
    # ------------------------------------------------------------------
    async def _pool(self) -> tuple[list[SeasonEntry], list[Subject], str]:
        """按配置取当季池子，「auto」 时 Bangumi 优先、长门番堂兜底。

        为什么是 Bangumi 优先：它的每日放送是官方 JSON 接口，稳定、带评分与在看
        人数，抽到的番能直接拿去做跨源匹配。长门番堂胜在带制作组 / 声优 / 题材，
        但它需要放宽 TLS 才连得上，不少机房会直接超时 —— 把它放在第一位等于
        让每次 「/抽番」 都先赌一次网络。所以它退到兜底位，想独占仍可显式选 「yuc」。
        """

        deps = self._deps
        preferred = deps.conf.gacha_source
        label = season_label(season_code())
        if preferred in {"auto", "bangumi"}:
            subjects = await self._bangumi_pool()
            if subjects:
                return [], subjects, f"{label}·Bangumi 放送表"
            if preferred == "bangumi":
                return [], [], label
        try:
            table = await deps.hub.yuc.season()
        except Exception as error:  # noqa: BLE001 - 上游任何异常都只该降级，不该让 /抽番 崩掉
            deps.activity.warn("gacha", f"长门番堂拉取失败：{error}")
            return [], [], label
        if table is not None and table.total:
            return list(table.entries), [], f"{label}·长门番堂"
        return [], [], label

    async def _bangumi_pool(self) -> list[Subject]:
        """把 Bangumi 整周放送表拉平成一份去重后的条目列表。

        同一部番会出现在多天（重播 / 多平台），按条目 ID 去重，否则热门番
        在池子里占的份额会凭空翻倍。
        """

        deps = self._deps
        try:
            days = await deps.hub.bangumi.calendar()
        except Exception as error:  # noqa: BLE001 - 网络波动只该让池子为空，交给上层兜底
            deps.activity.warn("gacha", f"Bangumi 日历拉取失败：{error}")
            return []
        subjects: list[Subject] = []
        seen: set[int] = set()
        for day in days:
            for item in day.items:
                if item.id not in seen:
                    seen.add(item.id)
                    subjects.append(item)
        return subjects

    def _pick(self, umo: str, items: Sequence, *, key) -> object | None:
        """随机取一个，尽量避开这个会话最近抽过的。"""

        if not items:
            return None
        memory = self._recent.setdefault(umo, deque(maxlen=RECENT_MEMORY))
        candidates = [item for item in items if key(item) not in memory]
        chosen = random.choice(candidates or list(items))
        memory.append(key(chosen))
        return chosen

    async def _enrich(
        self, title: str, entry: SeasonEntry | None, subject: Subject | None
    ) -> MatchResult:
        """把抽到的东西补全成一张能看的卡。"""

        deps = self._deps
        if subject is not None:
            match = await deps.matcher.enrich(subject, title=title)
        else:
            match = await deps.matcher.by_title(title)
        if entry is not None and match.season is None:
            # 池子本来就是从长门番堂抽的，别再回头去搜一次
            match = MatchResult(
                subject=match.subject,
                data_item=match.data_item,
                anime1=match.anime1,
                season=entry,
                age=match.age,
                moegirl=match.moegirl,
                mikan_rss=match.mikan_rss,
                confidence=match.confidence,
                notes=match.notes,
            )
        return match

    def stats(self) -> dict[str, int]:
        return {"draws": self._draws, "sessions": len(self._recent)}


def _entry_matches(entry: SeasonEntry, genre: str) -> bool:
    needle = genre.lower()
    haystack = [entry.category, entry.title_cn, entry.title_jp, *entry.genres]
    return any(needle in str(text).lower() for text in haystack if text)


def _subject_matches(subject: Subject, genre: str) -> bool:
    needle = genre.lower()
    haystack = [subject.name, subject.name_cn, *subject.tags]
    return any(needle in str(text).lower() for text in haystack if text)


def _tags(subjects: Sequence[Subject]) -> tuple[str, ...]:
    """Bangumi 池子的「题材」只能从条目标签里统计，取出现次数最多的那些。"""

    counter: dict[str, int] = {}
    for subject in subjects:
        for tag in subject.tags:
            counter[tag] = counter.get(tag, 0) + 1
    return tuple(name for name, _ in sorted(counter.items(), key=lambda pair: -pair[1]))


def _genres(entries: Sequence[SeasonEntry]) -> tuple[str, ...]:
    counter: dict[str, int] = {}
    for entry in entries:
        for genre in entry.genres:
            counter[genre] = counter.get(genre, 0) + 1
    return tuple(name for name, _ in sorted(counter.items(), key=lambda pair: -pair[1]))


def _plain(match: MatchResult, reason: str, pool_size: int) -> str:
    lines = [f"抽到：{match.title}", reason]
    subject = match.subject
    if subject is not None:
        meta = [subject.type_label, subject.score_label, subject.weekday_label]
        lines.append(" · ".join(part for part in meta if part))
        if subject.url:
            lines.append(subject.url)
    season = match.season
    if season is not None:
        if season.studio:
            lines.append(f"动画制作：{season.studio}")
        if season.broadcast:
            lines.append(f"首播：{season.broadcast}")
    if pool_size:
        lines.append(f"（池子里有 {pool_size} 部）")
    return "\n".join(line for line in lines if str(line).strip())


__all__ = ["GachaService"]
