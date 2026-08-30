"""Dashboard 管理页的后端服务与 HTTP 适配层。

分两层，理由和 「services/」 一样 —— 让有意思的部分能脱离 AstrBot 跑测试：

「NexusService」
    纯 Python。只依赖 「Deps」 和已经装配好的各个服务，收发 「dict」，
    不认识任何 Web 框架，也不认识 HTTP 状态码。
「NexusWebApi」
    薄适配器。读请求、调服务、把结果包成 JSON 或二进制响应，
    并把业务异常翻译成 4xx/5xx。

**配置的单一事实来源**：可编辑项与类型直接从 「_conf_schema.json」 读，
不在这里再抄一份键名表。加配置项时只改 schema，WebUI 自动跟上。

**安全须知**：「register_web_api」 注册的路由活在 AstrBot Dashboard 的鉴权
后面，能打开面板的人就能调这些接口，所以这里不再叠一层凭据校验；但敏感字段
（Bangumi Token、Webhook Token）只回报「是否已设置」，绝不回显明文。

Copyright (C) 2026 Whereis-Alice and AstrBot Plugin Authors.
Licensed under the GNU Affero General Public License v3.0 or later.
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ..catalog import alias_count, category_count, command_count
from ..catalog import payload as catalog_payload
from ..config import GACHA_SOURCES, RENDERERS, SORT_KEYS, NexusConfig
from ..constants import (
    EXCLUDE_PRESETS,
    MAX_SUBSCRIPTIONS_PER_SESSION,
    MAX_WATCHLIST_PER_SESSION,
    PAGE_NAME,
    PLUGIN_BRAND,
    PLUGIN_DISPLAY_NAME,
    PLUGIN_NAME,
    PLUGIN_ROOT,
    PLUGIN_VERSION,
    REPO_URL,
    SOURCES,
    WATCH_STATUS_CN,
)
from ..models import Subject, Subscription, WatchItem
from ..render import (
    build_help_card,
    build_notice_card,
    build_search_card,
    build_watchlist_card,
    resolve_theme,
    theme_keys,
    themes_payload,
)
from ..services.base import (
    PREF_DAILY,
    PREF_EXCLUDES,
    PREF_RENDERER,
    PREF_TARGET,
    PREF_TEMPLATE,
    PREF_THEME,
    Deps,
    excludes_for,
    expand_excludes,
    make_card,
    set_excludes,
)
from ..services.diagnostics import DiagnosticsService
from ..services.gacha import GachaService
from ..services.notifier import Notifier
from ..services.scheduler import Scheduler
from ..services.search import SearchService
from ..services.subscriptions import SubscriptionService
from ..services.watchlist import (
    STATUS_DROPPED,
    STATUS_FINISHED,
    STATUS_PLANNED,
    STATUS_WATCHING,
    WatchlistService,
)
from ..services.webhook import WebhookAuthError, WebhookService
from .listener import WebhookListener

# 界面偏好（主题、密度、当前 tab）存后端：Dashboard 把插件页放在 sandbox iframe 里，
# 没有 localStorage 可用。
STATE_KEY = "webui_state"
STATE_MAX_BYTES = 32_000

# 敏感配置项：可写不可读。
SECRET_KEYS = frozenset({"bangumi_access_token", "webhook_token"})

# 活动日志一次最多回多少条。
LOG_LIMIT = 200

# 预览卡片支持的示例种类。
PREVIEW_KINDS = ("help", "search", "watchlist", "notice")

PREF_KEYS = (PREF_THEME, PREF_RENDERER, PREF_TEMPLATE, PREF_DAILY, PREF_TARGET, PREF_EXCLUDES)

WATCH_STATUSES = (STATUS_WATCHING, STATUS_PLANNED, STATUS_FINISHED, STATUS_DROPPED)


class NexusWebError(Exception):
    """带 HTTP 状态码的用户可见错误。"""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


# ---------------------------------------------------------------------------
# 配置 schema：可编辑项的单一事实来源
# ---------------------------------------------------------------------------


def _load_schema() -> dict[str, dict[str, Any]]:
    """读 「_conf_schema.json」。读不到就返回空表，WebUI 只是少了配置 tab。"""

    try:
        text = (PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8-sig")
        data = json.loads(text)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, Mapping):
        return {}
    return {str(key): dict(value) for key, value in data.items() if isinstance(value, Mapping)}


CONF_SCHEMA = _load_schema()

# 部分配置项在 UI 上更适合用下拉框，而 AstrBot 的 schema 没有 enum 字段，
# 于是在这里补一张候选表。键名对齐 schema。
_CHOICES: dict[str, tuple[str, ...]] = {
    "card_renderer": RENDERERS,
    "push_sort_by": SORT_KEYS,
    "push_sort_order": ("desc", "asc"),
    "gacha_source": GACHA_SOURCES,
}

# 配置项分组，决定 WebUI 里的显示顺序与折叠分区。
CONF_GROUPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("render", "卡片与渲染", ("card_theme", "card_renderer", "card_width", "long_reply_as_card")),
    (
        "network",
        "网络与缓存",
        (
            "bangumi_access_token",
            "user_agent",
            "proxy",
            "http_timeout_seconds",
            "http_max_retries",
            "cache_ttl_minutes",
            "max_concurrency",
        ),
    ),
    (
        "search",
        "搜索与匹配",
        ("search_max_results", "enable_cross_match", "translate_summary", "translate_provider_id"),
    ),
    (
        "push",
        "每日播报",
        (
            "push_enabled",
            "push_time",
            "push_targets",
            "push_max_items",
            "push_sort_by",
            "push_sort_order",
            "push_min_score",
            "push_min_doing",
        ),
    ),
    (
        "persona",
        "人格转述",
        (
            "persona_reply_enabled",
            "persona_id",
            "persona_provider_id",
            "persona_instruction",
            "persona_max_chars",
        ),
    ),
    (
        "rss",
        "RSS 订阅",
        (
            "rss_enabled",
            "rss_interval_minutes",
            "rss_max_items_per_poll",
            "rss_first_poll_silent",
            "rss_history_days",
            "rsshub_base",
            "mikan_base",
        ),
    ),
    (
        "webhook",
        "Webhook 接入",
        (
            "webhook_enabled",
            "webhook_path",
            "webhook_token",
            "webhook_port",
            "webhook_bind",
            "webhook_notify_watchers",
            "webhook_auto_progress",
            "dedup_window_seconds",
        ),
    ),
    ("anime1", "在线观看索引", ("anime1_enabled", "anime1_refresh_hours")),
    (
        "delivery",
        "消息投递",
        (
            "default_platform_id",
            "send_max_retries",
            "send_retry_delay_seconds",
            "send_concurrency",
        ),
    ),
    ("misc", "其它", ("gacha_source", "webui_enabled", "webui_theme")),
)


def config_schema_payload() -> list[dict[str, Any]]:
    """把 schema 整理成分组后的表单描述，给前端直接渲染。"""

    seen: set[str] = set()
    groups: list[dict[str, Any]] = []
    for key, title, members in CONF_GROUPS:
        fields = []
        for name in members:
            entry = CONF_SCHEMA.get(name)
            if entry is None:
                continue
            seen.add(name)
            fields.append(_field_payload(name, entry))
        if fields:
            groups.append({"key": key, "title": title, "fields": fields})
    leftovers = [
        _field_payload(name, entry) for name, entry in CONF_SCHEMA.items() if name not in seen
    ]
    if leftovers:
        groups.append({"key": "other", "title": "未分组", "fields": leftovers})
    return groups


def _field_payload(name: str, entry: Mapping[str, Any]) -> dict[str, Any]:
    kind = str(entry.get("type") or "string")
    data: dict[str, Any] = {
        "key": name,
        "label": str(entry.get("description") or name),
        "type": kind,
        "hint": str(entry.get("hint") or ""),
        "default": entry.get("default"),
        "secret": name in SECRET_KEYS,
    }
    if name == "card_theme" or name == "webui_theme":
        data["choices"] = list(theme_keys())
    elif name in _CHOICES:
        data["choices"] = list(_CHOICES[name])
    return data


def coerce_config_value(key: str, value: Any) -> Any:
    """按 schema 声明的类型收敛一个提交上来的值。

    这里只做类型转换，范围夹紧交给 「config.load_config」 —— 那边已经有一套
    完整的上下限规则，不该在两个地方各写一份。
    """

    entry = CONF_SCHEMA.get(key)
    if entry is None:
        raise NexusWebError("未知配置项：" + key)
    kind = str(entry.get("type") or "string")
    if kind == "bool":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on", "开"}
    if kind == "int":
        try:
            return int(float(value))
        except (TypeError, ValueError) as error:
            raise NexusWebError(key + " 需要一个整数") from error
    if kind == "float":
        try:
            return float(value)
        except (TypeError, ValueError) as error:
            raise NexusWebError(key + " 需要一个数字") from error
    if kind == "list":
        if isinstance(value, str):
            return [part.strip() for part in value.replace("\n", ",").split(",") if part.strip()]
        if isinstance(value, Sequence):
            return [str(item).strip() for item in value if str(item).strip()]
        raise NexusWebError(key + " 需要一个列表")
    if kind == "object":
        if isinstance(value, Mapping):
            return dict(value)
        raise NexusWebError(key + " 需要一个对象")
    return str(value if value is not None else "")


# ---------------------------------------------------------------------------
# 装配
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Wiring:
    """WebUI 需要用到的一组已装配好的服务。

    「main.py」 构造完所有服务后把它们塞进来，避免 「NexusService」 自己
    去 new 服务 —— 那样 WebUI 和聊天指令就会各拿一份状态，去重表、
    调度器计数全都会分裂。
    """

    search: SearchService
    watchlist: WatchlistService
    subs: SubscriptionService
    gacha: GachaService
    notifier: Notifier
    scheduler: Scheduler
    webhook: WebhookService
    diagnostics: DiagnosticsService
    listener: WebhookListener | None = None
    config_writer: Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any]]] | None = None


# ---------------------------------------------------------------------------
# 纯 Python 服务层
# ---------------------------------------------------------------------------


class NexusService:
    """WebUI 的全部业务逻辑。不认识 HTTP，也不认识 Quart。

    所有方法都是 async 且只收发可 JSON 化的基本类型（「render_preview」 例外，
    它回二进制），这样 「NexusWebApi」 只剩「解析请求 → 调这里 → 包响应」三步，
    也让这些逻辑能在 pytest 里直接 await。
    """

    def __init__(self, deps: Deps, wiring: Wiring) -> None:
        self._deps = deps
        self._wiring = wiring
        self._started = time.time()

    # -- 便捷访问 ---------------------------------------------------------
    @property
    def deps(self) -> Deps:
        return self._deps

    @property
    def wiring(self) -> Wiring:
        return self._wiring

    @property
    def conf(self) -> NexusConfig:
        return self._deps.conf

    def attach_listener(self, listener: WebhookListener | None) -> None:
        """替换概览里引用的独立监听对象。

        端口 / 路径 / 令牌变更时 「main.py」 会整体重建 「WebhookListener」，
        旧对象已经 stop 掉；不换引用的话 WebUI 概览会一直报旧端口的状态。
        """

        self._wiring = replace(self._wiring, listener=listener)

    def _prefix(self) -> str:
        """取 AstrBot 的第一个唤醒前缀，用于指令表展示。

        指令表里的 「/查番」 在把 wake_prefix 改成 「!」 的实例上会误导用户，
        所以这里跟着实例配置走，取不到就退回 「/」。
        """

        try:
            config = self._deps.context.get_config()
            prefixes = config.get("wake_prefix") if hasattr(config, "get") else None
        except Exception:  # noqa: BLE001 - 拿不到配置不该让整页 500
            return "/"
        if isinstance(prefixes, str):
            return prefixes or "/"
        if isinstance(prefixes, Sequence):
            for item in prefixes:
                text = str(item or "").strip()
                if text:
                    return text
        return "/"

    # -- 元信息 -----------------------------------------------------------
    async def meta(self) -> dict[str, Any]:
        """一次性把「不会变的东西」全给前端，省掉一堆小请求。"""

        prefix = self._prefix()
        return {
            "name": PLUGIN_NAME,
            "brand": PLUGIN_BRAND,
            "display_name": PLUGIN_DISPLAY_NAME,
            "version": PLUGIN_VERSION,
            "page": PAGE_NAME,
            "repo": REPO_URL,
            "prefix": prefix,
            "themes": themes_payload(),
            "options": {
                "renderers": list(RENDERERS),
                "sort_keys": list(SORT_KEYS),
                "sort_orders": ["desc", "asc"],
                "gacha_sources": list(GACHA_SOURCES),
                "preview_kinds": list(PREVIEW_KINDS),
                "watch_statuses": [
                    {"key": key, "label": WATCH_STATUS_CN.get(key, key)} for key in WATCH_STATUSES
                ],
                "pref_keys": list(PREF_KEYS),
            },
            "sources": [
                {
                    "key": source.key,
                    "name": source.name,
                    "role": source.role,
                    "home": source.home,
                    "license": source.license_note,
                }
                for source in SOURCES
            ],
            "catalog": catalog_payload(prefix),
            "counts": {
                "commands": command_count(),
                "categories": category_count(),
                "aliases": alias_count(),
            },
            "limits": {
                "subscriptions_per_session": MAX_SUBSCRIPTIONS_PER_SESSION,
                "watchlist_per_session": MAX_WATCHLIST_PER_SESSION,
                "state_bytes": STATE_MAX_BYTES,
                "logs": LOG_LIMIT,
            },
            "config_groups": config_schema_payload(),
            "webui_theme": self.conf.webui_theme,
            "writable": self._wiring.config_writer is not None,
        }

    # -- 概览 -------------------------------------------------------------
    async def overview(self) -> dict[str, Any]:
        """概览页的一次性快照：配置 + 各子系统自报的计数。

        每个 「stats()」 都是内存读数，唯一会碰磁盘的是 「store.stats()」，
        所以整页开销可以忽略，前端可以放心轮询。
        """

        wiring = self._wiring
        listener = wiring.listener
        return {
            "config": self.conf.payload(),
            "store": await self._deps.store.stats(),
            "http": self._deps.http.stats(),
            "sources": self._deps.hub.stats(),
            "render": self._deps.engine.stats(),
            "scheduler": wiring.scheduler.snapshot(),
            "notifier": wiring.notifier.stats(),
            "webhook": wiring.webhook.stats(),
            "subscriptions": wiring.subs.stats(),
            "gacha": wiring.gacha.stats(),
            "listener": listener.stats() if listener is not None else {"running": False},
            "activity": self._deps.activity.counters(),
            "uptime": max(0.0, time.time() - self._started),
            "now": time.time(),
        }

    # -- 活动日志 ---------------------------------------------------------
    async def logs(self, limit: int = 80, level: str = "") -> dict[str, Any]:
        capped = max(1, min(LOG_LIMIT, int(limit or 80)))
        entries = self._deps.activity.recent(capped, level=level or None)
        return {"entries": entries, "counters": self._deps.activity.counters()}

    async def clear_logs(self) -> dict[str, Any]:
        self._deps.activity.clear()
        return {"cleared": True}

    # -- 界面偏好 ---------------------------------------------------------
    async def state(self) -> dict[str, Any]:
        """读界面偏好。iframe 里没有 localStorage，只能存后端。"""

        raw = await self._deps.store.kv_get(STATE_KEY, {})
        return dict(raw) if isinstance(raw, Mapping) else {}

    async def save_state(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise NexusWebError("界面偏好必须是一个对象")
        encoded = json.dumps(payload, ensure_ascii=False)
        if len(encoded.encode("utf-8")) > STATE_MAX_BYTES:
            raise NexusWebError("界面偏好太大了，请精简后再保存")
        await self._deps.store.kv_set(STATE_KEY, dict(payload))
        return {"saved": True}

    # -- 追番清单 ---------------------------------------------------------
    async def watchlist(self, umo: str = "", status: str = "") -> dict[str, Any]:
        if status and status not in WATCH_STATUSES:
            raise NexusWebError("未知的追番状态：" + status)
        items = await self._deps.store.list_watch(umo, status=status)
        return {
            "items": [_watch_payload(item) for item in items],
            "total": len(items),
            "umo": umo,
            "status": status,
        }

    async def watch_action(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """改一条追番记录。op：progress / status / note / score / delete。"""

        op = str(payload.get("op") or "").strip().lower()
        watch_id = _as_int(payload.get("id"), label="追番记录 ID")
        if op == "delete":
            ok = await self._deps.store.delete_watch(watch_id)
            if not ok:
                raise NexusWebError("这条追番记录已经不在了", status_code=404)
            self._deps.activity.info("webui", "删除追番记录 #" + str(watch_id))
            return {"ok": True, "op": op, "id": watch_id}

        if op == "progress":
            fields: dict[str, Any] = {
                "progress": max(0, _as_int(payload.get("value"), label="集数"))
            }
        elif op == "status":
            value = str(payload.get("value") or "").strip().lower()
            if value not in WATCH_STATUSES:
                raise NexusWebError("未知的追番状态：" + value)
            fields = {"status": value}
        elif op == "note":
            fields = {"note": str(payload.get("value") or "")[:200]}
        elif op == "score":
            try:
                score = float(payload.get("value") or 0)
            except (TypeError, ValueError) as error:
                raise NexusWebError("评分需要是一个数字") from error
            fields = {"score": max(0.0, min(10.0, score))}
        elif op == "total":
            fields = {"total": max(0, _as_int(payload.get("value"), label="总集数"))}
        else:
            raise NexusWebError("不支持的操作：" + (op or "(空)"))

        ok = await self._deps.store.update_watch(watch_id, **fields)
        if not ok:
            raise NexusWebError("这条追番记录已经不在了", status_code=404)
        self._deps.activity.info("webui", "更新追番记录 #" + str(watch_id) + " " + op)
        return {"ok": True, "op": op, "id": watch_id, "fields": fields}

    # -- 订阅 -------------------------------------------------------------
    async def subscriptions(self, umo: str = "") -> dict[str, Any]:
        subs = await self._deps.store.list_subscriptions(umo)
        return {
            "items": [_sub_payload(sub) for sub in subs],
            "total": len(subs),
            "umo": umo,
            "enabled": sum(1 for sub in subs if sub.enabled),
        }

    async def sub_action(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """订阅操作。全部复用聊天指令那套服务，保证两条入口行为一致。

        WebUI 不该有「只有面板才能做」的隐藏逻辑 —— 那是 bug 的温床。
        """

        op = str(payload.get("op") or "").strip().lower()
        umo = str(payload.get("umo") or "").strip()
        value = str(payload.get("value") or "").strip()
        subs = self._wiring.subs

        if op in {"add", "remove", "test"} and not value:
            raise NexusWebError("请先填写内容")
        if op in {"add", "remove", "clear", "enable_all", "disable_all"} and not umo:
            raise NexusWebError("请先选择一个会话")

        if op == "add":
            reply = await subs.add(umo, value)
        elif op == "remove":
            reply = await subs.remove(umo, value)
        elif op == "clear":
            reply = await subs.clear(umo)
        elif op == "test":
            reply = await subs.test(umo, value)
        elif op == "enable_all":
            reply = await subs.set_enabled(umo, True)
        elif op == "disable_all":
            reply = await subs.set_enabled(umo, False)
        elif op == "toggle":
            sub_id = _as_int(payload.get("id"), label="订阅 ID")
            enabled = bool(payload.get("enabled"))
            # 「set_subscription_state」 对不存在的 id 是静默 no-op，所以先确认一下，
            # 否则前端点了半天开关却毫无反应，还以为是自己网不好。
            existing = await self._deps.store.list_subscriptions()
            if not any(sub.id == sub_id for sub in existing):
                raise NexusWebError("这条订阅已经不在了", status_code=404)
            await self._deps.store.set_subscription_state(sub_id, enabled=enabled)
            self._deps.activity.info(
                "webui", ("启用" if enabled else "暂停") + "订阅 #" + str(sub_id)
            )
            return {"ok": True, "op": op, "id": sub_id, "enabled": enabled}
        else:
            raise NexusWebError("不支持的操作：" + (op or "(空)"))

        self._deps.activity.info("webui", "订阅操作 " + op + " → " + (umo or "全局"))
        return {"ok": True, "op": op, "message": reply.text}

    async def sub_sources(self, name: str) -> dict[str, Any]:
        """列出一部番在 Mikan 上的字幕组，供面板上「选源」用。

        为什么面板也要有这一步：只按番名订阅拿到的是 Mikan 的关键词搜索源，
        一集番会被七八个字幕组各推一遍。跟聊天侧走同一个 「pick_options」，
        两处看到的候选完全一致。
        """
        name = str(name or "").strip()
        if not name:
            raise NexusWebError("请先填写番剧名称")
        options = await self._wiring.subs.pick_options(name)
        return {
            "name": name,
            "total": len(options),
            "items": [
                {
                    "index": option.index,
                    "label": option.label,
                    "detail": option.detail,
                    "tags": list(option.tags),
                    "group_id": option.group_id,
                    "url": option.url,
                }
                for option in options
            ],
        }

    async def excludes(self, umo: str = "") -> dict[str, Any]:
        """全局排除项：可勾的预设清单 + 当前会话已勾的 + 展开后的实际过滤词。"""

        chosen = await excludes_for(self._deps, umo) if umo else ()
        return {
            "umo": umo,
            "presets": [{"name": name, "words": list(words)} for name, words in EXCLUDE_PRESETS],
            "chosen": list(chosen),
            "expanded": list(expand_excludes(chosen)),
        }

    async def save_excludes(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """保存勾选，可选地立刻回写到该会话已有的订阅上。

        「apply」 默认关：改了全局清单不代表用户想动已经订好的老订阅，
        那属于批量覆盖，必须是明确的一次点击。
        """
        umo = str(payload.get("umo") or "").strip()
        if not umo:
            raise NexusWebError("请先选择一个会话")
        raw = payload.get("values")
        if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
            raise NexusWebError("排除项要给一个数组")
        chosen = await set_excludes(self._deps, umo, [str(item) for item in raw])
        expanded = expand_excludes(chosen)
        applied = 0
        if bool(payload.get("apply")):
            applied = await self._deps.store.apply_excludes(umo, expanded)
        self._deps.activity.info(
            "webui",
            f"排除项更新（{len(chosen)} 项）→ {umo}" + (f"，回写 {applied} 条" if applied else ""),
        )
        return {
            "ok": True,
            "umo": umo,
            "chosen": list(chosen),
            "expanded": list(expanded),
            "applied": applied,
        }

    # -- 会话 -------------------------------------------------------------
    async def sessions(self) -> dict[str, Any]:
        """把出现过的会话汇总成一张表，前端用它做下拉选择。

        会话 ID（unified_msg_origin）不可能让用户手打，所以这里从追番记录、
        订阅、会话偏好三处并集出所有见过的会话。
        """

        store = self._deps.store
        watch = await store.list_watch()
        subs = await store.list_subscriptions()
        prefs = await store.all_prefs()

        table: dict[str, dict[str, Any]] = {}

        def slot(umo: str) -> dict[str, Any]:
            if umo not in table:
                table[umo] = {"umo": umo, "watch": 0, "subscriptions": 0, "prefs": {}}
            return table[umo]

        for item in watch:
            slot(item.umo)["watch"] += 1
        for sub in subs:
            slot(sub.umo)["subscriptions"] += 1
        for row in prefs:
            umo = str(row.get("umo") or "")
            key = str(row.get("key") or "")
            if not umo or key not in PREF_KEYS:
                continue
            slot(umo)["prefs"][key] = str(row.get("value") or "")

        items = sorted(
            table.values(),
            key=lambda row: (-int(row["watch"]) - int(row["subscriptions"]), str(row["umo"])),
        )
        return {"items": items, "total": len(items)}

    # -- 推送目标 ---------------------------------------------------------
    async def targets(self) -> dict[str, Any]:
        """播报目标全景：配置里写死的 + 会话自己订阅的 + 最终生效的并集。"""

        conf = self.conf
        configured = list(conf.push_targets)
        resolved = list(self._wiring.notifier.resolve_targets(configured))
        effective = list(await self._wiring.scheduler.push_targets())
        opted_in = await self._deps.store.sessions_with_pref(PREF_DAILY, "1")
        return {
            "configured": configured,
            "resolved": resolved,
            "effective": effective,
            "opted_in": list(opted_in),
            "push_enabled": conf.push_enabled,
            "push_times": list(conf.push_times),
            "default_platform_id": conf.default_platform_id,
        }

    async def save_targets(self, values: Sequence[Any]) -> dict[str, Any]:
        if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
            raise NexusWebError("推送目标必须是一个列表")
        cleaned = [str(item).strip() for item in values if str(item).strip()]
        result = await self._write_config({"push_targets": cleaned})
        return {"ok": True, "configured": cleaned, "applied": result}

    # -- 手动触发 ---------------------------------------------------------
    async def push_now(self, targets: Sequence[Any] = (), weekday: int = 0) -> dict[str, Any]:
        picked = tuple(str(item).strip() for item in targets or () if str(item).strip())
        sent = await self._wiring.scheduler.run_daily(
            targets=picked, weekday=max(0, min(7, int(weekday or 0)))
        )
        return {"ok": True, "sent": sent}

    async def poll_now(self, umo: str = "") -> dict[str, Any]:
        pushed = await self._wiring.scheduler.run_rss(umo=str(umo or "").strip(), force=True)
        return {"ok": True, "pushed": pushed}

    async def refresh_anime1(self) -> dict[str, Any]:
        count = await self._wiring.scheduler.refresh_anime1(force=True)
        return {"ok": True, "entries": count}

    # -- Webhook ----------------------------------------------------------
    async def handle_webhook(
        self,
        raw: Any,
        *,
        token: str = "",
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """转发一次 Webhook 请求给 「WebhookService」。

        Dashboard 那条通道只用于同源自测（它受 JWT 保护，外部程序进不来）；
        真正给 AutoBangumi 回调的是 「webhook_port」 独立监听。两条通道共用
        这一个入口，保证去重表和计数只有一份。
        """

        return await self._wiring.webhook.handle(raw, token=token, headers=headers)

    async def webhook_selftest(self) -> dict[str, Any]:
        return await self._wiring.webhook.selftest()

    # -- 诊断 / 娱乐 / 搜索 ------------------------------------------------
    async def diagnose(self) -> dict[str, Any]:
        probes = await self._wiring.diagnostics.run_probes()
        rows = [
            {"name": name, "ok": bool(ok), "detail": detail, "elapsed": round(float(elapsed), 3)}
            for name, ok, detail, elapsed in probes
        ]
        healthy = sum(1 for row in rows if row["ok"])
        return {"probes": rows, "healthy": healthy, "total": len(rows)}

    async def gacha_preview(self, genre: str = "") -> dict[str, Any]:
        reply = await self._wiring.gacha.draw("", str(genre or "").strip())
        return {"text": reply.text, "notes": list(reply.notes)}

    async def search(self, keyword: str, limit: int = 8) -> dict[str, Any]:
        """给 WebUI 用的轻量搜索：只回展示与「加入追番」需要的字段。"""

        query = str(keyword or "").strip()
        if not query:
            raise NexusWebError("请先输入关键词")
        capped = max(1, min(20, int(limit or 8)))
        if query.isdigit():
            subject = await self._wiring.search.resolve(query)
            subjects = [subject] if subject is not None else []
        else:
            subjects = list(await self._deps.hub.bangumi.search(query, limit=capped))
        return {
            "keyword": query,
            "items": [
                {
                    "id": item.id,
                    "name": item.name,
                    "name_cn": item.name_cn,
                    "display_name": item.display_name,
                    "type_label": item.type_label,
                    "score": item.score,
                    "score_label": item.score_label,
                    "doing": item.doing,
                    "air_date": item.air_date,
                    "weekday_label": item.weekday_label,
                    "eps": item.total_episodes or item.eps,
                    "image": item.image,
                    "url": item.url,
                    "tags": list(item.tags[:6]),
                }
                for item in subjects
            ],
        }

    async def add_watch(self, umo: str, query: str) -> dict[str, Any]:
        """从 WebUI 直接加追番，顺带把可用的 RSS 源建议一起回去。"""

        session = str(umo or "").strip()
        if not session:
            raise NexusWebError("请先选择一个会话")
        reply, match = await self._wiring.watchlist.add(session, str(query or "").strip())
        suggestions = (
            [{"label": label, "url": url} for label, url in self._wiring.subs.suggest(match)]
            if match is not None
            else []
        )
        return {"ok": True, "message": reply.text, "suggestions": suggestions}

    # -- 导入导出 ---------------------------------------------------------
    async def export(self, umo: str = "") -> dict[str, Any]:
        return await self._deps.store.export_all(str(umo or "").strip())

    async def import_payload(self, payload: Mapping[str, Any], umo: str = "") -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise NexusWebError("导入内容必须是一个对象")
        counts = await self._deps.store.import_all(dict(payload), umo=str(umo or "").strip())
        self._deps.activity.info("webui", "导入数据 " + json.dumps(counts, ensure_ascii=False))
        return {"ok": True, "counts": counts}

    # -- 配置 -------------------------------------------------------------
    async def config_view(self) -> dict[str, Any]:
        conf = self.conf
        return {
            "groups": config_schema_payload(),
            "values": conf.payload(),
            "secrets": {key: bool(getattr(conf, key, "")) for key in sorted(SECRET_KEYS)},
            "writable": self._wiring.config_writer is not None,
        }

    async def save_config(self, patch: Mapping[str, Any]) -> dict[str, Any]:
        """写回配置。空字符串对敏感字段视为「不改」，避免脱敏回显把 token 抹掉。"""

        if not isinstance(patch, Mapping):
            raise NexusWebError("配置补丁必须是一个对象")
        cleaned: dict[str, Any] = {}
        for key, value in patch.items():
            name = str(key)
            if name in SECRET_KEYS and (value is None or str(value) == ""):
                continue
            cleaned[name] = coerce_config_value(name, value)
        if not cleaned:
            raise NexusWebError("没有需要保存的改动")
        applied = await self._write_config(cleaned)
        return {
            "ok": True,
            "changed": sorted(name for name in cleaned if name not in SECRET_KEYS),
            "applied": applied,
            "values": self.conf.payload(),
        }

    async def _write_config(self, patch: Mapping[str, Any]) -> dict[str, Any]:
        writer = self._wiring.config_writer
        if writer is None:
            raise NexusWebError("当前运行环境不支持从面板改配置", status_code=503)
        result = await writer(patch)
        self._deps.activity.info("webui", "更新配置：" + "、".join(sorted(patch)))
        return dict(result) if isinstance(result, Mapping) else {}

    # -- 主题与指令表 -----------------------------------------------------
    async def themes(self) -> dict[str, Any]:
        return {
            "themes": themes_payload(),
            "current": self.conf.card_theme,
            "webui": self.conf.webui_theme,
        }

    async def commands(self) -> dict[str, Any]:
        prefix = self._prefix()
        return {
            "prefix": prefix,
            "categories": catalog_payload(prefix),
            "counts": {
                "commands": command_count(),
                "categories": category_count(),
                "aliases": alias_count(),
            },
        }

    # -- 卡片预览 ---------------------------------------------------------
    async def render_preview(
        self, theme: str = "", kind: str = "help", renderer: str = ""
    ) -> tuple[bytes, str]:
        """渲染一张示例卡片，返回 「(bytes, mime)」。

        主题选择器需要「所见即所得」，而真实卡片依赖网络数据；所以这里用固定的
        假数据走同一条渲染管线 —— 预览的字节和真实推送出去的字节由同一套模板
        和同一个 「CardEngine」 产出，不会出现「面板好看、群里难看」。
        """

        wanted = str(kind or "help").strip().lower()
        if wanted not in PREVIEW_KINDS:
            raise NexusWebError("未知的预览种类：" + wanted)
        conf = self.conf
        resolved = resolve_theme(theme or conf.card_theme)
        request = _preview_request(wanted, resolved, self._prefix())

        if renderer:
            picked = str(renderer).strip().lower()
            if picked not in RENDERERS:
                raise NexusWebError("未知的渲染器：" + picked)
            conf = replace(conf, card_renderer=picked)

        card = await self._deps.engine.render(request, conf)
        data = await self._card_bytes(card)
        if not data:
            raise NexusWebError("渲染失败了，看看 AstrBot 日志里的浏览器报错", status_code=503)
        return data, "image/png"

    async def _card_bytes(self, card: Any) -> bytes:
        """从渲染结果里把图片字节挖出来。t2i 只给 URL，需要再抓一次。"""

        path = str(getattr(card, "image_path", "") or "")
        if path:
            try:
                return Path(path).read_bytes()
            except OSError:
                pass
        url = str(getattr(card, "image_url", "") or "")
        if url.startswith("data:"):
            _, _, encoded = url.partition(",")
            try:
                return base64.b64decode(encoded)
            except ValueError:
                return b""
        if url:
            try:
                return await self._deps.http.fetch_bytes(url, limit=8 * 1024 * 1024)
            except Exception:  # noqa: BLE001 - 预览失败不该 500
                return b""
        return b""

    async def preview_payload(
        self, theme: str = "", kind: str = "help", renderer: str = ""
    ) -> dict[str, Any]:
        """给 「bridge.apiGet」 用的 JSON 版预览：图片以 data URI 内联。"""

        data, mime = await self.render_preview(theme, kind, renderer)
        return {
            "theme": (theme or self.conf.card_theme),
            "kind": kind or "help",
            "mime": mime,
            "bytes": len(data),
            "data_uri": "data:" + mime + ";base64," + base64.b64encode(data).decode("ascii"),
        }


# ---------------------------------------------------------------------------
# 载荷整形与预览假数据
# ---------------------------------------------------------------------------


def _as_int(value: Any, *, label: str) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError) as error:
        raise NexusWebError(label + " 需要是一个整数") from error


def _watch_payload(item: WatchItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "umo": item.umo,
        "subject_id": item.subject_id,
        "title": item.title,
        "status": item.status,
        "status_label": item.status_label,
        "progress": item.progress,
        "total": item.total,
        "progress_label": item.progress_label,
        "percent": item.percent,
        "score": item.score,
        "cover": item.cover,
        "weekday": item.weekday,
        "note": item.note,
        "updated_at": item.updated_at,
    }


def _sub_payload(sub: Subscription) -> dict[str, Any]:
    return {
        "id": sub.id,
        "umo": sub.umo,
        "name": sub.name,
        "url": sub.url,
        "enabled": sub.enabled,
        "subject_id": sub.subject_id,
        "keywords": list(sub.keywords),
        "excludes": list(sub.excludes),
        "last_checked": sub.last_checked,
        "last_item": sub.last_item,
        "error": sub.error,
        "created_at": sub.created_at,
    }


_PREVIEW_SUBJECTS = (
    Subject(
        id=302286,
        name="蒼のアネモイ",
        name_cn="苍之阿涅摩伊",
        summary="风车小镇的少女为了再看一次天空，把废弃的滑翔机重新拼了起来。",
        image="",
        score=8.4,
        rank=142,
        rating_total=5312,
        air_date="2026-07-05",
        air_weekday=7,
        total_episodes=13,
        tags=("原创", "治愈", "飞行", "2026年7月"),
        collection={"doing": 4821, "wish": 9310},
    ),
    Subject(
        id=411902,
        name="星屑クロニクル",
        name_cn="星屑编年史",
        summary="一支拾荒队在轨道墓场里翻找上个纪元的记忆。",
        score=7.9,
        rating_total=2140,
        air_date="2026-07-08",
        air_weekday=3,
        total_episodes=12,
        tags=("科幻", "群像"),
        collection={"doing": 1980},
    ),
    Subject(
        id=488013,
        name="Bakery on the Hill",
        name_cn="山丘上的面包店",
        score=7.2,
        rating_total=860,
        air_date="2026-07-02",
        air_weekday=4,
        total_episodes=24,
        tags=("日常", "美食"),
        collection={"doing": 640},
    ),
)

_PREVIEW_WATCH = (
    WatchItem(
        id=1,
        umo="preview",
        subject_id=302286,
        title="苍之阿涅摩伊",
        progress=7,
        total=13,
        score=9.0,
        weekday=7,
    ),
    WatchItem(
        id=2,
        umo="preview",
        subject_id=411902,
        title="星屑编年史",
        progress=12,
        total=12,
        status="finished",
        score=8.0,
        weekday=3,
    ),
    WatchItem(
        id=3,
        umo="preview",
        subject_id=488013,
        title="山丘上的面包店",
        status="planned",
        total=24,
        weekday=4,
    ),
)


def _preview_request(kind: str, theme: Any, prefix: str) -> Any:
    """按种类造预览卡片的 「CardRequest」。"""

    if kind == "help":
        html = build_help_card(theme, prefix=prefix, version=PLUGIN_VERSION)
        return make_card(
            html,
            plain=PLUGIN_DISPLAY_NAME + " 指令总览",
            title=PLUGIN_DISPLAY_NAME,
            theme=theme.key,
        )
    if kind == "search":
        html = build_search_card(theme, "阿涅摩伊", _PREVIEW_SUBJECTS, version=PLUGIN_VERSION)
        return make_card(html, plain="搜索结果预览", title="搜索结果", theme=theme.key)
    if kind == "watchlist":
        html = build_watchlist_card(
            theme,
            _PREVIEW_WATCH,
            owner="示例会话",
            airing={302286: "今晚 23:30 第 8 集"},
            version=PLUGIN_VERSION,
        )
        return make_card(html, plain="追番清单预览", title="追番清单", theme=theme.key)
    html = build_notice_card(
        theme,
        eyebrow="新集更新",
        title="苍之阿涅摩伊 第 8 集",
        lines=("字幕组：示例字幕组", "分辨率：1080P 简日双语", "大小：742.1 MB"),
        subtitle="每周日 23:30 放送",
        persona_text="第八集来了哦，这次的滑翔镜头一定要看完再睡。",
        chips=("Mikan", "1080P", "简日"),
        version=PLUGIN_VERSION,
    )
    return make_card(html, plain="更新通知预览", title="更新通知", theme=theme.key)


# ---------------------------------------------------------------------------
# HTTP 适配层
# ---------------------------------------------------------------------------


def _detect_web_backend() -> tuple[str, Any]:
    """挑一个可用的 Web 运行时。

    AstrBot 较新的版本计划提供框架无关的 「astrbot.api.web」；4.25 还没有，
    所以主力路径其实是 Quart 的请求上下文。两条都留着，将来 SDK 补上时
    这里不用改。
    """

    try:
        from astrbot.api import web as astrbot_web
    except Exception:  # noqa: BLE001 - 可选依赖
        astrbot_web = None
    if astrbot_web is not None and hasattr(astrbot_web, "json_response"):
        return "astrbot", astrbot_web
    try:
        import quart
    except Exception:  # noqa: BLE001 - 裸解释器里跑测试时会走到这
        return "none", None
    return "quart", quart


_WEB_BACKEND, _WEB = _detect_web_backend()


def _require_backend() -> Any:
    if _WEB is None:  # pragma: no cover - 只在没有 Web 框架的环境出现
        raise NexusWebError("当前运行环境没有可用的 Web 框架", status_code=503)
    return _WEB


def _json(data: Any, *, status_code: int = 200, headers: Mapping[str, str] | None = None) -> Any:
    web = _require_backend()
    if _WEB_BACKEND == "astrbot":
        return web.json_response(data, status_code=status_code, headers=dict(headers or {}) or None)
    return web.Response(
        json.dumps(data, ensure_ascii=False),
        status=status_code,
        content_type="application/json; charset=utf-8",
        headers=dict(headers or {}),
    )


def _error(message: str, *, status_code: int = 400) -> Any:
    web = _require_backend()
    if _WEB_BACKEND == "astrbot" and hasattr(web, "error_response"):
        return web.error_response(message, status_code=status_code)
    return _json({"status": "error", "message": message, "data": None}, status_code=status_code)


def _content_disposition(filename: str, *, inline: bool = False) -> str:
    fallback = filename.encode("ascii", "ignore").decode("ascii") or "download.bin"
    quoted = quote(filename, safe="")
    kind = "inline" if inline else "attachment"
    return kind + '; filename="' + fallback + "\"; filename*=UTF-8''" + quoted


def _binary(data: bytes, *, content_type: str, filename: str, inline: bool = False) -> Any:
    web = _require_backend()
    headers = {
        "Content-Disposition": _content_disposition(filename, inline=inline),
        "Content-Length": str(len(data)),
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }
    if _WEB_BACKEND == "astrbot" and hasattr(web, "stream_response"):
        return web.stream_response(iter([data]), content_type=content_type, headers=headers)
    return web.Response(data, content_type=content_type, headers=headers)


def _request_obj() -> Any:
    return _require_backend().request


def _query(key: str, default: Any = None) -> Any:
    holder = _request_obj()
    bag = holder.query if _WEB_BACKEND == "astrbot" else holder.args
    return bag.get(key, default)


def _query_int(key: str, default: int = 0) -> int:
    raw = _query(key, None)
    if raw in (None, ""):
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


async def _json_body() -> Mapping[str, Any]:
    holder = _request_obj()
    if _WEB_BACKEND == "astrbot":
        payload = await holder.json(default={})
    else:
        payload = await holder.get_json(silent=True)
    if isinstance(payload, Mapping):
        return payload
    raise NexusWebError("请求体必须是 JSON 对象")


class NexusWebApi:
    """把 Dashboard 的 bridge 调用映射到 「NexusService」。

    每个处理函数都是零位置参数的协程 —— 这正是 「Context.register_web_api」
    要求的形状。所有异常都在 「_guard」 里收口，绝不让 traceback 漏成 500 白页。
    """

    def __init__(self, service: NexusService, *, logger: Any = None) -> None:
        self._service = service
        self._logger = logger

    # -- 基础设施 ---------------------------------------------------------
    @property
    def available(self) -> bool:
        return _WEB_BACKEND != "none"

    @property
    def backend(self) -> str:
        return _WEB_BACKEND

    @property
    def service(self) -> NexusService:
        return self._service

    def routes(self) -> list[tuple[str, Callable[[], Awaitable[Any]], list[str], str]]:
        """返回 「(路由, 处理函数, 方法, 描述)」 列表。

        路由必须是 「/{PLUGIN_NAME}/xxx」：Dashboard 会把它挂到
        「/api/plug/{plugin_name}/xxx」，前端 「bridge.apiGet("xxx")」 才对得上。
        """

        prefix = "/" + PLUGIN_NAME
        label = PLUGIN_DISPLAY_NAME + " · "
        return [
            (prefix + "/meta", self.get_meta, ["GET"], label + "能力清单与指令表"),
            (prefix + "/overview", self.get_overview, ["GET"], label + "运行概览"),
            (prefix + "/config", self.get_config, ["GET"], label + "读取配置"),
            (prefix + "/config", self.post_config, ["POST"], label + "保存配置"),
            (prefix + "/state", self.get_state, ["GET"], label + "读取界面偏好"),
            (prefix + "/state", self.post_state, ["POST"], label + "保存界面偏好"),
            (prefix + "/logs", self.get_logs, ["GET"], label + "活动日志"),
            (prefix + "/logs/clear", self.post_logs_clear, ["POST"], label + "清空活动日志"),
            (prefix + "/watchlist", self.get_watchlist, ["GET"], label + "追番清单"),
            (prefix + "/watchlist", self.post_watchlist, ["POST"], label + "修改追番清单"),
            (prefix + "/watchlist/add", self.post_watch_add, ["POST"], label + "添加追番"),
            (prefix + "/subs", self.get_subs, ["GET"], label + "订阅列表"),
            (prefix + "/subs", self.post_subs, ["POST"], label + "订阅操作"),
            (prefix + "/subs/sources", self.get_sub_sources, ["GET"], label + "列出可选字幕组"),
            (prefix + "/excludes", self.get_excludes, ["GET"], label + "读取全局排除项"),
            (prefix + "/excludes", self.post_excludes, ["POST"], label + "保存全局排除项"),
            (prefix + "/sessions", self.get_sessions, ["GET"], label + "已知会话"),
            (prefix + "/targets", self.get_targets, ["GET"], label + "播报目标"),
            (prefix + "/targets", self.post_targets, ["POST"], label + "保存播报目标"),
            (prefix + "/push_now", self.post_push_now, ["POST"], label + "立即播报"),
            (prefix + "/poll_now", self.post_poll_now, ["POST"], label + "立即抓取 RSS"),
            (prefix + "/refresh", self.post_refresh, ["POST"], label + "刷新在线观看索引"),
            (prefix + "/diagnose", self.get_diagnose, ["GET"], label + "数据源体检"),
            (
                prefix + "/webhook/test",
                self.post_webhook_test,
                ["POST"],
                label + "发一条 Webhook 测试通知",
            ),
            (prefix + "/themes", self.get_themes, ["GET"], label + "主题清单"),
            (prefix + "/commands", self.get_commands, ["GET"], label + "指令表"),
            (prefix + "/card", self.get_card, ["GET"], label + "卡片预览"),
            (prefix + "/card/download", self.get_card_download, ["GET"], label + "下载卡片预览"),
            (prefix + "/search", self.get_search, ["GET"], label + "搜索番剧"),
            (prefix + "/gacha", self.post_gacha, ["POST"], label + "抽番预览"),
            (prefix + "/export", self.get_export, ["GET"], label + "导出数据"),
            (prefix + "/import", self.post_import, ["POST"], label + "导入数据"),
        ]

    def _log(self, message: str) -> None:
        logger = self._logger
        if logger is None:
            return
        try:
            logger.warning(message)
        except Exception:  # noqa: BLE001 - 打日志本身不能反过来炸请求
            pass

    async def _guard(self, action: Callable[[], Awaitable[Any]]) -> Any:
        """统一异常出口：业务错误给用户看得懂的文案，未知错误只给一句话 + 日志。"""

        try:
            return await action()
        except NexusWebError as error:
            return _error(error.message, status_code=error.status_code)
        except (ValueError, KeyError, TypeError) as error:
            self._log("[" + PLUGIN_NAME + "] web api 拒绝了一个请求：" + repr(error))
            return _error("请求参数不对，检查一下再试试")
        except OSError as error:
            self._log("[" + PLUGIN_NAME + "] web api 读写失败：" + repr(error))
            return _error("读写数据失败了，看看磁盘和权限", status_code=503)
        except Exception as error:  # noqa: BLE001 - 兜底
            self._log("[" + PLUGIN_NAME + "] web api 崩了：" + repr(error))
            return _error("插件内部错误，请查看 AstrBot 日志", status_code=500)

    # -- 处理函数 ---------------------------------------------------------
    async def get_meta(self) -> Any:
        async def run() -> Any:
            payload = await self._service.meta()
            payload["webui"] = {"backend": _WEB_BACKEND, "page": PAGE_NAME}
            return _json(payload)

        return await self._guard(run)

    async def get_overview(self) -> Any:
        return await self._guard(lambda: self._wrap(self._service.overview()))

    async def get_config(self) -> Any:
        return await self._guard(lambda: self._wrap(self._service.config_view()))

    async def post_config(self) -> Any:
        async def run() -> Any:
            body = await _json_body()
            patch = body.get("patch", body)
            if not isinstance(patch, Mapping):
                raise NexusWebError("配置补丁必须是一个对象")
            return _json(await self._service.save_config(patch))

        return await self._guard(run)

    async def get_state(self) -> Any:
        return await self._guard(lambda: self._wrap(self._service.state()))

    async def post_state(self) -> Any:
        async def run() -> Any:
            body = await _json_body()
            payload = body.get("state", body)
            if not isinstance(payload, Mapping):
                raise NexusWebError("界面偏好必须是一个对象")
            return _json(await self._service.save_state(payload))

        return await self._guard(run)

    async def get_logs(self) -> Any:
        async def run() -> Any:
            limit = _query_int("limit", 80)
            level = str(_query("level", "") or "")
            return _json(await self._service.logs(limit, level))

        return await self._guard(run)

    async def post_logs_clear(self) -> Any:
        return await self._guard(lambda: self._wrap(self._service.clear_logs()))

    async def get_watchlist(self) -> Any:
        async def run() -> Any:
            umo = str(_query("umo", "") or "")
            status = str(_query("status", "") or "")
            return _json(await self._service.watchlist(umo, status))

        return await self._guard(run)

    async def post_watchlist(self) -> Any:
        async def run() -> Any:
            return _json(await self._service.watch_action(await _json_body()))

        return await self._guard(run)

    async def post_watch_add(self) -> Any:
        async def run() -> Any:
            body = await _json_body()
            return _json(
                await self._service.add_watch(
                    str(body.get("umo") or ""), str(body.get("query") or "")
                )
            )

        return await self._guard(run)

    async def get_subs(self) -> Any:
        async def run() -> Any:
            return _json(await self._service.subscriptions(str(_query("umo", "") or "")))

        return await self._guard(run)

    async def post_subs(self) -> Any:
        async def run() -> Any:
            return _json(await self._service.sub_action(await _json_body()))

        return await self._guard(run)

    async def get_sub_sources(self) -> Any:
        async def run() -> Any:
            return _json(await self._service.sub_sources(str(_query("name", "") or "")))

        return await self._guard(run)

    async def get_excludes(self) -> Any:
        async def run() -> Any:
            return _json(await self._service.excludes(str(_query("umo", "") or "")))

        return await self._guard(run)

    async def post_excludes(self) -> Any:
        async def run() -> Any:
            return _json(await self._service.save_excludes(await _json_body()))

        return await self._guard(run)

    async def get_sessions(self) -> Any:
        return await self._guard(lambda: self._wrap(self._service.sessions()))

    async def get_targets(self) -> Any:
        return await self._guard(lambda: self._wrap(self._service.targets()))

    async def post_targets(self) -> Any:
        async def run() -> Any:
            body = await _json_body()
            values = body.get("targets", [])
            return _json(await self._service.save_targets(values))

        return await self._guard(run)

    async def post_push_now(self) -> Any:
        async def run() -> Any:
            body = await _json_body()
            targets = body.get("targets") or ()
            if isinstance(targets, (str, bytes)):
                raise NexusWebError("推送目标必须是一个列表")
            weekday = body.get("weekday") or 0
            return _json(await self._service.push_now(targets, int(weekday)))

        return await self._guard(run)

    async def post_poll_now(self) -> Any:
        async def run() -> Any:
            body = await _json_body()
            return _json(await self._service.poll_now(str(body.get("umo") or "")))

        return await self._guard(run)

    async def post_refresh(self) -> Any:
        return await self._guard(lambda: self._wrap(self._service.refresh_anime1()))

    async def get_diagnose(self) -> Any:
        return await self._guard(lambda: self._wrap(self._service.diagnose()))

    async def get_themes(self) -> Any:
        return await self._guard(lambda: self._wrap(self._service.themes()))

    async def get_commands(self) -> Any:
        return await self._guard(lambda: self._wrap(self._service.commands()))

    async def get_card(self) -> Any:
        async def run() -> Any:
            payload = await self._service.preview_payload(
                str(_query("theme", "") or ""),
                str(_query("kind", "help") or "help"),
                str(_query("renderer", "") or ""),
            )
            return _json(payload)

        return await self._guard(run)

    async def get_card_download(self) -> Any:
        async def run() -> Any:
            theme = str(_query("theme", "") or "")
            kind = str(_query("kind", "help") or "help")
            data, mime = await self._service.render_preview(
                theme, kind, str(_query("renderer", "") or "")
            )
            name = PAGE_NAME + "_" + (kind or "help") + "_" + (theme or "theme") + ".png"
            return _binary(data, content_type=mime, filename=name, inline=True)

        return await self._guard(run)

    async def get_search(self) -> Any:
        async def run() -> Any:
            keyword = str(_query("keyword", "") or _query("q", "") or "")
            return _json(await self._service.search(keyword, _query_int("limit", 8)))

        return await self._guard(run)

    async def post_gacha(self) -> Any:
        async def run() -> Any:
            body = await _json_body()
            return _json(await self._service.gacha_preview(str(body.get("genre") or "")))

        return await self._guard(run)

    async def get_export(self) -> Any:
        async def run() -> Any:
            return _json(await self._service.export(str(_query("umo", "") or "")))

        return await self._guard(run)

    async def post_import(self) -> Any:
        async def run() -> Any:
            body = await _json_body()
            payload = body.get("payload", body)
            if not isinstance(payload, Mapping):
                raise NexusWebError("导入内容必须是一个对象")
            return _json(await self._service.import_payload(payload, str(body.get("umo") or "")))

        return await self._guard(run)

    async def post_webhook_test(self) -> Any:
        return await self._guard(lambda: self._wrap(self._service.webhook_selftest()))

    # -- Webhook 接收（Dashboard 同源通道） --------------------------------
    def webhook_route(self, path: str) -> tuple[str, Callable[[], Awaitable[Any]], list[str], str]:
        """构造 Webhook 接收路由，交给 「main.py」 去 register。

        单独一个方法而不是塞进 「routes()」，因为它的路径由用户配置决定，
        而且启用与否跟 「webhook_enabled」 绑定，跟其它 WebUI 路由不同命。
        """

        route = "/" + str(path or "").strip().strip("/")

        async def handler() -> Any:
            return await self._guard(self._receive_webhook)

        return (route, handler, ["POST"], PLUGIN_DISPLAY_NAME + " · Webhook 接收")

    async def _receive_webhook(self) -> Any:
        payload = await _json_body()
        holder = _request_obj()
        raw_headers = getattr(holder, "headers", None)
        headers: dict[str, str] = {}
        if raw_headers is not None:
            try:
                headers = {str(key).lower(): str(value) for key, value in raw_headers.items()}
            except Exception:  # noqa: BLE001 - 不同框架的 headers 形状不完全一致
                headers = {}
        token = str(_query("token", "") or "")
        try:
            result = await self._service.handle_webhook(payload, token=token, headers=headers)
        except WebhookAuthError as error:
            raise NexusWebError(str(error), status_code=401) from error
        return _json({"status": "ok", "message": "", "data": result})

    @staticmethod
    async def _wrap(awaitable: Awaitable[Any]) -> Any:
        """把「服务返回 dict」统一包成 JSON 响应，省掉一堆同形状的小闭包。"""

        return _json(await awaitable)


__all__ = [
    "CONF_GROUPS",
    "CONF_SCHEMA",
    "NexusService",
    "NexusWebApi",
    "NexusWebError",
    "Wiring",
    "coerce_config_value",
    "config_schema_payload",
]
