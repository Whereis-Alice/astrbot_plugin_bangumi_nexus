"""RSS / Atom 适配器，兼顾 Mikan 与 RSSHub。

「feedparser」 装了就用它（容错最好），没装就退回 stdlib 的 「xml.etree」 —— 这样
插件不会因为一个可选依赖装不上就整块功能失效。上游 RSS 插件把 feedparser 列为硬
依赖，在部分离线环境里直接起不来。

去重键 「uid」 的取值顺序是 guid → link → 「标题+发布时间」 的哈希，因为不少字幕组
的 RSS 根本不给 guid。

Copyright (C) 2026 Whereis-Alice and AstrBot Plugin Authors.
Licensed under the GNU Affero General Public License v3.0 or later.
"""

from __future__ import annotations

import hashlib
import re
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote

from ..http import HttpClient
from ..models import FeedItem
from ..titles import parse_datetime

_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(TB|GB|MB|KB|B)", re.I)
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str, limit: int = 200) -> str:
    body = _TAG_RE.sub(" ", str(text or ""))
    body = re.sub(r"\s{2,}", " ", body).strip()
    return body[:limit]


def _timestamp(*candidates: str) -> float:
    for value in candidates:
        text = str(value or "").strip()
        if not text:
            continue
        moment = parse_datetime(text)
        if moment is not None:
            return moment.timestamp()
        try:
            return parsedate_to_datetime(text).timestamp()
        except (TypeError, ValueError):
            continue
    return 0.0


def _uid(guid: str, link: str, title: str, published: str) -> str:
    for value in (guid, link):
        if str(value or "").strip():
            return str(value).strip()
    seed = f"{title}|{published}".encode()
    return hashlib.sha1(seed).hexdigest()


def _size_of(text: str) -> str:
    match = _SIZE_RE.search(str(text or ""))
    return f"{match.group(1)} {match.group(2).upper()}" if match else ""


def parse_feed(payload: str, *, limit: int = 60) -> list[FeedItem]:
    """解析 RSS 2.0 / Atom。优先 feedparser，退回 ElementTree。"""

    if not payload or not payload.strip():
        return []
    items = _parse_with_feedparser(payload, limit)
    if items is not None:
        return items
    return _parse_with_etree(payload, limit)


def _parse_with_feedparser(payload: str, limit: int) -> list[FeedItem] | None:
    try:
        import feedparser  # type: ignore[import-untyped]
    except ImportError:
        return None
    parsed = feedparser.parse(payload)
    entries = getattr(parsed, "entries", None) or []
    if not entries and getattr(parsed, "bozo", 0) and not getattr(parsed, "feed", None):
        return None
    result: list[FeedItem] = []
    for entry in entries[:limit]:
        title = str(entry.get("title") or "").strip()
        if not title:
            continue
        link = str(entry.get("link") or "").strip()
        published = str(entry.get("published") or entry.get("updated") or "").strip()
        summary = _strip_html(entry.get("summary") or entry.get("description") or "")
        size = ""
        for enclosure in entry.get("enclosures") or ():
            length = str(enclosure.get("length") or "")
            if length.isdigit() and int(length) > 0:
                size = _human_size(int(length))
                break
        result.append(
            FeedItem(
                uid=_uid(str(entry.get("id") or ""), link, title, published),
                title=title,
                link=link,
                published=published,
                summary=summary,
                size=size or _size_of(summary),
                published_ts=_timestamp(published),
            )
        )
    return result


def _parse_with_etree(payload: str, limit: int) -> list[FeedItem]:
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(payload.strip())
    except ET.ParseError:
        return []

    def tag_of(element: Any) -> str:
        return element.tag.rsplit("}", 1)[-1].lower()

    def text_of(parent: Any, *names: str) -> str:
        for child in parent:
            if tag_of(child) in names:
                return (child.text or "").strip()
        return ""

    nodes: list[Any] = []
    for element in root.iter():
        if tag_of(element) in {"item", "entry"}:
            nodes.append(element)
    result: list[FeedItem] = []
    for node in nodes[:limit]:
        title = text_of(node, "title")
        if not title:
            continue
        link = text_of(node, "link")
        if not link:
            for child in node:
                if tag_of(child) == "link" and child.get("href"):
                    link = str(child.get("href"))
                    break
        published = text_of(node, "pubdate", "published", "updated")
        summary = _strip_html(text_of(node, "description", "summary", "content"))
        size = ""
        for child in node:
            if tag_of(child) == "enclosure":
                length = str(child.get("length") or "")
                if length.isdigit() and int(length) > 0:
                    size = _human_size(int(length))
                break
        result.append(
            FeedItem(
                uid=_uid(text_of(node, "guid", "id"), link, title, published),
                title=title,
                link=link,
                published=published,
                summary=summary,
                size=size or _size_of(summary),
                published_ts=_timestamp(published),
            )
        )
    return result


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit in {"B", "KB"} else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


