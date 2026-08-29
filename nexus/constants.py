"""插件级常量：标识符、外部端点、文案与数据源清单。

这里不做任何 IO，也不 import 插件内其它模块，因此可以被任意层安全引用。

Copyright (C) 2026 Whereis-Alice and AstrBot Plugin Authors.
Licensed under the GNU Affero General Public License v3.0 or later.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

PLUGIN_NAME = "astrbot_plugin_bangumi_nexus"
PLUGIN_ID = "bangumi_nexus"
PLUGIN_DISPLAY_NAME = "番剧中枢"
PLUGIN_BRAND = "Bangumi Nexus"
PAGE_NAME = "nexus"
LOG_PREFIX = f"[{PLUGIN_DISPLAY_NAME}]"
REPO_URL = "https://github.com/Whereis-Alice/astrbot_plugin_bangumi_nexus"

PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def declared_version() -> str:
    """从 metadata.yaml 读版本号（去掉前缀 v），避免和 main 循环 import。"""

    try:
        text = (PLUGIN_ROOT / "metadata.yaml").read_text(encoding="utf-8")
    except OSError:  # pragma: no cover - 打包异常时不该让插件起不来
        return "0.0.0"
    for line in text.splitlines():
        if line.startswith("version:"):
            value = line.split(":", 1)[1].strip().strip("\"'")
            if value:
                return value.lstrip("vV")
    return "0.0.0"


PLUGIN_VERSION = declared_version()

DEFAULT_USER_AGENT = f"{PLUGIN_NAME}/{PLUGIN_VERSION} (+{REPO_URL})"

# --- 外部端点 --------------------------------------------------------------

BANGUMI_API = "https://api.bgm.tv"
BANGUMI_SITE = "https://bgm.tv"
BANGUMI_DATA_CDN = "https://cdn.jsdelivr.net/gh/bangumi-data/bangumi-data@latest/data/items"
BANGUMI_DATA_RAW = "https://raw.githubusercontent.com/bangumi-data/bangumi-data/master/data/items"
ANIME1_LIST_URL = "https://anime1.me/animelist.json"
ANIME1_WATCH_URL = "https://anime1.me/?cat={cat}"
YUC_SEASON_URL = "https://yuc.wiki/{season}"
AGE_RECOMMEND_URL = "https://www.agedm.io/recommend/{page}"
AGE_SITE = "https://www.agedm.io"
MOEGIRL_API = "https://zh.moegirl.org.cn/api.php"
MOEGIRL_PAGE = "https://zh.moegirl.org.cn/{title}"

# --- 展示文案 --------------------------------------------------------------

WEEKDAY_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
SUBJECT_TYPE_CN = {
    1: "书籍",
    2: "动画",
    3: "音乐",
    4: "游戏",
    6: "三次元",
}
COLLECTION_CN = {
    "wish": "想看",
    "collect": "看过",
    "doing": "在看",
    "on_hold": "搁置",
    "dropped": "抛弃",
}
WATCH_STATUS_CN = {
    "watching": "在追",
    "planned": "想看",
    "finished": "已看完",
    "dropped": "已弃坑",
}

# --- 数据源清单（诊断页与 README 共用一份事实） -----------------------------


@dataclass(frozen=True)
class SourceInfo:
    """一个外部数据源的说明，用于诊断卡片、WebUI 与致谢。"""

    key: str
    name: str
    role: str
    home: str
    license_note: str = ""


SOURCES: tuple[SourceInfo, ...] = (
    SourceInfo("bangumi", "Bangumi 番组计划", "条目、评分、每日放送、收藏数", "https://bgm.tv"),
    SourceInfo(
        "bangumi_data",
        "bangumi-data",
        "跨站 ID 与多语言标题的对照总表，跨源匹配的枢纽",
        "https://github.com/bangumi-data/bangumi-data",
        "CC0-1.0",
    ),
    SourceInfo("anime1", "anime1.me", "在线观看索引（繁体）", "https://anime1.me"),
    SourceInfo("yuc", "長門番堂", "季度新番表：制作组、声优、题材、首播时间", "https://yuc.wiki"),
    SourceInfo("age", "AGE 动漫", "推荐位与更新集数", "https://www.agedm.io"),
    SourceInfo(
        "moegirl", "萌娘百科", "角色 / 作品词条摘要", "https://zh.moegirl.org.cn", "CC BY-NC-SA 3.0"
    ),
    SourceInfo("mikan", "Mikan Project", "单番字幕组资源 RSS", "https://mikanani.me"),
    SourceInfo("rsshub", "RSSHub", "万物皆可 RSS，补足其它站点订阅", "https://docs.rsshub.app"),
)

SOURCE_BY_KEY = {source.key: source for source in SOURCES}

# --- 内部限额 --------------------------------------------------------------

MAX_SUBSCRIPTIONS_PER_SESSION = 60
MAX_WATCHLIST_PER_SESSION = 300
MAX_ACTIVITY_ENTRIES = 240
MIN_RSS_INTERVAL_MINUTES = 5
COVER_CACHE_DAYS = 30
COVER_MAX_BYTES = 4 * 1024 * 1024
