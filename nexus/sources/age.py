"""AGE 动漫（agedm.io）推荐位适配器。

AGE 站点只有 HTML，没有 API。这里抓 「/recommend/{1..5}」 五页推荐位，用来回答
「最近有什么在更、更到第几集」，并给跨源匹配补一个「哪里能看」的入口。

注意：缩略图真正的地址在 「data-original」 上，「src」 是懒加载占位图 —— 直接取 src
会拿到一张灰色占位，上游插件踩过这个坑。

Copyright (C) 2026 Whereis-Alice and AstrBot Plugin Authors.
Licensed under the GNU Affero General Public License v3.0 or later.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..constants import AGE_RECOMMEND_URL, AGE_SITE
from ..http import FetchError, HttpClient
from ..models import AgeItem
from ..titles import alias_keys, best_match

MAX_PAGES = 5


def _absolute(url: str) -> str:
    text = str(url or "").strip()
    if not text:
        return ""
    if text.startswith("//"):
        return f"https:{text}"
    if text.startswith("http"):
        return text
    return f"{AGE_SITE}/{text.lstrip('/')}"


def parse_recommend(html: str) -> tuple[AgeItem, ...]:
    """解析一页推荐位。依赖缺失或页面改版时返回空元组。"""

    try:
        from bs4 import BeautifulSoup
    except ImportError:  # pragma: no cover
        return ()

    soup = BeautifulSoup(html or "", "html.parser")
    items: list[AgeItem] = []
    for block in soup.select("div.video_item"):
        anchor = block.select_one("div.video_item-title a") or block.find("a", href=True)
        if anchor is None:
            continue
        title = anchor.get_text(" ", strip=True)
        if not title:
            continue
        image = block.find("img")
        cover = ""
        if image is not None:
            cover = _absolute(
                image.get("data-original") or image.get("data-src") or image.get("src") or ""
            )
        progress_tag = block.select_one("span.video_item--info")
        items.append(
            AgeItem(
                title=title,
                url=_absolute(anchor.get("href") or ""),
                cover=cover,
                progress=progress_tag.get_text(" ", strip=True) if progress_tag else "",
            )
        )
    return tuple(items)


class AgeSource:
    """AGE 动漫推荐位。五页并发抓，任一页失败不影响其它页。"""

    key = "age"

    def __init__(self, http: HttpClient) -> None:
        self._http = http
        self._items: tuple[AgeItem, ...] = ()
        self._index: dict[str, AgeItem] = {}

    async def _page(self, page: int) -> tuple[AgeItem, ...]:
        try:
            html = await self._http.fetch_text(
                AGE_RECOMMEND_URL.format(page=page), cache_key=f"age:{page}", ttl=3600
            )
        except FetchError:
            return ()
        return parse_recommend(html)

    async def refresh(self, *, pages: int = MAX_PAGES) -> tuple[AgeItem, ...]:
        chunks = await asyncio.gather(
            *(self._page(page) for page in range(1, max(1, min(MAX_PAGES, pages)) + 1)),
            return_exceptions=True,
        )
        items: list[AgeItem] = []
        seen: set[str] = set()
        for chunk in chunks:
            if not isinstance(chunk, tuple):
                continue
            for item in chunk:
                if item.title not in seen:
                    seen.add(item.title)
                    items.append(item)
        if items:
            self._items = tuple(items)
            self._index = {}
            for item in self._items:
                for key in alias_keys(item.title):
                    self._index.setdefault(key, item)
        return self._items

    async def items(self) -> tuple[AgeItem, ...]:
        if not self._items:
            await self.refresh()
        return self._items

    async def recommend(self, *, limit: int = 12) -> list[AgeItem]:
        return list(await self.items())[: max(1, limit)]

    async def match(self, *titles: str) -> AgeItem | None:
        await self.items()
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
            hit, _ = best_match(title, self._items, key=lambda item: item.title, threshold=0.76)
            if hit:
                return hit
        return None

    def stats(self) -> dict[str, Any]:
        return {"items": len(self._items)}
