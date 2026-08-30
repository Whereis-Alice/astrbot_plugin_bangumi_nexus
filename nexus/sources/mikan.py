"""Mikan Project 番组页解析：把「一部番有哪些字幕组」抓出来。

为什么需要这个模块：Mikan 只提供两种 RSS —— 关键词搜索源
「/RSS/Search?searchstr=...」 和单字幕组源 「/RSS/Bangumi?bangumiId=X&subgroupid=Y」。
上游插件（含 「astrbot_plugin_rsshub」）一律用前者，于是所有字幕组、所有语言、
所有画质的发布全被收下：一集番能推七八条，群里直接刷屏。
后者才是正确姿势，但 「subgroupid」 官方没有 API，只能解析番组页拿到。

页面结构（2026-08 实测）：
- 左栏 「div.leftbar-nav」 里每个 「a.subgroup-name.subgroup-{id}」 是一个组，
  同 「li」 内的 「span.date」 是该组最后更新日期；
- 正文里每个组有一块 「div.m-bangumi-content#bangumi-episode-subgroup-{id}」，
  其中的 「div.m-bangumi-item .text a」 是发布标题原文。

解析全部走 「BeautifulSoup」，缺依赖或页面改版时返回空元组而不是抛错 ——
选源流程会自动退回「关键词搜索源」这条老路，功能降级但不中断。

Copyright (C) 2026 Whereis-Alice and AstrBot Plugin Authors.
Licensed under the GNU Affero General Public License v3.0 or later.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from ..constants import MIKAN_SITE, RELEASE_TAG_RULES
from ..http import FetchError, HttpClient, browser_headers
from ..models import MikanGroup

#: 左栏里 「subgroup-615」 这种 class 名，用来取组 id。
_GROUP_CLASS_RE = re.compile(r"subgroup-(\d+)\b")
#: 搜索结果页里的番组链接 「/Home/Bangumi/3883」。
_BANGUMI_ID_RE = re.compile(r"/Home/Bangumi/(\d+)")
#: 一个组最多留几条样例标题：够看出简繁 / 画质就行，多了只是噪音。
SAMPLE_LIMIT = 6


def release_tags(titles: tuple[str, ...] | list[str], *, limit: int = 5) -> tuple[str, ...]:
    """从发布标题里嗅出语言 / 画质 / 片源标记。

    为什么要嗅：同一个字幕组常同时发简体和繁体、1080p 和 720p，
    甚至同一集分别从 Baha 和 ABEMA 压两版（「Kirara Fantasia」 就是这样）。
    用户在选源时看不到这些差异，就只能靠订阅后被刷屏才发现。

    命中顺序按 「RELEASE_TAG_RULES」 固定，这样两个组的标记可以横向对比。
    """
    blob = " ".join(titles).lower()
    hits = [
        label
        for label, needles in RELEASE_TAG_RULES
        if any(needle.lower() in blob for needle in needles)
    ]
    return tuple(hits[: max(1, limit)])


def parse_search(html: str) -> int:
    """从 Mikan 搜索结果页里取第一个番组 id，取不到返回 0。"""

    found = _BANGUMI_ID_RE.search(html or "")
    return int(found.group(1)) if found else 0


def parse_groups(html: str) -> tuple[MikanGroup, ...]:
    """解析番组页，按左栏顺序（即最近更新优先）返回字幕组列表。"""

    try:
        from bs4 import BeautifulSoup
    except ImportError:  # pragma: no cover - 打包环境缺依赖时降级
        return ()

    soup = BeautifulSoup(html or "", "html.parser")
    samples = _samples_by_group(soup)
    groups: list[MikanGroup] = []
    seen: set[int] = set()
    for anchor in soup.select("div.leftbar-nav a.subgroup-name"):
        group_id = _group_id(anchor.get("class") or ())
        if not group_id or group_id in seen:
            continue
        name = anchor.get_text(" ", strip=True)
        if not name:
            continue
        seen.add(group_id)
        titles = samples.get(group_id, ())
        groups.append(
            MikanGroup(
                id=group_id,
                name=name,
                updated=_updated_of(anchor),
                samples=titles,
                tags=release_tags(titles),
            )
        )
    return tuple(groups)


def _group_id(classes: Any) -> int:
    """从 class 列表里抠出 「subgroup-{id}」 的数字部分。"""

    for name in classes or ():
        found = _GROUP_CLASS_RE.fullmatch(str(name))
        if found:
            return int(found.group(1))
    return 0


def _updated_of(anchor: Any) -> str:
    """取同一 「li」 里的更新日期；页面偶尔不给，缺了就留空。"""

    holder = anchor.find_parent("li")
    if holder is None:
        return ""
    date = holder.select_one("span.date")
    return date.get_text(" ", strip=True) if date else ""


def _samples_by_group(soup: Any) -> dict[int, tuple[str, ...]]:
    """收集每个组最近的发布标题。"""

    result: dict[int, tuple[str, ...]] = {}
    for block in soup.select('div[id^="bangumi-episode-subgroup-"]'):
        found = _GROUP_CLASS_RE.search(str(block.get("id") or ""))
        if not found:
            continue
        titles = [
            text
            for node in block.select("div.m-bangumi-item div.text a")
            if (text := node.get_text(" ", strip=True))
        ]
        if titles:
            result[int(found.group(1))] = tuple(titles[:SAMPLE_LIMIT])
    return result


class MikanSource:
    """Mikan 番组页的只读访问层。所有失败都降级成空结果。"""

    def __init__(self, http: HttpClient, *, base: str = MIKAN_SITE) -> None:
        self._http = http
        self._base = base.rstrip("/") or MIKAN_SITE

    def set_base(self, base: str) -> None:
        """站点被墙时用户会换镜像域名，这里跟着配置走。"""

        self._base = (base or MIKAN_SITE).rstrip("/")

    @property
    def base(self) -> str:
        return self._base

    async def groups(self, bangumi_id: str | int, *, ttl: float = 1800.0) -> tuple[MikanGroup, ...]:
        """列出某个番组的字幕组。缓存半小时：字幕组阵容一天都不会变几次。"""

        token = str(bangumi_id or "").strip()
        if not token.isdigit():
            return ()
        url = f"{self._base}/Home/Bangumi/{token}"
        try:
            html = await self._http.fetch_text(
                url,
                cache_key=f"mikan:groups:{token}",
                ttl=ttl,
                headers=browser_headers(f"{self._base}/"),
            )
        except FetchError:
            return ()
        return parse_groups(html)

    async def search_id(self, keyword: str, *, ttl: float = 3600.0) -> int:
        """按关键词搜一个番组 id，供 bangumi-data 没登记 mikan_id 时兜底。"""

        text = str(keyword or "").strip()
        if not text:
            return 0
        url = f"{self._base}/Home/Search?searchstr={quote(text, safe='')}"
        try:
            html = await self._http.fetch_text(
                url,
                cache_key=f"mikan:search:{text}",
                ttl=ttl,
                headers=browser_headers(f"{self._base}/"),
            )
        except FetchError:
            return 0
        return parse_search(html)
