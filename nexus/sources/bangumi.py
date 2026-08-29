"""Bangumi 番组计划（bgm.tv）适配器。

覆盖每日放送、条目详情、搜索与分集列表。搜索优先走 v0 的 POST 接口（支持按类型
过滤、返回字段更全），失败时自动退回旧版 GET 接口 —— 旧接口偶尔比新接口更命中，
两条路都留着比只留一条稳。

Copyright (C) 2026 Whereis-Alice and AstrBot Plugin Authors.
Licensed under the GNU Affero General Public License v3.0 or later.
"""

from __future__ import annotations

import re
from typing import Any

from ..constants import BANGUMI_API, BANGUMI_SITE
from ..http import FetchError, HttpClient
from ..models import CalendarDay, Episode, Subject

#: Bangumi 的 type 常量。
TYPE_BOOK, TYPE_ANIME, TYPE_MUSIC, TYPE_GAME, TYPE_REAL = 1, 2, 3, 4, 6

TYPE_ALIASES: dict[str, int] = {
    "动画": TYPE_ANIME,
    "番剧": TYPE_ANIME,
    "动漫": TYPE_ANIME,
    "anime": TYPE_ANIME,
    "书籍": TYPE_BOOK,
    "漫画": TYPE_BOOK,
    "小说": TYPE_BOOK,
    "音乐": TYPE_MUSIC,
    "游戏": TYPE_GAME,
    "三次元": TYPE_REAL,
    "真人": TYPE_REAL,
}

_TAG_BLOCKLIST = frozenset(
    {"tv", "日本", "動畫", "动画", "アニメ", "2026", "2025", "2027", "漫画改", "轻小说改"}
)


def _clean_summary(text: str, limit: int = 320) -> str:
    """简介里常见连续空行与首尾空白，压平后截断。"""

    body = re.sub(r"\r\n?", "\n", str(text or "")).strip()
    body = re.sub(r"\n{2,}", "\n", body)
    if len(body) > limit:
        body = body[: limit - 1].rstrip() + "…"
    return body


def select_tags(raw: Any, limit: int = 6) -> tuple[str, ...]:
    """挑最有信息量的标签：按 count 降序，剔掉「TV」「日本」这类废话。"""

    entries: list[tuple[str, int]] = []
    for item in raw or ():
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            count = int(item.get("count") or 0)
        else:
            name, count = str(item).strip(), 0
        if name and name.lower() not in _TAG_BLOCKLIST:
            entries.append((name, count))
    entries.sort(key=lambda pair: (-pair[1], pair[0]))
    return tuple(name for name, _ in entries[:limit])


def _infobox(raw: Any) -> dict[str, str]:
    """把 infobox 拍平成 「键 -> 文本」，列表值用 「/」 连接。"""

    result: dict[str, str] = {}
    for item in raw or ():
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        value = item.get("value")
        if not key:
            continue
        if isinstance(value, list):
            parts = []
            for entry in value:
                if isinstance(entry, dict):
                    parts.append(str(entry.get("v") or entry.get("k") or "").strip())
                else:
                    parts.append(str(entry).strip())
            text = " / ".join(part for part in parts if part)
        else:
            text = str(value or "").strip()
        if text:
            result[key] = text[:200]
    return result


def _image(images: Any) -> str:
    if isinstance(images, dict):
        for size in ("large", "common", "medium", "grid", "small"):
            value = str(images.get(size) or "").strip()
            if value:
                return value if value.startswith("http") else f"https:{value}"
    if isinstance(images, str) and images.strip():
        return images.strip()
    return ""


def parse_subject(raw: dict[str, Any]) -> Subject:
    """把任意 Bangumi 接口返回的条目字典收敛成 「Subject」。"""

    rating = raw.get("rating") or {}
    collection = raw.get("collection") or {}
    subject_id = int(raw.get("id") or 0)
    eps = int(raw.get("eps") or raw.get("episodes") or 0)
    return Subject(
        id=subject_id,
        name=str(raw.get("name") or "").strip(),
        name_cn=str(raw.get("name_cn") or "").strip(),
        type=int(raw.get("type") or TYPE_ANIME),
        summary=_clean_summary(raw.get("summary") or ""),
        image=_image(raw.get("images") or raw.get("image")),
        url=str(raw.get("url") or "") or f"{BANGUMI_SITE}/subject/{subject_id}",
        score=float(rating.get("score") or 0),
        rank=int(raw.get("rank") or rating.get("rank") or 0),
        rating_total=int(rating.get("total") or 0),
        air_date=str(raw.get("air_date") or raw.get("date") or "").strip(),
        air_weekday=int(raw.get("air_weekday") or 0),
        eps=eps,
        total_episodes=int(raw.get("total_episodes") or eps),
        tags=select_tags(raw.get("tags") or raw.get("meta_tags")),
        collection={str(key): int(value or 0) for key, value in collection.items()}
        if isinstance(collection, dict)
        else {},
        platform=str(raw.get("platform") or "").strip(),
        infobox=_infobox(raw.get("infobox")),
    )


