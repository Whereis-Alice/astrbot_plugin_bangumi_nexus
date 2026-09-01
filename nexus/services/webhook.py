"""下载器 Webhook 接入。

把 AutoBangumi、ani-rss（或任何愿意 POST 同构 JSON 的下载器）推来的事件翻译成
本插件的 「Notification」，再交给 「Notifier」 去做人格口播、卡片渲染和重试投递。

相比上游 「astrbot_plugin_autobangumi_notify」，这里多做三件事：

1. **精确路由**：除了配置里的固定推送目标，还能只推给「追番表里真的有这部番」
   的会话 —— 一台 AutoBangumi 服务多个群时，不用再把所有番刷给所有人；
2. **进度回填**：新集入库时顺手把追番表的进度推进到对应集数，
   于是 「/追番列表」 的进度条不用手动 「/看到」 也能跟上；
3. **封面补全**：上游不给海报时，回落到 Bangumi 的条目封面，卡片不会开天窗；
4. **中文事件名**：ani-rss 的 Webhook 模板只能给出中文动作名（「下载完成」）
   和 emoji（「🎉」），这里一并认下来，用户直接写 「"event": "${action}"」 就行；
5. **静默记账**：ani-rss 允许一次订阅同时推「开始下载」和「下载完成」，
   全发卡片会一集刷两条 —— 「webhook_silent_kinds」 里列出的事件只回填进度、
   不发卡片。

Copyright (C) 2026 Whereis-Alice and AstrBot Plugin Authors.
Licensed under the GNU Affero General Public License v3.0 or later.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping
from typing import Any

from ..models import Notification
from ..titles import similarity
from .base import Deps
from .notifier import Notifier
from .watchlist import STATUS_DROPPED, WatchlistService, backfill_progress

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
    # —— ani-rss（NotificationStatusEnum）——
    # ani-rss 的 Webhook body 只能塞占位符，「${action}」 出来是中文动作名、
    # 「${emoji}」 是对应表情。两种都收，用户就不必在 body 里手写死事件名。
    "开始下载": "download_start",
    "🎈": "download_start",
    "下载完成": "download_complete",
    "🎉": "download_complete",
    "缺少集数": "episode_missing",
    "缺集": "episode_missing",
    "episode_missing": "episode_missing",
    "missing": "episode_missing",
    "omit": "episode_missing",
    "⛔": "episode_missing",
    "发生错误": "download_error",
    "❌": "download_error",
    "订阅完结": "series_completed",
    "series_completed": "series_completed",
    "subscription_completed": "series_completed",
    "🎊": "series_completed",
    "摸鱼检测": "idle_warning",
    "摸鱼": "idle_warning",
    "idle_warning": "idle_warning",
    "procrastinating": "idle_warning",
    "🐟": "idle_warning",
    # —— 整理入库（ani-rss 无此状态，AutoBangumi 有）——
    "整理完成": "rename_complete",
    "重命名完成": "rename_complete",
}

# 事件 → 事件描述短语，用作卡片副标题。
KIND_PHRASE = {
    "new_episode": "新集已更新",
    "download_start": "开始下载",
    "download_complete": "下载完成",
    "rename_complete": "已整理入库",
    "download_error": "下载失败",
    "rss_error": "RSS 抓取异常",
    "episode_missing": "缺集提醒",
    "series_completed": "本季完结",
    "idle_warning": "久未更新",
    "test": "连通性测试",
}

# 这些事件意味着「这一集已经能看了」，可以顺手推进追番进度。
PROGRESS_KINDS = frozenset({"rename_complete", "download_complete"})

# 归一化事件串时要抹掉的装饰字符。有人喜欢在模板里写 「[下载完成]」。
_EVENT_TRIM = "[]【】()（）「」<>《》:：·|/\\'\"“”"

# 这些别名太通用（一句话里随便就能撞上 「start」），只允许精确命中，不参与
# 宽松的子串匹配，免得把一整段描述文本误判成事件名。
_STRICT_ONLY = frozenset(
    {"new", "start", "update", "download", "complete", "rename", "error", "missing", "omit"}
)

# 宽松匹配的候选表：长别名排前面，「download_error」 才不会被 「download」 抢走。
_LOOSE_ALIASES: tuple[tuple[str, str], ...] = tuple(
    sorted(
        ((key, kind) for key, kind in EVENT_ALIASES.items() if key not in _STRICT_ONLY),
        key=lambda pair: len(pair[0]),
        reverse=True,
    )
)

# 请求头里可以带 token 的几种常见写法。
TOKEN_HEADERS = ("x-webhook-token", "x-token", "authorization")

# 上游没有封面时塞的占位图。原样当封面渲染会得到一张空白，不如干脆不给，
# 让 「_cover_for」 去 Bangumi 补一张真海报。
PLACEHOLDER_COVERS = ("docs.wushuo.top/null.png", "/null.png")

# 「S01E05」/「s1e5.5」 这类进度串。ani-rss 的 「${text}」 天然带它，
# 于是就算用户没在 body 里单独写 episode 字段，进度回填也不会失效。
EPISODE_PATTERN = re.compile(r"S(\d{1,2})E(\d{1,3})", re.IGNORECASE)


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
        self._silenced = 0
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
        silent = notification.kind in self.silent_kinds()
        if not targets:
            deps.activity.warn("webhook", f"{notification.title} 没有匹配的推送目标")
            return {"ok": True, "kind": notification.kind, "delivered": 0, "targets": 0}

        # 先记账，再发卡片。
        # 通知链路要拉封面、调人格 LLM、渲染卡片，任何一步炸了都不该让
        # 「这集已经入库」这条既成事实丢掉；回填只碰数据库，几乎不会失败。
        # 也正因如此，静默事件同样要回填 —— 静默的意义就是「只记账，不刷屏」。
        if notification.kind in PROGRESS_KINDS:
            await self._auto_progress(notification, targets)

        sent = 0
        if silent:
            self._silenced += 1
        else:
            sent = await self._notifier.dispatch(notification, targets)
            self._delivered += sent
        tail = "静默记账" if silent else f"→ {sent}/{len(targets)}"
        deps.activity.info("webhook", f"{notification.title} · {notification.kind} {tail}")
        return {
            "ok": True,
            "kind": notification.kind,
            "title": notification.title,
            "delivered": sent,
            "targets": len(targets),
            "silent": silent,
        }

    def silent_kinds(self) -> frozenset[str]:
        """配置里声明的「只回填进度、不发卡片」事件集合。

        用户可能写内部 kind（「download_complete」）、上游中文动作名（「下载完成」）
        甚至 emoji，统一折成内部 kind 再比对，免得因为写法不同而静默失效。
        """
        folded: set[str] = set()
        for item in self._deps.conf.webhook_silent_kinds:
            token = str(item).strip()
            if not token:
                continue
            folded.add(fold_event(token) or token.lower())
        return frozenset(folded)

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
        if cover and any(mark in cover for mark in PLACEHOLDER_COVERS):
            cover = ""
        link = _first(raw, "url", "link", "torrent_url", "web_url")
        error = _first(raw, "error_msg", "error", "err_msg")
        message = _first(raw, "message", "msg", "text")
        if not episode:
            # body 里没写集数字段时，从正文里的 「S01E05」 反推，
            # 这样最小化的 ani-rss 模板也能驱动进度回填。
            hint_season, hint_episode = parse_episode_marker(message)
            episode = hint_episode
            season = season or hint_season

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
            ("字幕组", "subgroup"),
            ("评分", "score"),
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
            payload={
                "source": _first(raw, "source", "from") or "webhook",
                "season": season,
                "episode": episode,
            },
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

        固定目标优先取 「webhook_targets」，留空才退回 「push_targets」。
        分开两个字段是因为这两条链的收件人经常不一样 —— 下载完成属于噪音较大的
        流水账，多数人想丢进某个群；每日播报则更常留在私聊。共用一份名单的话，
        想把下载通知挪进群就得连带把播报也挪走，反过来也一样。
        """
        deps = self._deps
        conf = deps.conf
        targets = list(self._notifier.resolve_targets(self.fixed_targets()))
        if conf.webhook_notify_watchers and notification.kind != "rss_error":
            targets.extend(await self._watchers(notification.title))
        return tuple(dict.fromkeys(target for target in targets if target))

    def fixed_targets(self) -> tuple[str, ...]:
        """本链的固定收件人：「webhook_targets」 优先，留空退回 「push_targets」。

        返回的是配置原文而非解析结果 —— 解析（平台标识重映射、去重）统一由
        Notifier 负责，这里只管「该看哪份名单」。
        """
        conf = self._deps.conf
        return conf.webhook_targets or conf.push_targets

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
        await backfill_progress(
            deps,
            self._watchlist,
            title=notification.title,
            episode=_as_int(notification.payload.get("episode")),
            targets=targets,
            channel="webhook",
        )

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
        targets = self._notifier.resolve_targets(self.fixed_targets())
        sent = await self._notifier.dispatch(notification, targets)
        return {"ok": bool(sent), "delivered": sent, "targets": len(targets)}

    def stats(self) -> dict[str, Any]:
        return {
            "received": self._received,
            "rejected": self._rejected,
            "delivered": self._delivered,
            "silenced": self._silenced,
            "silent_kinds": sorted(self.silent_kinds()),
            "last_at": self._last_at,
            "last_kind": self._last_kind,
            "route": self._deps.conf.webhook_route,
            "enabled": self._deps.conf.webhook_enabled,
            "token_set": bool(self._deps.conf.webhook_token),
            # 下面四项给管理页拼「ani-rss 该怎么填」的示例用。
            # 令牌本身绝不外传，只报「设了没有」。
            "port": self._deps.conf.webhook_port,
            "bind": self._deps.conf.webhook_bind,
            "auto_progress": self._deps.conf.webhook_auto_progress,
            "notify_watchers": self._deps.conf.webhook_notify_watchers,
            "targets": list(self.fixed_targets()),
            "targets_own": bool(self._deps.conf.webhook_targets),
        }


