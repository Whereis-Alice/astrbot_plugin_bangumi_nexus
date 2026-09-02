"""跨层传递的数据结构。

全部是 frozen dataclass 或轻量 dataclass，只带纯粹的展示派生属性，不做 IO。
数据源适配器负责把各站点五花八门的 JSON / HTML 收敛成这里的类型，上层服务与
渲染层因此只需要认识一套模型。

Copyright (C) 2026 Whereis-Alice and AstrBot Plugin Authors.
Licensed under the GNU Affero General Public License v3.0 or later.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .constants import SUBJECT_TYPE_CN, WATCH_STATUS_CN, WEEKDAY_CN
from .excludes import blocked_by

# ---------------------------------------------------------------------------
# Bangumi
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Subject:
    """一个 Bangumi 条目。字段全部可缺省，因为不同接口给的详细度不同。"""

    id: int
    name: str
    name_cn: str = ""
    type: int = 2
    summary: str = ""
    image: str = ""
    url: str = ""
    score: float = 0.0
    rank: int = 0
    rating_total: int = 0
    air_date: str = ""
    air_weekday: int = 0
    eps: int = 0
    total_episodes: int = 0
    tags: tuple[str, ...] = ()
    collection: dict[str, int] = field(default_factory=dict)
    platform: str = ""
    infobox: dict[str, str] = field(default_factory=dict)

    @property
    def display_name(self) -> str:
        return self.name_cn or self.name

    @property
    def alt_name(self) -> str:
        """副标题：中文名存在时给出原名，否则留空避免重复。"""

        return self.name if self.name_cn and self.name != self.name_cn else ""

    @property
    def type_label(self) -> str:
        return SUBJECT_TYPE_CN.get(self.type, "条目")

    @property
    def weekday_label(self) -> str:
        index = self.air_weekday - 1
        return WEEKDAY_CN[index] if 0 <= index < 7 else ""

    @property
    def doing(self) -> int:
        return int(self.collection.get("doing", 0) or 0)

    @property
    def score_label(self) -> str:
        return f"{self.score:.1f}" if self.score else "—"


@dataclass(frozen=True)
class CalendarDay:
    """每日放送的一天。"""

    weekday: int
    label: str
    items: tuple[Subject, ...]


@dataclass(frozen=True)
class Episode:
    """单集信息。

    「sort」 和 「ep」 在年番上会分道扬镳：Bangumi 给《超超超超超喜欢你的100个女朋友
    第三季》的第 1 集记的是 「ep=1、sort=25」 —— 前两季各 12 集，连续编号从 25 起算。
    字幕组的文件名跟 「sort」 走，观众数的却是 「ep」，所以两个都得留着：
    「number」 用来展示和记进度，「sort」 用来跟下载器给的编号对账。
    """

    id: int
    sort: float
    ep: float = 0.0
    name: str = ""
    name_cn: str = ""
    airdate: str = ""
    duration: str = ""
    summary: str = ""

    @property
    def number(self) -> float:
        """季内集数。上游没给 「ep」 时退回 「sort」，至少不会是 0。"""
        return self.ep or self.sort

    @property
    def display_name(self) -> str:
        return self.name_cn or self.name or f"第 {self.number:g} 话"


# ---------------------------------------------------------------------------
# bangumi-data：跨站 ID 与多语言标题
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SiteRef:
    """bangumi-data 里的一条站点引用。"""

    site: str
    id: str
    url: str = ""


@dataclass(frozen=True)
class DataItem:
    """bangumi-data 的一个条目，是跨源匹配的枢纽。"""

    title: str
    titles: tuple[str, ...]
    type: str = "tv"
    lang: str = "ja"
    official_site: str = ""
    begin: str = ""
    end: str = ""
    broadcast: str = ""
    sites: tuple[SiteRef, ...] = ()

    def site_id(self, site: str) -> str:
        for ref in self.sites:
            if ref.site == site:
                return ref.id
        return ""

    @property
    def bangumi_id(self) -> str:
        return self.site_id("bangumi")

    @property
    def mikan_id(self) -> str:
        return self.site_id("mikan")


# ---------------------------------------------------------------------------
# 其它数据源
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Anime1Entry:
    """anime1.me 的一行番剧。"""

    cat: int
    title: str
    status: str = ""
    year: str = ""
    season: str = ""
    note: str = ""

    @property
    def watch_url(self) -> str:
        return f"https://anime1.me/?cat={self.cat}"


@dataclass(frozen=True)
class SeasonEntry:
    """長門番堂季度表里的一部番，信息密度是所有源里最高的。"""

    title_cn: str
    title_jp: str = ""
    category: str = ""
    genres: tuple[str, ...] = ()
    staff: tuple[tuple[str, str], ...] = ()
    cast: tuple[tuple[str, str], ...] = ()
    broadcast: str = ""
    official_site: str = ""
    cover: str = ""

    @property
    def display_name(self) -> str:
        return self.title_cn or self.title_jp

    def staff_of(self, *keys: str) -> str:
        for key in keys:
            for role, value in self.staff:
                if key in role:
                    return value
        return ""

    @property
    def studio(self) -> str:
        return self.staff_of("动画制作", "製作", "制作")


@dataclass(frozen=True)
class AgeItem:
    """AGE 动漫推荐位的一条。"""

    title: str
    url: str
    cover: str = ""
    progress: str = ""


@dataclass(frozen=True)
class MoegirlHit:
    """萌娘百科搜索结果。"""

    title: str
    url: str
    summary: str = ""


# ---------------------------------------------------------------------------
# RSS 与订阅
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FeedItem:
    """RSS 条目。`uid` 是去重键，优先用 guid，退化到链接 / 标题。"""

    uid: str
    title: str
    link: str = ""
    published: str = ""
    summary: str = ""
    size: str = ""
    published_ts: float = 0.0


@dataclass(frozen=True)
class MikanGroup:
    """Mikan 番组页上的一个字幕组 / 搬运组。

    「samples」 是这个组最近几条发布的标题原文，「tags」 是从标题里嗅出来的
    语言 / 画质 / 片源标记。两者都只用于让用户在选源时看清「这个组给的是
    简体还是繁体、1080p 还是 720p、Baha 还是 ABEMA」，不参与去重。
    """

    id: int
    name: str
    updated: str = ""
    samples: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        """列表里那一行的短描述：组名 + 语言画质标记。"""

        return f"{self.name}（{' / '.join(self.tags)}）" if self.tags else self.name


@dataclass
class Subscription:
    """一条 RSS 订阅。同一会话内 `name` 唯一。"""

    id: int
    umo: str
    name: str
    url: str
    enabled: bool = True
    subject_id: int = 0
    keywords: tuple[str, ...] = ()
    excludes: tuple[str, ...] = ()
    last_checked: float = 0.0
    last_item: str = ""
    error: str = ""
    created_at: float = field(default_factory=time.time)

    def matches(self, title: str) -> bool:
        """关键词白名单 + 黑名单过滤，全部大小写不敏感。

        黑名单刻意复用 「nexus.excludes.blocked_by」 而不是在这里重写一遍
        「word in title」：这条订阅自己的 「excludes」 与全局/会话两层排除项
        本该是同一套语义，之前分家的结果是 「CR 」 的空格边界和双语单文件豁免
        只在轮询那条路上生效，per-subscription 黑名单静默误杀。
        """

        text = title.lower()
        if self.keywords and not any(word.lower() in text for word in self.keywords):
            return False
        return not blocked_by(title, self.excludes)


@dataclass
class WatchItem:
    """追番清单里的一部番。"""

    id: int
    umo: str
    subject_id: int
    title: str
    status: str = "watching"
    progress: int = 0
    total: int = 0
    score: float = 0.0
    cover: str = ""
    weekday: int = 0
    note: str = ""
    updated_at: float = field(default_factory=time.time)

    @property
    def status_label(self) -> str:
        return WATCH_STATUS_CN.get(self.status, self.status)

    @property
    def progress_label(self) -> str:
        return f"{self.progress}/{self.total}" if self.total else str(self.progress)

    @property
    def percent(self) -> int:
        if self.total <= 0:
            return 0
        return max(0, min(100, round(self.progress * 100 / self.total)))


# ---------------------------------------------------------------------------
# 跨源聚合结果
# ---------------------------------------------------------------------------


@dataclass
class MatchResult:
    """一部番在各个数据源里的对应物，聚合卡片直接吃这个对象。"""

    subject: Subject | None = None
    data_item: DataItem | None = None
    anime1: Anime1Entry | None = None
    season: SeasonEntry | None = None
    age: AgeItem | None = None
    moegirl: MoegirlHit | None = None
    #: Mikan 的番组 ID。有它才能列字幕组、拼单组 RSS；bangumi-data 没登记时为空。
    mikan_id: str = ""
    mikan_rss: str = ""
    confidence: float = 0.0
    notes: tuple[str, ...] = ()

    @property
    def title(self) -> str:
        if self.subject:
            return self.subject.display_name
        if self.season:
            return self.season.display_name
        if self.data_item:
            return self.data_item.titles[0] if self.data_item.titles else self.data_item.title
        if self.anime1:
            return self.anime1.title
        return "未知番剧"

    def matched_sources(self) -> tuple[str, ...]:
        pairs = (
            ("bangumi", self.subject),
            ("bangumi_data", self.data_item),
            ("anime1", self.anime1),
            ("yuc", self.season),
            ("age", self.age),
            ("moegirl", self.moegirl),
            ("mikan", self.mikan_rss or None),
        )
        return tuple(key for key, value in pairs if value)


# ---------------------------------------------------------------------------
# 通知
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Notification:
    """要发出去的一条通知。渲染与人格转述都以它为输入。"""

    kind: str
    title: str
    lines: tuple[str, ...] = ()
    subtitle: str = ""
    link: str = ""
    cover: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    def plain_text(self) -> str:
        head = f"{self.title}\n{self.subtitle}".strip()
        body = "\n".join(self.lines)
        return f"{head}\n{body}".strip() if body else head

    def dedup_key(self) -> str:
        return "|".join((self.kind, self.title, self.link, *self.lines))
