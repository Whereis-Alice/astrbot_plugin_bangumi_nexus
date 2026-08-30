"""Bangumi 番组计划（bgm.tv）适配器。

覆盖每日放送、条目详情、搜索、分集列表，以及制作阵容与主要声优。搜索优先走 v0 的
POST 接口（支持按类型过滤、返回字段更全），失败时自动退回旧版 GET 接口 —— 旧接口
偶尔比新接口更命中，两条路都留着比只留一条稳。

制作阵容特意从条目自带的 「infobox」 里提，而不是另外调 「/persons」：
「/persons」 一部长番能返回四百多条（连「转场绘」「制作进行」都算），既慢又没法直接
展示；「infobox」 是官方整理过的、按角色归好类的文本，一次条目请求就顺带拿到了。

Copyright (C) 2026 Whereis-Alice and AstrBot Plugin Authors.
Licensed under the GNU Affero General Public License v3.0 or later.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
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

#: 「展示标签 -> infobox 候选键」。同一个岗位在不同条目里写法不统一
#: （监督 / 総監督 / 系列监督 / シリーズ監督 都出现过），所以按优先级列一串，
#: 取第一个命中的即可；顺序也决定了卡片上的行序，从「谁做的」到「怎么做的」。
STAFF_LABELS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("原作", ("原作",)),
    ("导演", ("导演", "監督", "监督", "总导演", "総監督", "系列监督", "シリーズ監督")),
    ("动画制作", ("动画制作", "アニメーション制作", "製作", "制作")),
    ("系列构成", ("系列构成", "シリーズ構成", "脚本")),
    ("人物设定", ("人物设定", "キャラクターデザイン", "角色设计")),
    ("总作画监督", ("总作画监督", "総作画監督", "作画监督")),
    ("美术监督", ("美术监督", "美術監督")),
    ("音乐", ("音乐", "音楽")),
)

#: 「/characters」 里的 「relation」。只展示前两种：客串角色跟本作阵容无关，
#: 混进来会把真正的主角挤出榜（柯南就出现在《名侦探光之美少女！》的客串位）。
CAST_RELATIONS: tuple[str, ...] = ("主角", "配角")

#: infobox 里常见的「主美术：」「制片人辅佐：」这类岗位前缀，展示时是噪音。
_STAFF_PREFIX_RE = re.compile(r"^[^：:]{1,12}[：:]")
#: 人名分隔符：中日文顿号、全角逗号、斜杠、半角逗号都出现过。
_STAFF_SPLIT_RE = re.compile(r"[、,，/／]")


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


def staff_from_infobox(
    infobox: Mapping[str, str],
) -> tuple[tuple[tuple[str, str], ...], str]:
    """从 「infobox」 里挑出适合上卡片的制作阵容，顺带返回动画制作公司。

    为什么要裁剪：infobox 原文经常是 「主美术：濱野英次」 或
    「田中昂 (ABCアニメーション)、矢﨑史 (ADKエモーションズ)」 这种带前缀、带括号、
    十几个人并列的长串。卡片一行放不下，全塞进去反而什么都读不到，
    所以统一「去掉冒号前缀 → 只留前三个人名 → 单行截断」。

    返回 「(阵容行, 动画制作)」：动画制作要单独拎出来，因为它同时被用作卡片副标题。
    """
    rows: list[tuple[str, str]] = []
    studio = ""
    for label, keys in STAFF_LABELS:
        value = ""
        for key in keys:
            value = str(infobox.get(key) or "").strip()
            if value:
                break
        if not value:
            continue
        cleaned = _trim_staff(value)
        if not cleaned:
            continue
        if label == "动画制作" and not studio:
            studio = cleaned
        rows.append((label, cleaned))
    return tuple(rows), studio


def _trim_staff(value: str) -> str:
    """把 infobox 里一格制作信息压成一行能读的短文本。"""

    text = _STAFF_PREFIX_RE.sub("", value.replace("\n", "、").strip())
    names = [part.strip() for part in _STAFF_SPLIT_RE.split(text) if part.strip()]
    if not names:
        return ""
    head = "、".join(names[:3])
    if len(names) > 3:
        head += f" 等 {len(names)} 人"
    return head[:80]


def cast_from_characters(raw: Any, *, limit: int = 8) -> tuple[tuple[tuple[str, str], ...], str]:
    """把 「/v0/subjects/{id}/characters」 收敛成 「(角色, 声优)」 列表。

    按 「CAST_RELATIONS」 的顺序分组输出而不是保持原序：接口返回的顺序里
    客串角色可能排在最前面，直接截前 8 条会把主角截掉。
    同一位声优兼多角时也只留第一次出现，避免整块看起来像复读。

    返回 「(列表, 主角数量描述)」，第二项直接给卡片当角标用。
    """
    buckets: dict[str, list[tuple[str, str]]] = {name: [] for name in CAST_RELATIONS}
    total = 0
    for item in raw or ():
        if not isinstance(item, dict):
            continue
        relation = str(item.get("relation") or "").strip()
        if relation not in buckets:
            continue
        name = str(item.get("name") or "").strip()
        actors = item.get("actors") or ()
        voice = ""
        for actor in actors:
            if isinstance(actor, dict):
                voice = str(actor.get("name") or "").strip()
                if voice:
                    break
        if not name or not voice:
            continue
        total += 1
        buckets[relation].append((name, voice))
    ordered: list[tuple[str, str]] = []
    seen_voice: set[str] = set()
    for relation in CAST_RELATIONS:
        for name, voice in buckets[relation]:
            if voice in seen_voice:
                continue
            seen_voice.add(voice)
            ordered.append((name, voice))
    return tuple(ordered[: max(1, limit)]), f"{total} 位" if total else ""


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

    async def characters(self, subject_id: int, *, limit: int = 8) -> tuple[tuple[str, str], str]:
        """主要角色与声优。失败返回空，卡片自己少一栏就好，不该整张查番失败。

        缓存键不带 「limit」：接口一次就把全部角色返回了，裁剪是本地做的，
        换个 「limit」 再打一次请求纯属浪费。
        """
        try:
            raw = await self._http.fetch_json(
                f"{BANGUMI_API}/v0/subjects/{int(subject_id)}/characters",
                headers=self._headers(),
                cache_key=f"bgm:chars:{subject_id}",
                ttl=6 * 3600,
            )
        except FetchError:
            return (), ""
        return cast_from_characters(raw, limit=limit)

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
