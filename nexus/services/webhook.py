"""AutoBangumi Webhook 接入。

把 AutoBangumi（或任何愿意 POST 同构 JSON 的下载器）推来的事件翻译成本插件的
「Notification」，再交给 「Notifier」 去做人格口播、卡片渲染和重试投递。

相比上游 「astrbot_plugin_autobangumi_notify」，这里多做三件事：

1. **精确路由**：除了配置里的固定推送目标，还能只推给「追番表里真的有这部番」
   的会话 —— 一台 AutoBangumi 服务多个群时，不用再把所有番刷给所有人；
2. **进度回填**：新集入库时顺手把追番表的进度推进到对应集数，
   于是 「/追番列表」 的进度条不用手动 「/看到」 也能跟上；
3. **封面补全**：AutoBangumi 不给海报时，回落到 Bangumi 的条目封面，
   卡片不会开天窗。

Copyright (C) 2026 Whereis-Alice and AstrBot Plugin Authors.
Licensed under the GNU Affero General Public License v3.0 or later.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from typing import Any

from ..models import Notification
from ..titles import similarity
from .base import Deps
from .notifier import Notifier
from .watchlist import STATUS_DROPPED, WatchlistService

# 事件标识 → 本插件内部 kind。AutoBangumi 各版本字段名不统一，
# 这里把见过的写法全列出来，识别不出再走字段推断。
EVENT_ALIASES: dict[str, str] = {
    "new_episode": "new_episode",
    "new": "new_episode",
    "update": "new_episode",
    "rss_update": "new_episode",
    "download_start": "download_start",
    "download_started": "download_start",
    "start": "download_start",
    "download_complete": "download_complete",
    "download_completed": "download_complete",
    "download": "download_complete",
    "complete": "download_complete",
    "rename_complete": "rename_complete",
    "rename_completed": "rename_complete",
    "rename": "rename_complete",
    "download_error": "download_error",
    "download_failed": "download_error",
    "error": "download_error",
    "rss_error": "rss_error",
    "rss_failed": "rss_error",
}

# 事件 → 事件描述短语，用作卡片副标题。
KIND_PHRASE = {
    "new_episode": "新集已更新",
    "download_start": "开始下载",
    "download_complete": "下载完成",
    "rename_complete": "已整理入库",
    "download_error": "下载失败",
    "rss_error": "RSS 抓取异常",
    "test": "连通性测试",
}

# 这些事件意味着「这一集已经能看了」，可以顺手推进追番进度。
PROGRESS_KINDS = frozenset({"rename_complete", "download_complete"})

# 请求头里可以带 token 的几种常见写法。
TOKEN_HEADERS = ("x-webhook-token", "x-token", "authorization")


class WebhookAuthError(Exception):
    """token 校验失败。"""


class WebhookService:
    """Webhook 事件的解析、路由与投递。"""

    def __init__(
        self,
        deps: Deps,
        *,
        notifier: Notifier,
        watchlist: WatchlistService | None = None,
    ) -> None:
        self._deps = deps
        self._notifier = notifier
        self._watchlist = watchlist
        self._received = 0
        self._rejected = 0
        self._delivered = 0
        self._last_at = 0.0
        self._last_kind = ""

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------
    async def handle(
        self,
        raw: Any,
        *,
        token: str = "",
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """处理一次 Webhook 请求，返回可直接 JSON 化的结果。

        任何异常都会被上层转成 HTTP 4xx/5xx；这里只负责业务判断。
        """
        deps = self._deps
        conf = deps.conf
        if not conf.webhook_enabled:
            raise WebhookAuthError("Webhook 未启用，请先在插件配置里打开 webhook_enabled")
        self._verify(conf.webhook_token, token, headers)

        payload = _as_mapping(raw)
        if payload is None:
            raise ValueError("请求体不是 JSON 对象")

        self._received += 1
        notification = await self.build(payload)
        self._last_at = time.time()
        self._last_kind = notification.kind

        targets = await self.targets_for(notification)
        if not targets:
            deps.activity.warn("webhook", f"{notification.title} 没有匹配的推送目标")
            return {"ok": True, "kind": notification.kind, "delivered": 0, "targets": 0}

        sent = await self._notifier.dispatch(notification, targets)
        self._delivered += sent
        if notification.kind in PROGRESS_KINDS:
            await self._auto_progress(notification, targets)
        deps.activity.info(
            "webhook", f"{notification.title} · {notification.kind} → {sent}/{len(targets)}"
        )
        return {
            "ok": True,
            "kind": notification.kind,
            "title": notification.title,
            "delivered": sent,
            "targets": len(targets),
        }

    def _verify(self, expected: str, token: str, headers: Mapping[str, str] | None) -> None:
        """校验 token。没配 token 就不校验，但 README 会明确劝配。"""
        if not expected:
            return
        provided = token.strip()
        if not provided and headers:
            lowered = {str(key).lower(): str(value) for key, value in headers.items()}
            for name in TOKEN_HEADERS:
                value = lowered.get(name, "").strip()
                if value:
                    provided = value.removeprefix("Bearer ").removeprefix("bearer ").strip()
                    break
        if provided != expected:
            self._rejected += 1
            self._deps.activity.warn("webhook", "token 校验失败，已拒绝")
            raise WebhookAuthError("token 不匹配")

    # ------------------------------------------------------------------
    # 解析
    # ------------------------------------------------------------------
    async def build(self, raw: Mapping[str, Any]) -> Notification:
        """原始 JSON → 「Notification」。"""
        kind = classify(raw)
        title = _first(raw, "title", "official_title", "name", "bangumi_name") or "未知番剧"
        season = _as_int(_first(raw, "season", "season_num"))
        episode = _as_int(_first(raw, "episode", "episode_num", "ep"))
        cover = _first(raw, "poster_url", "poster", "image", "cover")
        link = _first(raw, "url", "link", "torrent_url", "web_url")
        error = _first(raw, "error_msg", "error", "err_msg")
        message = _first(raw, "message", "msg")

        lines: list[str] = []
        marker = _episode_marker(season, episode)
        if marker:
            lines.append(f"进度：{marker}")
        for label, key in (
            ("种子", "torrent_name"),
            ("文件", "file_name"),
            ("大小", "size"),
            ("下载器", "downloader"),
            ("媒体库", "library"),
        ):
            value = _first(raw, key, f"{key}s")
            if value:
                lines.append(f"{label}：{value}")
        if error:
            lines.append(f"错误：{error}")
        if message and message not in lines:
            lines.append(message)
        if not lines:
            lines.append("上游没有给更多细节。")

        if not cover:
            cover = await self._cover_for(title)

        subtitle = KIND_PHRASE.get(kind, "番剧通知")
        if marker and kind == "new_episode":
            subtitle = f"{marker} · {subtitle}"
        return Notification(
            kind=kind,
            title=title,
            lines=tuple(lines),
            subtitle=subtitle,
            link=link,
            cover=cover,
            payload={"source": "autobangumi", "season": season, "episode": episode},
        )

    async def _cover_for(self, title: str) -> str:
        """上游没给海报时去 Bangumi 找一张，失败就算了。"""
        deps = self._deps
        if not title or not deps.conf.enable_cross_match:
            return ""
        try:
            found = await deps.hub.bangumi.search(title, limit=1)
        except Exception:  # noqa: BLE001
            return ""
        return found[0].image if found else ""

    # ------------------------------------------------------------------
    # 路由
    # ------------------------------------------------------------------
    async def targets_for(self, notification: Notification) -> tuple[str, ...]:
        """决定这条事件推给谁。

        固定目标始终收；打开 「webhook_notify_watchers」 后，追番表里有这部番的
        会话也会收到 —— 这样一台下载器服务多个群时不会互相刷屏。
        RSS 类错误属于运维信息，只发固定目标，不去烦普通群。
        """
        deps = self._deps
        conf = deps.conf
        targets = list(self._notifier.resolve_targets(conf.push_targets))
        if conf.webhook_notify_watchers and notification.kind != "rss_error":
            targets.extend(await self._watchers(notification.title))
        return tuple(dict.fromkeys(target for target in targets if target))

    async def _watchers(self, title: str) -> list[str]:
        """找出追番表里收录了这部番的会话。"""
        deps = self._deps
        if not title:
            return []
        try:
            items = await deps.store.list_watch("")
        except Exception as error:  # noqa: BLE001
            deps.activity.warn("webhook", f"读取追番表失败：{error}")
            return []
        return [
            item.umo
            for item in items
            if item.status != STATUS_DROPPED and similarity(item.title, title) >= 0.72
        ]

    # ------------------------------------------------------------------
    # 进度回填
    # ------------------------------------------------------------------
    async def _auto_progress(self, notification: Notification, targets: tuple[str, ...]) -> None:
        """新集入库时把追番进度推到对应集数。

        只在「下载完成 / 整理入库」时动，且只往前推不往后退 —— 补种老集数
        不该把用户已经看到的进度打回去。
        """
        deps = self._deps
        if not deps.conf.webhook_auto_progress or self._watchlist is None:
            return
        episode = _as_int(notification.payload.get("episode"))
        if not episode:
            return
        for session in targets:
            try:
                hits = await self._watchlist.matching_titles(session, notification.title)
            except Exception:  # noqa: BLE001
                continue
            for item in hits[:1]:
                if item.progress >= episode:
                    continue
                capped = min(episode, item.total) if item.total else episode
                try:
                    await deps.store.update_watch(item.id, progress=capped, updated_at=time.time())
                except Exception as error:  # noqa: BLE001
                    deps.activity.warn("webhook", f"回填进度失败：{error}")
                    continue
                deps.activity.info("webhook", f"{session} 的「{item.title}」进度回填到 {capped}")

    # ------------------------------------------------------------------
    # 观测
    # ------------------------------------------------------------------
    async def selftest(self) -> dict[str, Any]:
        """给 WebUI 的「发一条测试通知」按钮用。"""
        notification = Notification(
            kind="test",
            title="番剧中枢 Webhook 自检",
            lines=("这是一条测试通知。", "看到这张卡说明 Webhook 到消息平台的链路是通的。"),
            subtitle=KIND_PHRASE["test"],
        )
        targets = self._notifier.resolve_targets(self._deps.conf.push_targets)
        sent = await self._notifier.dispatch(notification, targets)
        return {"ok": bool(sent), "delivered": sent, "targets": len(targets)}

    def stats(self) -> dict[str, Any]:
        return {
            "received": self._received,
            "rejected": self._rejected,
            "delivered": self._delivered,
            "last_at": self._last_at,
            "last_kind": self._last_kind,
            "route": self._deps.conf.webhook_route,
            "enabled": self._deps.conf.webhook_enabled,
            "token_set": bool(self._deps.conf.webhook_token),
        }


# ---------------------------------------------------------------------------
# 纯函数：分类与字段提取（便于单测）
# ---------------------------------------------------------------------------
def classify(raw: Mapping[str, Any]) -> str:
    """识别事件类型：先看显式字段，再按字段组合推断。"""
    explicit = _first(raw, "event", "type", "event_type", "notify_type", "action")
    if explicit:
        mapped = EVENT_ALIASES.get(explicit.strip().lower())
        if mapped:
            return mapped

    error = _first(raw, "error_msg", "error", "err_msg")
    if error:
        haystack = (error + " " + _first(raw, "message", "msg")).lower()
        return "rss_error" if "rss" in haystack else "download_error"

    blob = json.dumps(raw, ensure_ascii=False, default=str).lower()
    if _first(raw, "file_name", "filename"):
        return "rename_complete" if "rename" in blob else "download_complete"
    if _first(raw, "torrent_name", "torrent") and "start" in blob:
        return "download_start"
    return "new_episode"


def _episode_marker(season: int, episode: int) -> str:
    """拼出 「第 2 季第 07 集」 这样的进度串。"""
    parts = []
    if season:
        parts.append(f"第 {season} 季")
    if episode:
        parts.append(f"第 {episode:02d} 集")
    return "".join(parts)


def _first(raw: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = raw.get(key)
        if value not in (None, "", [], {}):
            return str(value).strip()
    return ""


def _as_int(value: Any) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _as_mapping(raw: Any) -> Mapping[str, Any] | None:
    """请求体可能是 dict、JSON 字符串或 bytes，三种都收。"""
    if isinstance(raw, Mapping):
        return raw
    if isinstance(raw, (str, bytes, bytearray)):
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, Mapping) else None
    return None


__all__ = [
    "EVENT_ALIASES",
    "KIND_PHRASE",
    "WebhookAuthError",
    "WebhookService",
    "classify",
]
