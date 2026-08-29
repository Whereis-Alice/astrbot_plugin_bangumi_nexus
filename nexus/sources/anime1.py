"""anime1.me 在线观看索引适配器。

「animelist.json」 是一个数组的数组，形如
「[1919, "标题", "連載中(09)", "2026", "夏", ""]」，全部是繁体。上游插件用同步
「requests」 抓、把结果写在插件目录里，这里改成 async + 数据目录，并额外做繁简
归一化的别名索引，让简体标题也能命中。

Copyright (C) 2026 Whereis-Alice and AstrBot Plugin Authors.
Licensed under the GNU Affero General Public License v3.0 or later.
"""

from __future__ import annotations

import re
import time
from typing import Any

from ..constants import ANIME1_LIST_URL, ANIME1_WATCH_URL
from ..http import FetchError, HttpClient
from ..models import Anime1Entry
from ..titles import alias_keys, best_match

_TAG_RE = re.compile(r"<[^>]+>")
_EPISODE_RE = re.compile(r"\((\d+)\)")


def _text(value: Any) -> str:
    """列表里偶尔混着 HTML 片段（比如加了角标的标题），统一剥标签。"""

    return _TAG_RE.sub("", str(value or "")).strip()


def parse_rows(raw: Any) -> tuple[Anime1Entry, ...]:
    entries: list[Anime1Entry] = []
    for row in raw or ():
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        try:
            cat = int(str(row[0]).strip())
        except (TypeError, ValueError):
            continue
        title = _text(row[1])
        if not title:
            continue
        entries.append(
            Anime1Entry(
                cat=cat,
                title=title,
                status=_text(row[2]) if len(row) > 2 else "",
                year=_text(row[3]) if len(row) > 3 else "",
                season=_text(row[4]) if len(row) > 4 else "",
                note=_text(row[5]) if len(row) > 5 else "",
            )
        )
    return tuple(entries)


def episode_number(status: str) -> int:
    """从 「連載中(09)」 里抠出 9；抠不到返回 0。"""

    match = _EPISODE_RE.search(status or "")
    return int(match.group(1)) if match else 0


class Anime1Source:
    """anime1.me 番剧列表。整表只有几百 KB，一次拉全再本地检索最省。"""

    key = "anime1"

    def __init__(self, http: HttpClient) -> None:
        self._http = http
        self._entries: tuple[Anime1Entry, ...] = ()
        self._index: dict[str, Anime1Entry] = {}
        self.fetched_at: float = 0.0

    async def refresh(self, *, force: bool = False) -> tuple[Anime1Entry, ...]:
        if self._entries and not force:
            return self._entries
        raw = await self._http.fetch_json(
            ANIME1_LIST_URL, cache_key="anime1:list", ttl=0 if force else 3600
        )
        entries = parse_rows(raw)
        if entries:
            self._entries = entries
            self.fetched_at = time.time()
            self._index = {}
            for entry in entries:
                for key in alias_keys(entry.title):
                    self._index.setdefault(key, entry)
        return self._entries

    async def entries(self) -> tuple[Anime1Entry, ...]:
        if not self._entries:
            try:
                await self.refresh()
            except FetchError:
                return ()
        return self._entries

    async def search(self, keyword: str, *, limit: int = 8) -> list[Anime1Entry]:
        entries = await self.entries()
        token = keyword.strip()
        if not token:
            return []
        for key in alias_keys(token):
            hit = self._index.get(key)
            if hit is not None:
                return [hit]
        scored: list[tuple[float, Anime1Entry]] = []
        lowered = token.lower()
        for entry in entries:
            if lowered in entry.title.lower():
                scored.append((1.0, entry))
        if scored:
            return [entry for _, entry in scored[:limit]]
        hit, _score = best_match(token, entries, key=lambda item: item.title, threshold=0.6)
        return [hit] if hit else []

    async def match(self, *titles: str) -> Anime1Entry | None:
        """给跨源匹配用：任意一个别名命中就算命中。"""

        await self.entries()
        for title in titles:
            if not title:
                continue
            for key in alias_keys(title):
                hit = self._index.get(key)
                if hit is not None:
                    return hit
        for title in titles:
            if not title:
                continue
            hit, _ = best_match(title, self._entries, key=lambda item: item.title, threshold=0.74)
            if hit:
                return hit
        return None

    async def season(self, year: str, season: str, *, limit: int = 0) -> list[Anime1Entry]:
        entries = await self.entries()
        result = [
            entry
            for entry in entries
            if (not year or entry.year == str(year)) and (not season or entry.season == season)
        ]
        result.sort(key=lambda entry: (-episode_number(entry.status), entry.title))
        return result[:limit] if limit else result

    async def latest(self, *, limit: int = 12) -> list[Anime1Entry]:
        """连载中的、集数最多的排前面 —— 近似「最近更新」。"""

        entries = await self.entries()
        airing = [entry for entry in entries if "連載" in entry.status or "连载" in entry.status]
        airing.sort(key=lambda entry: (-entry.cat, entry.title))
        return airing[:limit]

    async def watch_url(self, cat: int) -> str:
        """「?cat=」 会 302 到真实播放页，这里只取 Location，不下载正文。"""

        url = ANIME1_WATCH_URL.format(cat=int(cat))
        resolved = await self._http.resolve_redirect(url)
        return resolved or url

    def stats(self) -> dict[str, Any]:
        return {"entries": len(self._entries), "fetched_at": self.fetched_at}