class RssSource:
    """拉取并解析一个 feed。缓存刻意很短 —— 订阅轮询要的就是新鲜度。"""

    key = "rss"

    def __init__(self, http: HttpClient) -> None:
        self._http = http

    async def poll(self, url: str, *, limit: int = 60, ttl: float = 60.0) -> list[FeedItem]:
        payload = await self._http.fetch_text(url, cache_key=f"rss:{url}", ttl=ttl)
        items = parse_feed(payload, limit=limit)
        items.sort(key=lambda item: item.published_ts, reverse=True)
        return items

    async def probe(self, url: str) -> tuple[bool, str, int]:
        """试拉一次，返回 「(是否可用, 说明, 条目数)」，给 「/sub_test」 与诊断页用。"""

        try:
            items = await self.poll(url, limit=5, ttl=0)
        except Exception as error:  # noqa: BLE001 - 探测本身就是为了把任意故障翻成一句人话
            return False, str(error)[:160], 0
        if not items:
            return False, "能连上，但没解析出任何条目（可能不是 RSS 或当前为空）", 0
        return True, f"最新一条：{items[0].title[:60]}", len(items)


# ---------------------------------------------------------------------------
# 常用 feed 地址构造
# ---------------------------------------------------------------------------


def mikan_bangumi_feed(base: str, mikan_id: str | int) -> str:
    """Mikan 单番 RSS。「mikan_id」 来自 bangumi-data 的 sites 表。"""

    return f"{base.rstrip('/')}/RSS/Bangumi?bangumiId={mikan_id}"


def mikan_group_feed(base: str, mikan_id: str | int, subgroup_id: str | int) -> str:
    """Mikan 单番 + 单字幕组 RSS。

    这是订阅番剧的正确姿势：只订一个组，一集只推一条。
    「subgroupid」 留空时 Mikan 会退回整番源，所以调用方必须先确认拿到了组 id。
    """

    return f"{base.rstrip('/')}/RSS/Bangumi?bangumiId={mikan_id}&subgroupid={subgroup_id}"


def mikan_search_feed(base: str, keyword: str) -> str:
    return f"{base.rstrip('/')}/RSS/Search?searchstr={quote(keyword, safe='')}"


def mikan_classic_feed(base: str) -> str:
    return f"{base.rstrip('/')}/RSS/Classic"


def rsshub_feed(base: str, path: str) -> str:
    """把用户写的 「/bangumi/tv/calendar/today」 或整条 URL 都归一化成完整地址。"""

    text = str(path or "").strip()
    if text.startswith(("http://", "https://")):
        return text
    return f"{base.rstrip('/')}/{text.lstrip('/')}"


def dmhy_feed(keyword: str) -> str:
    return f"https://share.dmhy.org/topics/rss/rss.xml?keyword={quote(keyword, safe='')}"


def normalize_feed_url(raw: str, *, rsshub_base: str, mikan_base: str) -> str:
    """用户输入的订阅地址归一化。

    支持三种写法：完整 URL、「rsshub:路由」、「mikan:番剧ID」。这样群友不用记
    RSSHub 的域名，也不用手拼 Mikan 的查询串。
    """

    text = str(raw or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    if lowered.startswith("rsshub:"):
        return rsshub_feed(rsshub_base, text.split(":", 1)[1])
    if lowered.startswith("mikan:"):
        token = text.split(":", 1)[1].strip()
        if token.isdigit():
            return mikan_bangumi_feed(mikan_base, token)
        return mikan_search_feed(mikan_base, token)
    if lowered.startswith("dmhy:"):
        return dmhy_feed(text.split(":", 1)[1].strip())
    if text.startswith("/"):
        return rsshub_feed(rsshub_base, text)
    if not lowered.startswith("http"):
        return f"https://{text}"
    return text
