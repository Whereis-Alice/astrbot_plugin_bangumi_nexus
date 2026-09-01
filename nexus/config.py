"""把 AstrBotConfig 收敛成一个不可变快照。

配置项在 Dashboard 里随时会被改，业务代码却不该每次都去 `dict.get(...)` 加一堆
类型判断。这里在每次需要时重新解析一遍（很便宜），返回 frozen dataclass，
于是下游拿到的一定是已经校验、夹紧过范围的值。

Copyright (C) 2026 Whereis-Alice and AstrBot Plugin Authors.
Licensed under the GNU Affero General Public License v3.0 or later.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from .constants import (
    DEFAULT_USER_AGENT,
    EPISODE_DEDUP_WINDOW_HOURS,
    EPISODE_PREFER_DEFAULT,
    MIN_RSS_INTERVAL_MINUTES,
)
from .dedup import normalize_prefer

# ani-rss 的默认端口与地址规范化属于协议知识，放在客户端模块里只存一份；
# 配置层直接复用，用户填 「192.168.1.8」 也能存成可用的 base URL。
from .sources.anirss import normalize_base as normalize_anirss_base

RENDERERS = ("auto", "html", "raster", "t2i", "text")
SORT_KEYS = ("score", "doing", "time", "name")
GACHA_SOURCES = ("auto", "yuc", "bangumi")

#: 只写不读的敏感配置项。这份名单必须只有一处 —— 「payload()」 按它脱敏、
#: WebUI 按它决定「不回显、留空即不改」，两边一旦对不上，前端就会把脱敏后的
#: 「true」 当成真值回写，密钥被悄悄改成字符串 「true」。
SECRET_KEYS: frozenset[str] = frozenset(
    {
        "bangumi_access_token",
        "webhook_token",
        "anirss_api_key",
        "anirss_password",
    }
)

DEFAULT_PERSONA_INSTRUCTION = (
    "请用你自己的口吻，简短自然地向大家转述下面这条番剧通知。"
    "保持信息准确，不要编造剧情，不要使用列表和标题，控制在两句话以内。"
)


def _get(source: Mapping[str, Any] | Any, key: str, default: Any) -> Any:
    if source is None:
        return default
    try:
        value = source.get(key, default)  # type: ignore[union-attr]
    except AttributeError:
        value = getattr(source, key, default)
    return default if value is None else value


def _as_int(value: Any, default: int, *, low: int | None = None, high: int | None = None) -> int:
    try:
        result = int(float(value))
    except (TypeError, ValueError):
        result = default
    if low is not None:
        result = max(low, result)
    if high is not None:
        result = min(high, result)
    return result


def _as_float(
    value: Any, default: float, *, low: float | None = None, high: float | None = None
) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        result = default
    if low is not None:
        result = max(low, result)
    if high is not None:
        result = min(high, result)
    return result


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in {"true", "1", "yes", "on", "开", "是"}:
        return True
    if text in {"false", "0", "no", "off", "关", "否"}:
        return False
    return default


def _as_str(value: Any, default: str = "") -> str:
    text = str(value if value is not None else default).strip()
    return text or default


def _as_choice(value: Any, allowed: tuple[str, ...], default: str) -> str:
    text = _as_str(value).lower()
    return text if text in allowed else default


def _as_list(value: Any) -> tuple[str, ...]:
    """列表 / 逗号或换行分隔的字符串都接受，自动去空去重且保序。"""

    if isinstance(value, str):
        parts = re.split(r"[,\n;，、]+", value)
    elif isinstance(value, (list, tuple, set)):
        parts = [str(item) for item in value]
    else:
        return ()
    seen: dict[str, None] = {}
    for part in parts:
        text = part.strip()
        if text:
            seen.setdefault(text, None)
    return tuple(seen)


def parse_times(value: Any, default: tuple[str, ...] = ("08:30",)) -> tuple[str, ...]:
    """解析 `08:30,20:00` 形式的时刻表，返回排好序的 `HH:MM` 元组。"""

    result: list[str] = []
    for token in _as_list(value):
        match = re.fullmatch(r"(\d{1,2})\s*[:：]\s*(\d{1,2})", token.strip())
        if not match:
            match = re.fullmatch(r"(\d{1,2})", token.strip())
            if not match:
                continue
            hour, minute = int(match.group(1)), 0
        else:
            hour, minute = int(match.group(1)), int(match.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            result.append(f"{hour:02d}:{minute:02d}")
    return tuple(sorted(set(result))) or default


def parse_hours(value: Any) -> tuple[int, ...]:
    """解析 `1,13,22` 形式的整点小时列表。"""

    hours = []
    for token in _as_list(value):
        text = token.strip()
        if text.startswith(("-", "+")):
            # 带符号写法一律丢弃：否则「-1」会被抽成「1」，静默变成凌晨 1 点刷新。
            continue
        digits = re.sub(r"\D", "", text)
        if digits and 0 <= int(digits) <= 23:
            hours.append(int(digits))
    return tuple(sorted(set(hours)))


@dataclass(frozen=True)
class NexusConfig:
    """一次解析、随处引用的运行时配置快照。"""

    # 渲染
    card_theme: str = "midnight"
    card_renderer: str = "auto"
    card_width: int = 860
    # 网络
    bangumi_access_token: str = ""
    user_agent: str = DEFAULT_USER_AGENT
    proxy: str = ""
    http_timeout_seconds: int = 20
    http_max_retries: int = 3
    cache_ttl_minutes: int = 30
    max_concurrency: int = 5
    # 搜索
    search_max_results: int = 5
    enable_cross_match: bool = True
    translate_summary: bool = False
    translate_provider_id: str = ""
    long_reply_as_card: bool = True
    show_watch_text: bool = True
    # 每日播报
    push_enabled: bool = False
    push_times: tuple[str, ...] = ("08:30",)
    push_targets: tuple[str, ...] = ()
    push_max_items: int = 12
    push_sort_by: str = "score"
    push_sort_order: str = "desc"
    push_min_score: float = 0.0
    push_min_doing: int = 0
    #: 每日播报只播当前会话追番表里的番。默认关 —— 新装的实例追番表是空的，
    #: 一上来就开会让播报永远空手而归，用户只会以为功能坏了。
    push_only_watchlist: bool = False
    # 人格转述
    persona_reply_enabled: bool = True
    persona_id: str = ""
    persona_provider_id: str = ""
    persona_instruction: str = DEFAULT_PERSONA_INSTRUCTION
    persona_max_chars: int = 180
    # RSS
    rss_enabled: bool = True
    rss_interval_minutes: int = 15
    rss_max_items_per_poll: int = 5
    rss_first_poll_silent: bool = True
    #: 只给番名订阅时，先列出 Mikan 上的字幕组让用户回序号，而不是直接收下全部发布。
    rss_pick_source: bool = True
    rss_history_days: int = 14
    #: 跨会话生效的排除项（预设名或自定义词）。会话级 「/sub_exclude」 会叠加在它之上。
    global_excludes: tuple[str, ...] = ()
    #: 同一集出现多个版本（简繁 / 画质 / 片源）时只推一条。
    rss_episode_dedup: bool = True
    #: 同集归并时的优选顺序，越靠前优先级越高。
    rss_episode_prefer: tuple[str, ...] = EPISODE_PREFER_DEFAULT
    #: 同集归并的跨轮次时间窗（小时）。0 表示只在单次轮询内归并。
    rss_episode_dedup_window_hours: int = EPISODE_DEDUP_WINDOW_HOURS
    #: RSS 推出新集时顺手把追番进度推到那一集。
    rss_auto_progress: bool = True
    rsshub_base: str = "https://rsshub.app"
    mikan_base: str = "https://mikanani.me"
    # Webhook
    webhook_enabled: bool = False
    webhook_path: str = "bangumi_nexus/notify"
    webhook_token: str = ""
    webhook_notify_watchers: bool = True
    webhook_auto_progress: bool = False
    webhook_port: int = 0
    webhook_bind: str = "0.0.0.0"
    webhook_silent_kinds: tuple[str, ...] = ()
    dedup_window_seconds: int = 300
    # ani-rss 同步
    anirss_enabled: bool = False
    anirss_base: str = ""
    anirss_api_key: str = ""
    anirss_username: str = ""
    anirss_password: str = ""
    #: 自动同步间隔（分钟）。0 表示只在 「/anirss sync」 或 WebUI 点按钮时同步。
    anirss_sync_interval_minutes: int = 60
    #: 同步结果往哪些会话推。留空＝不发通知，只静默写库。
    anirss_sync_targets: tuple[str, ...] = ()
    anirss_sync_watchlist: bool = True
    #: 连 RSS 源一起搬过来。默认关：ani-rss 自己已经在下载了，插件再订阅同一个源
    #: 只是把同一集的通知发两遍。想让机器人也播更新时才打开。
    anirss_sync_subscriptions: bool = False
    anirss_notify_on_change: bool = True
    anirss_verify_tls: bool = True
    # anime1
    anime1_enabled: bool = True
    anime1_refresh_hours: tuple[int, ...] = (1, 13, 22)
    # 投递
    default_platform_id: str = ""
    send_max_retries: int = 3
    send_retry_delay_seconds: float = 2.0
    send_concurrency: int = 3
    # 其它
    gacha_source: str = "auto"
    webui_enabled: bool = True
    webui_theme: str = "midnight"

    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def cache_ttl_seconds(self) -> int:
        return max(0, self.cache_ttl_minutes) * 60

    @property
    def webhook_route(self) -> str:
        return self.webhook_path.strip().strip("/") or "bangumi_nexus/notify"

    def payload(self) -> dict[str, Any]:
        """给 WebUI 用的可 JSON 化视图，敏感字段只回报「是否已设置」。"""

        data = asdict(self)
        data.pop("extras", None)
        for key in SECRET_KEYS:
            data[key] = bool(getattr(self, key, ""))
        # 元组字段一律摊成列表。这里原先是逐个手写的，结果每新增一个 list 型配置
        # 就会漏一行，WebUI 拿到 「()」 直接 JSON 序列化失败 —— 改成按类型扫。
        for key, value in data.items():
            if isinstance(value, tuple):
                data[key] = list(value)
        return data


def load_config(raw: Mapping[str, Any] | Any, *, themes: tuple[str, ...] = ()) -> NexusConfig:
    """从 AstrBotConfig（或任何 Mapping）构造配置快照。永不抛错。"""

    theme_pool = themes or ("midnight",)

    def theme(key: str, default: str) -> str:
        value = _as_str(_get(raw, key, default)).lower()
        return (
            value if value in theme_pool else (default if default in theme_pool else theme_pool[0])
        )

    return NexusConfig(
        card_theme=theme("card_theme", "midnight"),
        card_renderer=_as_choice(_get(raw, "card_renderer", "auto"), RENDERERS, "auto"),
        card_width=_as_int(_get(raw, "card_width", 860), 860, low=640, high=1400),
        bangumi_access_token=_as_str(_get(raw, "bangumi_access_token", "")),
        user_agent=_as_str(_get(raw, "user_agent", ""), DEFAULT_USER_AGENT),
        proxy=_as_str(_get(raw, "proxy", "")),
        http_timeout_seconds=_as_int(_get(raw, "http_timeout_seconds", 20), 20, low=5, high=120),
        http_max_retries=_as_int(_get(raw, "http_max_retries", 3), 3, low=0, high=8),
        cache_ttl_minutes=_as_int(_get(raw, "cache_ttl_minutes", 30), 30, low=0, high=1440),
        max_concurrency=_as_int(_get(raw, "max_concurrency", 5), 5, low=1, high=16),
        search_max_results=_as_int(_get(raw, "search_max_results", 5), 5, low=1, high=20),
        enable_cross_match=_as_bool(_get(raw, "enable_cross_match", True), True),
        translate_summary=_as_bool(_get(raw, "translate_summary", False), False),
        translate_provider_id=_as_str(_get(raw, "translate_provider_id", "")),
        long_reply_as_card=_as_bool(_get(raw, "long_reply_as_card", True), True),
        show_watch_text=_as_bool(_get(raw, "show_watch_text", True), True),
        push_enabled=_as_bool(_get(raw, "push_enabled", False), False),
        push_times=parse_times(_get(raw, "push_time", "08:30")),
        push_targets=_as_list(_get(raw, "push_targets", ())),
        push_max_items=_as_int(_get(raw, "push_max_items", 12), 12, low=1, high=40),
        push_sort_by=_as_choice(_get(raw, "push_sort_by", "score"), SORT_KEYS, "score"),
        push_sort_order=_as_choice(_get(raw, "push_sort_order", "desc"), ("desc", "asc"), "desc"),
        push_min_score=_as_float(_get(raw, "push_min_score", 0), 0.0, low=0.0, high=10.0),
        push_min_doing=_as_int(_get(raw, "push_min_doing", 0), 0, low=0),
        push_only_watchlist=_as_bool(_get(raw, "push_only_watchlist", False), False),
        persona_reply_enabled=_as_bool(_get(raw, "persona_reply_enabled", True), True),
        persona_id=_as_str(_get(raw, "persona_id", "")),
        persona_provider_id=_as_str(_get(raw, "persona_provider_id", "")),
        persona_instruction=_as_str(
            _get(raw, "persona_instruction", ""), DEFAULT_PERSONA_INSTRUCTION
        ),
        persona_max_chars=_as_int(_get(raw, "persona_max_chars", 180), 180, low=30, high=800),
        rss_enabled=_as_bool(_get(raw, "rss_enabled", True), True),
        rss_interval_minutes=_as_int(
            _get(raw, "rss_interval_minutes", 15), 15, low=MIN_RSS_INTERVAL_MINUTES, high=1440
        ),
        rss_max_items_per_poll=_as_int(_get(raw, "rss_max_items_per_poll", 5), 5, low=1, high=30),
        rss_first_poll_silent=_as_bool(_get(raw, "rss_first_poll_silent", True), True),
        rss_pick_source=_as_bool(_get(raw, "rss_pick_source", True), True),
        rss_history_days=_as_int(_get(raw, "rss_history_days", 14), 14, low=1, high=180),
        global_excludes=_as_list(_get(raw, "global_excludes", ())),
        rss_episode_dedup=_as_bool(_get(raw, "rss_episode_dedup", True), True),
        # 优选顺序里的非法标记直接洗掉；洗空了就退回默认序，
        # 否则「填错一个字」等于悄悄关掉归并的偏好，用户只会看到「怎么推的是繁体」。
        rss_episode_prefer=normalize_prefer(_as_list(_get(raw, "rss_episode_prefer", ())))
        or EPISODE_PREFER_DEFAULT,
        # 上限 336 小时（14 天）：再长就会把下一集也算进同一个窗口。
        rss_episode_dedup_window_hours=_as_int(
            _get(raw, "rss_episode_dedup_window_hours", EPISODE_DEDUP_WINDOW_HOURS),
            EPISODE_DEDUP_WINDOW_HOURS,
            low=0,
            high=336,
        ),
        rss_auto_progress=_as_bool(_get(raw, "rss_auto_progress", True), True),
        rsshub_base=_as_str(_get(raw, "rsshub_base", ""), "https://rsshub.app").rstrip("/"),
        mikan_base=_as_str(_get(raw, "mikan_base", ""), "https://mikanani.me").rstrip("/"),
        webhook_enabled=_as_bool(_get(raw, "webhook_enabled", False), False),
        webhook_path=_as_str(_get(raw, "webhook_path", ""), "bangumi_nexus/notify"),
        webhook_token=_as_str(_get(raw, "webhook_token", "")),
        webhook_notify_watchers=_as_bool(_get(raw, "webhook_notify_watchers", True), True),
        webhook_auto_progress=_as_bool(_get(raw, "webhook_auto_progress", False), False),
        webhook_port=_as_int(_get(raw, "webhook_port", 0), 0, low=0, high=65535),
        webhook_bind=_as_str(_get(raw, "webhook_bind", ""), "0.0.0.0"),
        webhook_silent_kinds=_as_list(_get(raw, "webhook_silent_kinds", ())),
        dedup_window_seconds=_as_int(
            _get(raw, "dedup_window_seconds", 300), 300, low=0, high=86400
        ),
        anirss_enabled=_as_bool(_get(raw, "anirss_enabled", False), False),
        # 地址在这里就洗成 「http://host:7789」 的规范形态，服务层与 WebUI 都不必再洗一遍。
        anirss_base=normalize_anirss_base(_as_str(_get(raw, "anirss_base", ""))),
        anirss_api_key=_as_str(_get(raw, "anirss_api_key", "")),
        anirss_username=_as_str(_get(raw, "anirss_username", "")),
        anirss_password=_as_str(_get(raw, "anirss_password", "")),
        # 下限 5 分钟：ani-rss 是局域网服务，但同步会写库并可能发通知，
        # 比 RSS 轮询更「重」，一分钟一次纯属浪费。
        anirss_sync_interval_minutes=_as_int(
            _get(raw, "anirss_sync_interval_minutes", 60), 60, low=0, high=1440
        ),
        anirss_sync_targets=_as_list(_get(raw, "anirss_sync_targets", ())),
        anirss_sync_watchlist=_as_bool(_get(raw, "anirss_sync_watchlist", True), True),
        anirss_sync_subscriptions=_as_bool(_get(raw, "anirss_sync_subscriptions", False), False),
        anirss_notify_on_change=_as_bool(_get(raw, "anirss_notify_on_change", True), True),
        anirss_verify_tls=_as_bool(_get(raw, "anirss_verify_tls", True), True),
        anime1_enabled=_as_bool(_get(raw, "anime1_enabled", True), True),
        anime1_refresh_hours=parse_hours(_get(raw, "anime1_refresh_hours", "1,13,22")),
        # 留空＝运行时自动挑一个启用中的平台实例，见 「nexus/platforms.py」
        default_platform_id=_as_str(_get(raw, "default_platform_id", ""), ""),
        send_max_retries=_as_int(_get(raw, "send_max_retries", 3), 3, low=0, high=8),
        send_retry_delay_seconds=_as_float(
            _get(raw, "send_retry_delay_seconds", 2), 2.0, low=0.2, high=60.0
        ),
        send_concurrency=_as_int(_get(raw, "send_concurrency", 3), 3, low=1, high=10),
        gacha_source=_as_choice(_get(raw, "gacha_source", "auto"), GACHA_SOURCES, "auto"),
        webui_enabled=_as_bool(_get(raw, "webui_enabled", True), True),
        webui_theme=theme("webui_theme", "midnight"),
    )