class BangumiSource:
    """bgm.tv 的只读客户端。带 access token 时能拿到更全的字段。"""

    key = "bangumi"

    def __init__(self, http: HttpClient, *, access_token: str = "") -> None:
        self._http = http
        self._token = access_token or ""

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def calendar(self) -> list[CalendarDay]:
        """每日放送。返回固定 7 天，缺的那天给空 items 而不是漏掉。"""

        raw = await self._http.fetch_json(
            f"{BANGUMI_API}/calendar", headers=self._headers(), cache_key="bgm:calendar"
        )
        by_weekday: dict[int, CalendarDay] = {}
        for entry in raw or ():
            if not isinstance(entry, dict):
                continue
            weekday_info = entry.get("weekday") or {}
            weekday = int(weekday_info.get("id") or 0)
            items = tuple(
                parse_subject(item) for item in entry.get("items") or () if isinstance(item, dict)
            )
            if 1 <= weekday <= 7:
                by_weekday[weekday] = CalendarDay(
                    weekday=weekday,
                    label=str(weekday_info.get("cn") or "").strip(),
                    items=items,
                )
        from ..constants import WEEKDAY_CN

        return [
            by_weekday.get(day, CalendarDay(weekday=day, label=WEEKDAY_CN[day - 1], items=()))
            for day in range(1, 8)
        ]

    async def subject(self, subject_id: int) -> Subject | None:
        try:
            raw = await self._http.fetch_json(
                f"{BANGUMI_API}/v0/subjects/{int(subject_id)}",
                headers=self._headers(),
                cache_key=f"bgm:subject:{subject_id}",
            )
        except FetchError:
            return None
        return parse_subject(raw) if isinstance(raw, dict) else None

    async def search(
        self, keyword: str, *, limit: int = 5, subject_type: int | None = TYPE_ANIME
    ) -> list[Subject]:
        """搜索条目。先试 v0，再退回旧接口。"""

        keyword = keyword.strip()
        if not keyword:
            return []
        results = await self._search_v0(keyword, limit=limit, subject_type=subject_type)
        if results:
            return results
        return await self._search_legacy(keyword, limit=limit, subject_type=subject_type)

    async def _search_v0(
        self, keyword: str, *, limit: int, subject_type: int | None
    ) -> list[Subject]:
        body: dict[str, Any] = {"keyword": keyword, "sort": "match"}
        if subject_type:
            body["filter"] = {"type": [int(subject_type)]}
        try:
            raw = await self._http.fetch_json(
                f"{BANGUMI_API}/v0/search/subjects",
                method="POST",
                headers={**self._headers(), "Content-Type": "application/json"},
                params={"limit": max(1, min(25, limit)), "offset": 0},
                json_body=body,
                cache_key=f"bgm:search:v0:{subject_type}:{keyword}:{limit}",
            )
        except FetchError:
            return []
        data = raw.get("data") if isinstance(raw, dict) else None
        return [parse_subject(item) for item in data or () if isinstance(item, dict)][:limit]

    async def _search_legacy(
        self, keyword: str, *, limit: int, subject_type: int | None
    ) -> list[Subject]:
        params: dict[str, Any] = {"responseGroup": "large", "max_results": max(1, min(25, limit))}
        if subject_type:
            params["type"] = int(subject_type)
        from urllib.parse import quote

        try:
            raw = await self._http.fetch_json(
                f"{BANGUMI_API}/search/subject/{quote(keyword, safe='')}",
                headers=self._headers(),
                params=params,
                cache_key=f"bgm:search:legacy:{subject_type}:{keyword}:{limit}",
            )
        except FetchError:
            return []
        if not isinstance(raw, dict):
            return []
        return [parse_subject(item) for item in raw.get("list") or () if isinstance(item, dict)][
            :limit
        ]

    async def episodes(self, subject_id: int, *, limit: int = 100) -> list[Episode]:
        """正片分集（type=0）。"""

        try:
            raw = await self._http.fetch_json(
                f"{BANGUMI_API}/v0/episodes",
                headers=self._headers(),
                params={
                    "subject_id": int(subject_id),
                    "type": 0,
                    "limit": max(1, min(200, limit)),
                    "offset": 0,
                },
                cache_key=f"bgm:eps:{subject_id}:{limit}",
            )
        except FetchError:
            return []
        data = raw.get("data") if isinstance(raw, dict) else None
        episodes: list[Episode] = []
        for item in data or ():
            if not isinstance(item, dict):
                continue
            episodes.append(
                Episode(
                    id=int(item.get("id") or 0),
                    sort=float(item.get("sort") or item.get("ep") or 0),
                    name=str(item.get("name") or "").strip(),
                    name_cn=str(item.get("name_cn") or "").strip(),
                    airdate=str(item.get("airdate") or "").strip(),
                    duration=str(item.get("duration") or "").strip(),
                    summary=_clean_summary(item.get("desc") or "", 160),
                )
            )
        episodes.sort(key=lambda episode: episode.sort)
        return episodes

    async def next_episode(self, subject_id: int, *, today: str = "") -> Episode | None:
        """今天之后最近的一集，用于「放送时间」与追番进度提示。"""

        import datetime as _dt

        stamp = today or _dt.date.today().isoformat()
        for episode in await self.episodes(subject_id):
            if episode.airdate and episode.airdate >= stamp:
                return episode
        return None


def resolve_type(text: str) -> int | None:
    """把用户写的「番剧」「剧场版」「漫画」翻译成 Bangumi 的 type。"""

    token = str(text or "").strip().lower()
    if not token:
        return None
    if token in {"剧场版", "劇場版", "电影", "movie", "映画"}:
        return TYPE_ANIME  # 剧场版在 Bangumi 也归到「动画」，靠 platform 区分
    return TYPE_ALIASES.get(token)


def is_movie(subject: Subject) -> bool:
    return "剧场版" in subject.platform or "movie" in subject.platform.lower()
