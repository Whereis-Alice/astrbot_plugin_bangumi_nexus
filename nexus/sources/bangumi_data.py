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
import time
from collections.abc import Collection
from datetime import UTC, datetime, timedelta
from typing import Any

from ..constants import BANGUMI_DATA_CDN, BANGUMI_DATA_RAW
from ..http import FetchError, HttpClient
from ..models import DataItem, SiteRef
from ..titles import (
    JST,
    Broadcast,
    alias_keys,
    best_match,
    data_months,
    parse_broadcast,
    parse_datetime,
    season_code,
    season_codes_around,
)

#: 只有连续剧集才谈得上「正在放送」，剧场版 / OVA 的 「begin」 是上映日，
#: 放进放送表只会制造噪音。
AIRING_TYPES = frozenset({"tv", "web"})

#: 「end」 缺失时的兜底窗口。bangumi-data 对仍在连载的番常常留空 end，
#: 但也有一批老条目是「忘了填」。年番满打满算 53 周，留到 400 天足够覆盖，
#: 又不至于把几年前的僵尸条目一起捞上来。
OPEN_ENDED_MAX_DAYS = 400

# 月表会持续补结束日期；HTTP 缓存之外的对象缓存也必须有期限。
MONTH_CACHE_SECONDS = 6 * 3600

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


def _slot_order(pair: tuple[DataItem, Broadcast]) -> tuple[int, int, str]:
    """按「本地放送时刻」排序，同一时刻再按标题定序，保证输出稳定。"""

    item, slot = pair
    local = slot.start.astimezone()
    return (local.hour, local.minute, item.title)


class BangumiDataSource:
    """按月分片抓取 bangumi-data，并在内存里建两张索引（标题键 / bangumi ID）。"""

    key = "bangumi_data"

    def __init__(self, http: HttpClient) -> None:
        self._http = http
        self._by_month: dict[tuple[int, int], tuple[DataItem, ...]] = {}
        self._alias_index: dict[str, DataItem] = {}
        self._bangumi_index: dict[str, DataItem] = {}
        self._month_fetched: dict[tuple[int, int], float] = {}
        self._month_locks: dict[tuple[int, int], asyncio.Lock] = {}

    async def month(self, year: int, month: int) -> tuple[DataItem, ...]:
        """月表六小时刷新一次，同月并发合并；抓取失败保留旧表但不重置时限。"""
        key = (year, month)
        lock = self._month_locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._by_month.get(key)
            if (
                cached is not None
                and time.monotonic() - self._month_fetched.get(key, 0) < MONTH_CACHE_SECONDS
            ):
                return cached
            return await self._fetch_month(year, month)

    async def _fetch_month(self, year: int, month: int) -> tuple[DataItem, ...]:
        """成功刷新后重建索引，清除已删除条目与旧结束日期，避免 setdefault 固化旧值。"""
        path = f"{year:04d}/{month:02d}.json"
        raw: Any = None
        for base in (BANGUMI_DATA_CDN, BANGUMI_DATA_RAW):
            try:
                raw = await self._http.fetch_json(
                    f"{base}/{path}", cache_key=f"bgmdata:{path}", ttl=MONTH_CACHE_SECONDS
                )
                break
            except FetchError:
                continue
        if not isinstance(raw, list):
            return self._by_month.get((year, month), ())
        items = tuple(parse_item(entry) for entry in raw or () if isinstance(entry, dict))
        self._by_month[(year, month)] = items
        self._month_fetched[(year, month)] = time.monotonic()
        self._alias_index.clear()
        self._bangumi_index.clear()
        for key in sorted(self._by_month):
            self._index(self._by_month[key])
        return items

    def _index(self, items: tuple[DataItem, ...]) -> None:
        for item in items:
            for key in alias_keys(item.titles, item.title):
                self._alias_index[key] = item
            if item.bangumi_id:
                self._bangumi_index[item.bangumi_id] = item

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

    # -- 正在放送 -----------------------------------------------------------

    def cached_by_bangumi_id(self, subject_id: int | str) -> DataItem | None:
        """只查内存索引，不触发抓取。

        给「批量给一屏条目补放送时间」这种场景用：调用方自己先 「warm」 一次，
        然后逐条 peek，避免每条都去 「by_bangumi_id」 里各自 await 一遍。
        """

        wanted = str(subject_id).strip()
        return self._bangumi_index.get(wanted) if wanted else None

    def is_airing(self, item: DataItem, moment: datetime) -> bool:
        """粗筛候选放送期；空 end 的 400 天窗口只用于召回，不能当成在播证据。"""

        if item.type not in AIRING_TYPES:
            return False
        begin = parse_datetime(item.begin)
        if begin is None or begin.astimezone(JST).date() > moment.astimezone(JST).date():
            return False
        end = parse_datetime(item.end)
        if end is not None:
            return end.astimezone(JST).date() >= moment.astimezone(JST).date()
        return moment - begin <= timedelta(days=OPEN_ENDED_MAX_DAYS)

    async def airing(
        self,
        *,
        span: int = 2,
        now: datetime | None = None,
    ) -> tuple[tuple[DataItem, Broadcast], ...]:
        """当下正在放送、且能解析出放送时刻的条目，按放送时间排序。

        「span」 决定往前后各看几个季度。这里默认 2（共五季、十五个月分片），
        因为年番 / 半年番的 「begin」 会落在两三个季度之前，只看当季必然漏掉。
        """

        await self.warm(span=span)
        moment = now or datetime.now(UTC)
        # 换到十月后，左右各两季的窗口从四月开始，会漏掉二月开播的年番。
        # 额外只补过去的月份，不扩大未来月份窗口；重启后也能找回真实在播年番。
        first = moment - timedelta(days=OPEN_ENDED_MAX_DAYS)
        start_month = first.year * 12 + first.month - 1
        end_month = moment.year * 12 + moment.month - 1
        await asyncio.gather(
            *(
                self.month(index // 12, index % 12 + 1)
                for index in range(start_month, end_month + 1)
            ),
            return_exceptions=True,
        )
        picked: dict[tuple[str, str], tuple[DataItem, Broadcast]] = {}
        for items in self._by_month.values():
            for item in items:
                slot = parse_broadcast(item.broadcast)
                if slot is None or not self.is_airing(item, moment):
                    continue
                # 同一部番可能被多个月份分片同时索引（跨季续播），按标题 + 开播日去重
                picked.setdefault((item.title, item.begin), (item, slot))
        return tuple(sorted(picked.values(), key=_slot_order))

    async def long_running(
        self,
        *,
        weekday: int,
        exclude: Collection[str] = (),
        span: int = 2,
        now: datetime | None = None,
    ) -> tuple[tuple[DataItem, Broadcast], ...]:
        """指定星期正在放送、但 Bangumi 每日放送没收录的番。

        为什么需要这一栏：「api.bgm.tv/calendar」 只返回**当季**新番，
        年番 / 半年番开播一个季度之后就从那个接口里消失了，可用户还在追。
        bangumi-data 带完整的 「begin」/「end」/「broadcast」，正好能把它们补回来。

        「exclude」 传整周日历里出现过的 bangumi 条目 ID，避免同一部番两栏重复。
        """

        skip = {str(value).strip() for value in exclude if str(value).strip()}
        return tuple(
            (item, slot)
            for item, slot in await self.airing(span=span, now=now)
            if slot.air_weekday == weekday and item.bangumi_id not in skip
        )

    def stats(self) -> dict[str, int]:
        return {
            "months": len(self._by_month),
            "items": sum(len(items) for items in self._by_month.values()),
            "aliases": len(self._alias_index),
        }
