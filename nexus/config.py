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

from .constants import DEFAULT_USER_AGENT, MIN_RSS_INTERVAL_MINUTES

RENDERERS = ("auto", "html", "raster", "t2i", "text")
SORT_KEYS = ("score", "doing", "time", "name")
GACHA_SOURCES = ("auto", "yuc", "bangumi")

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
    # 每日播报
    push_enabled: bool = False
    push_times: tuple[str, ...] = ("08:30",)
    push_targets: tuple[str, ...] = ()
    push_max_items: int = 12
    push_sort_by: str = "score"
    push_sort_order: str = "desc"
    push_min_score: float = 0.0
    push_min_doing: int = 0
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
    rss_history_days: int = 14
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
    dedup_window_seconds: int = 300
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
        data["bangumi_access_token"] = bool(self.bangumi_access_token)
        data["webhook_token"] = bool(self.webhook_token)
        data["push_times"] = list(self.push_times)
        data["push_targets"] = list(self.push_targets)
        data["anime1_refresh_hours"] = list(self.anime1_refresh_hours)
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
        push_enabled=_as_bool(_get(raw, "push_enabled", False), False),
        push_times=parse_times(_get(raw, "push_time", "08:30")),
        push_targets=_as_list(_get(raw, "push_targets", ())),
        push_max_items=_as_int(_get(raw, "push_max_items", 12), 12, low=1, high=40),
        push_sort_by=_as_choice(_get(raw, "push_sort_by", "score"), SORT_KEYS, "score"),
        push_sort_order=_as_choice(_get(raw, "push_sort_order", "desc"), ("desc", "asc"), "desc"),
        push_min_score=_as_float(_get(raw, "push_min_score", 0), 0.0, low=0.0, high=10.0),
        push_min_doing=_as_int(_get(raw, "push_min_doing", 0), 0, low=0),
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
        rss_history_days=_as_int(_get(raw, "rss_history_days", 14), 14, low=1, high=180),
        rsshub_base=_as_str(_get(raw, "rsshub_base", ""), "https://rsshub.app").rstrip("/"),
        mikan_base=_as_str(_get(raw, "mikan_base", ""), "https://mikanani.me").rstrip("/"),
        webhook_enabled=_as_bool(_get(raw, "webhook_enabled", False), False),
        webhook_path=_as_str(_get(raw, "webhook_path", ""), "bangumi_nexus/notify"),
        webhook_token=_as_str(_get(raw, "webhook_token", "")),
        webhook_notify_watchers=_as_bool(_get(raw, "webhook_notify_watchers", True), True),
        webhook_auto_progress=_as_bool(_get(raw, "webhook_auto_progress", False), False),
        webhook_port=_as_int(_get(raw, "webhook_port", 0), 0, low=0, high=65535),
        webhook_bind=_as_str(_get(raw, "webhook_bind", ""), "0.0.0.0"),
        dedup_window_seconds=_as_int(
            _get(raw, "dedup_window_seconds", 300), 300, low=0, high=86400
        ),
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
