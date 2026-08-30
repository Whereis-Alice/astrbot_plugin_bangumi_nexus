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

# 抓 HTML 的站点（AGE / 長門番堂 / anime1 / 萌娘百科）普遍对「机器人 UA」不友好，
# 有的直接 403。所以爬页面时换成主流浏览器 UA；调 API 的站点（Bangumi 明确
# 要求 UA 里带项目地址）仍然用 「DEFAULT_USER_AGENT」，两者不能混。
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

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
# AGE 每 2~3 个月换域名（官方 README 自己写的），所以镜像按「新 → 旧 → 易记」排，
# 逐个试到通为止；顺序即优先级，官方最新域名永远放第一个。
AGE_MIRRORS: tuple[str, ...] = (
    "https://www.agedm.io",
    "https://www.agedm.org",
    "https://www.age.tv",
    "https://www.agedm.com",
    "https://agefans.com",
)
# 官方维护的域名公告页，用来在全部内置镜像都挂掉时自动发现新域名
AGE_DOMAIN_NOTICE = "https://raw.githubusercontent.com/agefanscom/website/main/README.md"
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

MIKAN_SITE = "https://mikanani.me"

# --- 发布标记与全局排除项 ---------------------------------------------------

#: 「展示标记 -> 命中关键词」。用于在选源列表上标出一个字幕组给的是
#: 简体还是繁体、1080p 还是 720p、Baha 还是 ABEMA 片源。
#: 顺序固定，这样两个组的标记可以横向对比；关键词一律小写匹配。
#: ⚠ 只用「足够长、不会误伤」的关键词。早期版本写过裸的 「sc」「tc」「gb」「ass」，
#: 它们会在 「Discovery」「Switch」「class」 这类普通单词里命中，标记就成了噪音。
RELEASE_TAG_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("简体", ("简体", "简日", "简中", "简繁", "chs", "[gb", "gb]", "[sc]", "sc]")),
    ("繁体", ("繁体", "繁日", "繁中", "简繁", "big5", "cht", "[tc]", "tc]")),
    ("1080p", ("1080p", "1920x1080")),
    ("720p", ("720p", "1280x720")),
    ("2160p", ("2160p", "3840x2160", "4k")),
    ("Baha", ("baha", "bahamut")),
    ("ABEMA", ("abema",)),
    ("CR", ("crunchyroll", "[cr]", "(cr)", "cr ")),
    ("B-Global", ("b-global", "bglobal", "bilibili")),
    ("内嵌", ("内嵌", "hardsub")),
    ("外挂", ("外挂", "内封", "softsub", "[ass", "ass]")),
    ("MKV", ("mkv",)),
    ("MP4", ("mp4",)),
)

#: 全局排除项预设：「展示名 -> 写进订阅 excludes 的关键词」。
#: 为什么要预设：同一个字幕组把简体、繁体、720p、1080p 各发一遍，
#: 逐条手写黑名单太啰嗦，WebUI 与指令都从这份清单里勾选。
EXCLUDE_PRESETS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("简体", ("简体", "简日", "CHS", "GB")),
    ("繁体", ("繁体", "繁日", "CHT", "BIG5")),
    ("720p", ("720p", "1280x720")),
    ("1080p", ("1080p", "1920x1080")),
    ("2160p", ("2160p", "3840x2160")),
    ("Baha", ("Baha", "Bahamut")),
    ("ABEMA", ("ABEMA",)),
    ("CR", ("Crunchyroll", "CR ")),
    ("B-Global", ("B-Global", "Bilibili")),
    ("合集", ("合集", "Batch", "BDRip", "BDrip")),
    ("生肉", ("生肉", "无字幕", "Raw")),
    ("MP4", ("MP4",)),
)

EXCLUDE_PRESET_BY_NAME = dict(EXCLUDE_PRESETS)

# --- TLS 宽松名单 -----------------------------------------------------------

#: 允许在证书校验失败时降级重试一次的主机（含子域）。
#: 为什么需要：長門番堂（「yuc.wiki」）这类个人站长期只挂半条证书链，
#: 某些系统的根证书库补不齐中间证书，握手就直接失败 —— 于是「新番数据」这一路
#: 整个哑掉。名单写死在代码里而不是开放给配置：能被降级的站点必须经过人工确认，
#: 而且这些站点只提供公开只读数据，中间人攻击的收益接近零。
#: 降级只影响名单内主机，其它请求仍然严格校验（见 「http.HttpClient.client」）。
TLS_RELAXED_HOSTS: tuple[str, ...] = ("yuc.wiki",)

#: 选源会话的存活时间（秒）。太短来不及看列表，太长会跟下一次选源串台。
PICK_SESSION_SECONDS = 180.0
#: 选源列表一次最多列几个组。Mikan 上极个别热番能有二十多个组。
PICK_MAX_OPTIONS = 12

# --- 内部限额 --------------------------------------------------------------

MAX_SUBSCRIPTIONS_PER_SESSION = 60
MAX_WATCHLIST_PER_SESSION = 300
MAX_ACTIVITY_ENTRIES = 240
MIN_RSS_INTERVAL_MINUTES = 5
COVER_CACHE_DAYS = 30
COVER_MAX_BYTES = 4 * 1024 * 1024

# 卡片里的封面一律先瘦身再内联：bgm 图床的 「l」 档单张近 1 MB，
# 十二张 base64 之后 HTML 会涨到十几 MB，远端渲染服务直接吃不下。
# 「c」 档（common）分辨率对 96px 的瓦片来说仍然过剩，够用且省一个数量级。
COVER_BGM_SIZE = "c"
#: 瓦片封面的长边上限（px）。卡片按 1.5 倍缩放渲染，所以留到 2 倍显示尺寸。
COVER_THUMB_EDGE = 320
#: 详情卡主视觉的封面要大一些，单独一档。
COVER_HERO_EDGE = 640
