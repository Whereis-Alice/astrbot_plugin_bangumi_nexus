"""AGE 动漫（agedm.io）推荐位适配器。

AGE 站点只有 HTML，没有 API。这里抓 「/recommend/{1..5}」 五页推荐位，用来回答
「最近有什么在更、更到第几集」，并给跨源匹配补一个「哪里能看」的入口。

注意：缩略图真正的地址在 「data-original」 上，「src」 是懒加载占位图 —— 直接取 src
会拿到一张灰色占位，上游插件踩过这个坑。

另外两个坑，都在实机上撞过：
1. AGE 站在 Cloudflare 后面，机器人 UA 一律 403，必须伪装成浏览器；
2. 官方 README 自己写着「每 2~3 个月更换域名」，且机房 IP 常被整段风控。
   所以这里按镜像表逐个试，全挂了就去官方域名公告页捞新域名；
   确认被风控后进入冷却期，不再每次刷新都白等五个超时。

Copyright (C) 2026 Whereis-Alice and AstrBot Plugin Authors.
Licensed under the GNU Affero General Public License v3.0 or later.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from ..constants import AGE_DOMAIN_NOTICE, AGE_MIRRORS, AGE_SITE
from ..http import FetchError, HttpClient, browser_headers
from ..models import AgeItem
from ..titles import alias_keys, best_match

MAX_PAGES = 5
# 被风控（403）之后的冷却时长：期间直接返回空，不再发请求。
# 机房 IP 被 Cloudflare 拉黑不是几分钟能好的，每次刷新都重试纯属浪费。
BLOCK_COOLDOWN_SECONDS = 30 * 60
# 从官方域名公告页里捞域名用的正则；只认 age 系的域名，避免把贴吧链接也抓进来
_DOMAIN_RE = re.compile(r"https?://(?:www\.)?(age(?:dm|fans|app)?\.[a-z.]{2,8})", re.I)


def _absolute(url: str, site: str = AGE_SITE) -> str:
    text = str(url or "").strip()
    if not text:
        return ""
    if text.startswith("//"):
        return f"https:{text}"
    if text.startswith("http"):
        return text
    return f"{site.rstrip('/')}/{text.lstrip('/')}"


def parse_domains(markdown: str) -> tuple[str, ...]:
    """从官方域名公告页里提取还活着的域名，按出现顺序去重。

    公告里用 「~~删除线~~」 标记已阵亡的域名，先把这些整行剔掉再抓，
    否则会把一堆死域名排在前面，白试五六轮。
    """
    alive: list[str] = []
    for line in (markdown or "").splitlines():
        if "~~" in line:
            continue
        for match in _DOMAIN_RE.finditer(line):
            host = match.group(1).lower().rstrip(".")
            candidate = f"https://www.{host}" if not host.startswith("www.") else f"https://{host}"
            if candidate not in alive:
                alive.append(candidate)
    return tuple(alive)


def parse_recommend(html: str, site: str = AGE_SITE) -> tuple[AgeItem, ...]:
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
                image.get("data-original") or image.get("data-src") or image.get("src") or "",
                site,
            )
        progress_tag = block.select_one("span.video_item--info")
        items.append(
            AgeItem(
                title=title,
                url=_absolute(anchor.get("href") or "", site),
                cover=cover,
                progress=progress_tag.get_text(" ", strip=True) if progress_tag else "",
            )
        )
    return tuple(items)


class AgeSource:
    """AGE 动漫推荐位。先定域名，再五页并发抓，任一页失败不影响其它页。"""

    key = "age"

    def __init__(self, http: HttpClient) -> None:
        self._http = http
        self._items: tuple[AgeItem, ...] = ()
        self._index: dict[str, AgeItem] = {}
        self._site = ""
        self._blocked_until = 0.0
        self._blocked_note = ""
        self._mirrors: tuple[str, ...] = AGE_MIRRORS
        self._lock = asyncio.Lock()

    # -- 域名探测 -----------------------------------------------------------

    @property
    def site(self) -> str:
        """当前确认可用的域名；还没探测过就返回内置首选。"""
        return self._site or AGE_MIRRORS[0]

    @property
    def blocked(self) -> bool:
        """是否处于风控冷却期内。"""
        return time.time() < self._blocked_until

    @property
    def note(self) -> str:
        """给用户看的一句状态说明；正常时为空串。"""
        return self._blocked_note if self.blocked else ""

    async def _probe(self, site: str) -> str:
        """试抓某个域名的第一页推荐位，成功就返回 HTML。

        用推荐位而不是首页做探针：首页可能被 CDN 缓存成静态页而推荐位 403，
        真正要用的接口通了才算通。
        """
        return await self._http.fetch_text(
            f"{site.rstrip('/')}/recommend/1",
            cache_key=f"age:{site}:1",
            ttl=3600,
            headers=browser_headers(f"{site.rstrip('/')}/"),
            retries=1,
        )

    async def _discover(self) -> tuple[str, str]:
        """按镜像表逐个探测，返回（可用域名, 第一页 HTML）。

        全部失败且失败原因都是「被拒绝」时，去官方域名公告页捞一批新域名再试一轮
        —— AGE 每两三个月换一次域名，硬编码的表迟早过期。
        """
        refused = 0
        for site in self._mirrors:
            try:
                html = await self._probe(site)
            except FetchError as error:
                refused += 1 if error.refused else 0
                continue
            if "video_item" in html or "recommend" in html:
                return site, html
        extra = await self._fetch_domain_notice()
        for site in extra:
            if site in self._mirrors:
                continue
            try:
                html = await self._probe(site)
            except FetchError:
                continue
            if "video_item" in html or "recommend" in html:
                # 公告页里发现的新域名提到最前面，下次刷新直接命中
                self._mirrors = (site, *self._mirrors)
                return site, html
        if refused:
            self._blocked_until = time.time() + BLOCK_COOLDOWN_SECONDS
            self._blocked_note = (
                "AGE 动漫拒绝了本机访问（Cloudflare 风控常拦机房 IP）："
                f"已暂停 {BLOCK_COOLDOWN_SECONDS // 60} 分钟。"
                "想用这个源请在插件配置里填 「proxy」，或关掉 「age_enabled」。"
            )
        return "", ""

    async def _fetch_domain_notice(self) -> tuple[str, ...]:
        """读官方 GitHub 上的域名公告页。抓不到就当没有新域名。"""
        try:
            markdown = await self._http.fetch_text(
                AGE_DOMAIN_NOTICE, cache_key="age:domains", ttl=21600, retries=1
            )
        except FetchError:
            return ()
        return parse_domains(markdown)

    # -- 抓取 ---------------------------------------------------------------

    async def _page(self, site: str, page: int) -> tuple[AgeItem, ...]:
        try:
            html = await self._http.fetch_text(
                f"{site.rstrip('/')}/recommend/{page}",
                cache_key=f"age:{site}:{page}",
                ttl=3600,
                headers=browser_headers(f"{site.rstrip('/')}/"),
            )
        except FetchError:
            return ()
        return parse_recommend(html, site)

    async def refresh(self, *, pages: int = MAX_PAGES) -> tuple[AgeItem, ...]:
        """重抓推荐位。冷却期内直接返回上一次的结果，不发请求。"""
        if self.blocked:
            return self._items
        async with self._lock:
            wanted = max(1, min(MAX_PAGES, pages))
            site, first = await self._discover()
            if not site:
                return self._items
            self._site = site
            self._blocked_until = 0.0
            self._blocked_note = ""
            chunks: list[object] = [parse_recommend(first, site)]
            if wanted > 1:
                chunks.extend(
                    await asyncio.gather(
                        *(self._page(site, page) for page in range(2, wanted + 1)),
                        return_exceptions=True,
                    )
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
        return {
            "items": len(self._items),
            "site": self.site,
            "blocked": self.blocked,
            "note": self.note,
        }
