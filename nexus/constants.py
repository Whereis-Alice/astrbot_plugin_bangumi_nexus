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
    # ⚠ 同样不能收 「gb]」：「[2.1GB]」 这类体积标注会被误标成简体，
    # 于是一条纯繁体发布在同集归并里拿到「简体」的分，反过来压掉真正的简体版。
    ("简体", ("简体", "简日", "简中", "简繁", "chs", "[gb", "[sc]", "sc]")),
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
#: ⚠ 这份表跟 「RELEASE_TAG_RULES」 长得像，但**不能**合并：那张表是「给这条发布贴标签」，
#: 双语单文件既算简体也算繁体；这张表是「命中就丢掉」，双语单文件不该被
#: 「不要繁体」 误杀。所以两张表的语言项故意不同 —— 这里绝不收 「简繁」。
#: ⚠ 关键词一律走子串匹配，所以短缩写必须自带边界（「[CR]」「CR 」 而不是裸 「CR」，
#: 否则 「Secret」「Sacred」 里的 「cr」 也会命中；「Raw」 同理会打死 「[NC-Raws]」）。
EXCLUDE_PRESETS: tuple[tuple[str, tuple[str, ...]], ...] = (
    # ⚠ 这里刻意**不收** 「GB]」：「[2.1GB]」 这种体积标注满天飞，会把整组发布误杀。
    # 简体写成 「[GB]」「[GB_JP]」 的都由 「[GB」 覆盖。
    ("简体", ("简体", "简日", "简中", "CHS", "[GB", "[SC]", "SC]")),
    ("繁体", ("繁体", "繁日", "繁中", "CHT", "BIG5", "[TC]", "TC]")),
    ("简繁", ("简繁", "繁简", "CHS&CHT", "CHT&CHS", "GB&BIG5", "BIG5&GB")),
    ("720p", ("720p", "1280x720")),
    ("1080p", ("1080p", "1920x1080")),
    ("2160p", ("2160p", "3840x2160")),
    ("Baha", ("Baha", "Bahamut")),
    ("ABEMA", ("ABEMA",)),
    ("CR", ("Crunchyroll", "[CR]", "(CR)", "CR ")),
    ("B-Global", ("B-Global", "BGlobal", "Bilibili")),
    ("内嵌", ("内嵌", "hardsub")),
    ("外挂", ("外挂", "内封", "softsub", "[ASS", "ASS]")),
    ("MKV", ("MKV",)),
    ("MP4", ("MP4",)),
    ("合集", ("合集", "Batch", "BDRip")),
    ("生肉", ("生肉", "无字幕", "[RAW]", "(RAW)", "RAW ")),
)

#: 「单文件双语」的写法。命中其中之一，就说明这条发布同时带简体与繁体字幕。
#: 为什么要单列：过滤是纯子串匹配，而 「简繁日内封」 里含有 「繁日」、
#: 「CHS&CHT」 里含有 「CHT」 —— 用户勾「不要繁体」 是想躲纯繁版，
#: 不是想把本来就带简体的双语单文件一起丢掉（实测这类发布占比很高，
#: 误杀等于整集收不到）。
DUAL_LANGUAGE_MARKERS: tuple[str, ...] = (
    "简繁",
    "繁简",
    "chs&cht",
    "cht&chs",
    "chs_cht",
    "cht_chs",
    "chs-cht",
    "cht-chs",
    "gb&big5",
    "big5&gb",
    "gb_big5",
    "big5_gb",
)

#: 「简体」组的全部写法（小写）。
SIMPLIFIED_ONLY_WORDS: frozenset[str] = frozenset(
    word.lower() for word in dict(EXCLUDE_PRESETS)["简体"]
)

#: 「繁体」组的全部写法（小写）。
TRADITIONAL_ONLY_WORDS: frozenset[str] = frozenset(
    word.lower() for word in dict(EXCLUDE_PRESETS)["繁体"]
)

#: 只在「单语」时才算命中的关键词，即上面 「简体」/「繁体」 两组预设的全部写法。
#: 标题里出现双语迹象时，这些词的命中一律作废（见 「blocked_by」）。
#: 「简繁」 那组预设不在此列 —— 勾它的人要的正是「双语单文件也别给我」。
LANGUAGE_ONLY_WORDS: frozenset[str] = SIMPLIFIED_ONLY_WORDS | TRADITIONAL_ONLY_WORDS

EXCLUDE_PRESET_BY_NAME = dict(EXCLUDE_PRESETS)

#: 跨轮次同集归并的默认时间窗（小时）。0 表示只在单次轮询的批次内归并。
#: 为什么默认 48：实测同一个组的四个片源发布日期能跨两天（CR 8/31 → Baha 9/1，
#: ABEMA 甚至晚五天），只在一次轮询里归并等于形同虚设 —— 先到的那一版当轮就推，
#: 后到的下一轮又被当成新条目再推一次，一集照样刷两三条。窗口开到两天覆盖绝大
#: 多数场景，又短于常规周更间隔（7 天），所以下一集不会被上一集的记录挡住。
EPISODE_DEDUP_WINDOW_HOURS: int = 48

#: 同一集出现多个版本时的默认优选顺序（靠前的优先留下）。
#: 为什么需要：一个字幕组常把同一集分别从 Baha、ABEMA 压两版，再各出简体/繁体、
#: 1080p/720p —— 一集能刷出四到六条。靠排除项硬屏蔽 「ABEMA」 是个坏解法：
#: 那天 Baha 没出片就整集收不到。所以改成「都收下，但只推最合口味的那一条」。
#: 名字取自 「RELEASE_TAG_RULES」 的展示标记，没写进来的标记权重为 0。
EPISODE_PREFER_DEFAULT: tuple[str, ...] = ("简体", "1080p", "Baha", "MKV", "外挂")

#: 可用于优选顺序的合法标记，即 「RELEASE_TAG_RULES」 的全部展示名。
EPISODE_PREFER_CHOICES: tuple[str, ...] = tuple(label for label, _ in RELEASE_TAG_RULES)

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