# ---------------------------------------------------------------------------
# 纯函数：分类与字段提取（便于单测）
# ---------------------------------------------------------------------------
def fold_event(text: str) -> str:
    """把五花八门的事件写法折成内部 kind，折不出来返回空串。

    上游写法实在太杂：AutoBangumi 用 「download_complete」，ani-rss 用中文动作名，
    图省事的人会在 body 里写 「${emoji}${action}」 得到 「🎉下载完成」，还有人爱加
    方括号或空格。所以分三步：原样精确匹配 → 归一化分隔符后精确匹配 → 子串匹配。
    """
    raw = (text or "").strip().lower()
    if not raw:
        return ""
    squeezed = re.sub(r"[\s\-.]+", "_", raw.strip(_EVENT_TRIM).strip()).strip("_")
    for candidate in (raw, squeezed):
        mapped = EVENT_ALIASES.get(candidate)
        if mapped:
            return mapped
    for key, kind in _LOOSE_ALIASES:
        if key in raw:
            return kind
    return ""


def classify(raw: Mapping[str, Any]) -> str:
    """识别事件类型：先看显式字段，再按字段组合推断。"""
    explicit = _first(raw, "event", "type", "event_type", "notify_type", "action")
    if explicit:
        mapped = fold_event(explicit)
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


def parse_episode_marker(text: str) -> tuple[int, int]:
    """从 「S01E05」 这类串里抠出季号与集号，抠不到返回 「(0, 0)」。"""
    match = EPISODE_PATTERN.search(text or "")
    if not match:
        return 0, 0
    return _as_int(match.group(1)), _as_int(match.group(2))


def _as_int(value: Any) -> int:
    """尽量把值折成整数。

    ani-rss 对半集（总集篇、OVA）会给出 「5.5」，直接 「int("5.5")」 会抛
    「ValueError」 把集数丢成 0，所以先按整数试，再退一步过 float 截断。
    """
    text = str(value).strip()
    if not text:
        return 0
    try:
        return int(text)
    except (TypeError, ValueError):
        pass
    try:
        return int(float(text))
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
    "PLACEHOLDER_COVERS",
    "WebhookAuthError",
    "WebhookService",
    "classify",
    "fold_event",
    "parse_episode_marker",
]
