"""bangumi-data 适配器 —— 跨源匹配的枢纽。

「bangumi-data」 是一份 CC0 的社区维护对照表：每部番同时给出日文原名、多语言译名，
以及它在 bangumi / mikan / bangumi_moe / crunchyroll 等站点上的 ID。有了它，
「Bangumi 的条目」和「Mikan 的 RSS」之间才有一条可靠的连线，不必靠标题瞎猜。

上游插件普遍用已经 404 的 「bgmlist」，这里换成 bangumi-data，并且 jsDelivr 挂了会
自动退到 raw.githubusercontent。

Copyright (C) 2026 Whereis-Alice and AstrBot Plugin Authors.
Licensed under the GNU Affero General Public License v3.0 or later.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..constants import BANGUMI_DATA_CDN, BANGUMI_DATA_RAW
from ..http import FetchError, HttpClient
from ..models import DataItem, SiteRef
from ..titles import alias_keys, best_match, data_months, season_code, season_codes_around

#: 只有这些站点的链接对用户有意义，其余（字幕组内部 ID 等）不展示。
WATCHABLE_SITES = (
    ("bilibili", "哔哩哔哩"),
    ("iqiyi", "爱奇艺"),
    ("qq", "腾讯视频"),
    ("youku", "优酷"),
    ("mgtv", "芒果 TV"),
    ("acfun", "AcFun"),
    ("netflix", "Netflix"),
    ("crunchyroll", "Crunchyroll"),
    ("nicovideo", "niconico"),
    ("bangumi", "Bangumi"),
    ("mikan", "Mikan"),
    ("dmhy", "動漫花園"),
)

SITE_URL_TEMPLATES: dict[str, str] = {
    "bangumi": "https://bgm.tv/subject/{id}",
    "mikan": "https://mikanani.me/Home/Bangumi/{id}",
    "bilibili": "https://www.bilibili.com/bangumi/media/md{id}",
    "acfun": "https://www.acfun.cn/bangumi/aa{id}",
    "iqiyi": "https://www.iqiyi.com/{id}.html",
    "youku": "https://v.youku.com/v_show/id_{id}.html",
    "qq": "https://v.qq.com/detail/{id}.html",
    "mgtv": "https://www.mgtv.com/h/{id}.html",
    "netflix": "https://www.netflix.com/title/{id}",
    "crunchyroll": "https://www.crunchyroll.com/series/{id}",
    "nicovideo": "https://ch.nicovideo.jp/{id}",
    "dmhy": "https://share.dmhy.org/topics/list?keyword={id}",
}


def site_url(site: str, identifier: str, given: str = "") -> str:
    if given:
        return given
    template = SITE_URL_TEMPLATES.get(site)
    return template.format(id=identifier) if template and identifier else ""


def parse_item(raw: dict[str, Any]) -> DataItem:
    translate = raw.get("titleTranslate") or {}
    titles: list[str] = []
    original = str(raw.get("title") or "").strip()
    for lang in ("zh-Hans", "zh-Hant", "en", "ja"):
        for value in translate.get(lang) or ():
            text = str(value).strip()
            if text and text not in titles:
                titles.append(text)
    if original and original not in titles:
        titles.append(original)
    sites: list[SiteRef] = []
    for entry in raw.get("sites") or ():
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("site") or "").strip()
        identifier = str(entry.get("id") or "").strip()
        if not name:
            continue
        sites.append(
            SiteRef(
                site=name,
                id=identifier,
                url=site_url(name, identifier, str(entry.get("url") or "")),
            )
        )
    return DataItem(
        title=original,
        titles=tuple(titles),
        type=str(raw.get("type") or "tv"),
        lang=str(raw.get("lang") or "ja"),
        official_site=str(raw.get("officialSite") or "").strip(),
        begin=str(raw.get("begin") or "").strip(),
        end=str(raw.get("end") or "").strip(),
        broadcast=str(raw.get("broadcast") or "").strip(),
        sites=tuple(sites),
    )


class BangumiDataSource:
    """按月分片抓取 bangumi-data，并在内存里建两张索引（标题键 / bangumi ID）。"""

    key = "bangumi_data"

    def __init__(self, http: HttpClient) -> None:
        self._http = http
        self._by_month: dict[tuple[int, int], tuple[DataItem, ...]] = {}
        self._alias_index: dict[str, DataItem] = {}
        self._bangumi_index: dict[str, DataItem] = {}

    async def month(self, year: int, month: int) -> tuple[DataItem, ...]:
        cached = self._by_month.get((year, month))
        if cached is not None:
            return cached
        path = f"{year:04d}/{month:02d}.json"
        raw: Any = None
        for base in (BANGUMI_DATA_CDN, BANGUMI_DATA_RAW):
            try:
                raw = await self._http.fetch_json(
                    f"{base}/{path}", cache_key=f"bgmdata:{path}", ttl=6 * 3600
                )
                break
            except FetchError:
                continue
        items = tuple(parse_item(entry) for entry in raw or () if isinstance(entry, dict))
        self._by_month[(year, month)] = items
        self._index(items)
        return items

    def _index(self, items: tuple[DataItem, ...]) -> None:
        for item in items:
            for key in alias_keys(item.titles, item.title):
                self._alias_index.setdefault(key, item)
            if item.bangumi_id:
                self._bangumi_index.setdefault(item.bangumi_id, item)

    async def season(self, code: str = "") -> tuple[DataItem, ...]:
        """一个季度（三个月）的全部条目。"""

        months = data_months(code or season_code())
        if not months:
            return ()
        chunks = await asyncio.gather(
            *(self.month(year, month) for year, month in months), return_exceptions=True
        )
        result: list[DataItem] = []
        for chunk in chunks:
            if isinstance(chunk, tuple):
                result.extend(chunk)
        return tuple(result)

    async def warm(self, *, span: int = 1) -> int:
        """预热当前季度前后共 「2*span+1」 季，让后续匹配全部走内存。"""

        codes = season_codes_around(span=span)
        await asyncio.gather(*(self.season(code) for code in codes), return_exceptions=True)
        return len(self._alias_index)

    async def by_bangumi_id(self, subject_id: int | str) -> DataItem | None:
        """按 Bangumi 条目 ID 反查 —— 这是最可靠的一条匹配路径。"""

        wanted = str(subject_id).strip()
        if not wanted:
            return None
        if wanted in self._bangumi_index:
            return self._bangumi_index[wanted]
        await self.warm()
        return self._bangumi_index.get(wanted)

    async def by_title(
        self, title: str, *, threshold: float = 0.66
    ) -> tuple[DataItem | None, float]:
        """按标题匹配：先查归一化键的精确命中，再退化到模糊相似度。"""

        if not title.strip():
            return None, 0.0
        for key in alias_keys(title):
            hit = self._alias_index.get(key)
            if hit is not None:
                return hit, 1.0
        if not self._alias_index:
            await self.warm()
            for key in alias_keys(title):
                hit = self._alias_index.get(key)
                if hit is not None:
                    return hit, 1.0
        pool = list({id(item): item for item in self._alias_index.values()}.values())
        return best_match(title, pool, key=lambda item: item.titles, threshold=threshold)

    def watch_links(self, item: DataItem | None) -> tuple[tuple[str, str], ...]:
        """可点开的正版 / 索引站链接，「(站点中文名, URL)」。"""

        if item is None:
            return ()
        labels = dict(WATCHABLE_SITES)
        links: list[tuple[str, str]] = []
        for ref in item.sites:
            label = labels.get(ref.site)
            if label and ref.url:
                links.append((label, ref.url))
        return tuple(links)

    def stats(self) -> dict[str, int]:
        return {
            "months": len(self._by_month),
            "items": sum(len(items) for items in self._by_month.values()),
            "aliases": len(self._alias_index),
        }
