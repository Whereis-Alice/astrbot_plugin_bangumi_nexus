"""通知分发：人格转述 + 卡片 + 重试。

上游 「astrbot_plugin_autobangumi_notify」 只会把干巴巴的事件文本丢出去。这里做三件事：
1. 用 AstrBot 自带人格生成一句口播（拿人格的 system prompt，任务指令另外给）；
2. 把事件渲染成卡片，和口播一起发；
3. 去重 + 指数退避 + 并发闸门，避免刷屏和瞬时抖动造成漏推。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable, Mapping, Sequence

from astrbot.api import logger
from astrbot.api.message_components import Image, Plain
from astrbot.core.message.message_event_result import MessageChain

from ..models import FeedItem, Notification
from ..platforms import Instances, describe, live_platforms, pick_platform_id, remap_umo
from ..render import build_feed_card, build_notice_card
from .base import Deps, Reply, cover_uri, llm_text, make_card, style_for

DEDUP_CAPACITY = 500
KIND_EYEBROW = {
    "new_episode": "NEW EPISODE",
    "download_start": "DOWNLOADING",
    "download_complete": "DOWNLOADED",
    "rename_complete": "RENAMED",
    "download_error": "DOWNLOAD ERROR",
    "rss_error": "RSS ERROR",
    "rss_update": "RSS UPDATE",
    "daily_digest": "DAILY",
    "test": "TEST",
}
ERROR_KINDS = frozenset({"download_error", "rss_error"})


class Notifier:
    """把 「Notification」 送到一组会话去。"""

    def __init__(self, deps: Deps) -> None:
        self._deps = deps
        self._recent: dict[str, float] = {}
        # 平台段重映射每种写法只提示一次，否则每轮推送都会刷同一行日志
        self._remap_notified: set[str] = set()
        self._sent = 0
        self._failed = 0
        self._skipped = 0

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------
    async def dispatch(
        self,
        notification: Notification,
        targets: Sequence[str],
        *,
        persona: bool | None = None,
    ) -> int:
        """向多个会话推送同一条通知，返回成功条数。

        并发受 「send_concurrency」 限制：群多的时候一次性全发出去容易被平台限流。
        """
        sessions = tuple(dict.fromkeys(target for target in targets if target))
        if not sessions:
            return 0
        if self._duplicate(notification):
            self._skipped += 1
            self._deps.activity.info("notify", f"跳过重复通知：{notification.title}")
            return 0

        gate = asyncio.Semaphore(max(1, self._deps.conf.send_concurrency))

        async def one(session: str) -> bool:
            async with gate:
                return await self.send(notification, session, persona=persona)

        results = await asyncio.gather(*(one(session) for session in sessions))
        return sum(1 for ok in results if ok)

    async def send(
        self,
        notification: Notification,
        umo: str,
        *,
        persona: bool | None = None,
    ) -> bool:
        """向单个会话推送，失败按指数退避重试。"""
        chain = await self.build_chain(notification, umo, persona=persona)
        return await self.deliver(chain, umo)

    async def send_reply(
        self,
        reply: Reply,
        umo: str,
        *,
        facts: str = "",
        persona: bool | None = None,
    ) -> bool:
        """把服务层产出的 「Reply」 当作主动推送发出去。

        每日播报直接复用 「SearchService.today」 已经排好的那张卡，没必要为了
        走通知管道再把整张表退化成纯文本行；人格口播的素材由 「facts」 另给。
        """
        deps = self._deps
        use_persona = deps.conf.persona_reply_enabled if persona is None else persona
        spoken = await self.speak(facts, umo) if (use_persona and facts) else ""
        components: list = []
        if spoken:
            components.append(Plain(spoken))
        card = None
        if reply.card is not None:
            try:
                card = await deps.engine.render(reply.card, deps.conf)
            except Exception as error:  # noqa: BLE001
                deps.activity.warn("notify", f"卡片渲染失败，退回文本：{error}")
        if card is not None and card.has_image:
            components.append(
                Image.fromFileSystem(card.image_path)
                if card.image_path
                else Image.fromURL(card.image_url)
            )
        else:
            text = (card.text if card is not None and card.text else reply.text).strip()
            if text:
                components.append(Plain(("\n" if spoken else "") + text))
        if not components:
            return False
        return await self.deliver(MessageChain(chain=components), umo)

    async def deliver(self, chain: MessageChain, umo: str) -> bool:
        """真正落地的一次投递，失败按指数退避重试。

        投递前先把会话标识的平台段对一遍运行时的实例表：库里可能存着早期版本
        拼错的适配器类型名，不纠正的话 「send_message」 会静默返回 False。
        """
        deps = self._deps
        conf = deps.conf
        umo = self.normalize_umo(umo)
        attempts = max(1, conf.send_max_retries)
        delay = max(0.5, conf.send_retry_delay_seconds)
        for attempt in range(1, attempts + 1):
            try:
                ok = await deps.context.send_message(umo, chain)
                if ok:
                    self._sent += 1
                    return True
                raise RuntimeError("没有匹配的平台适配器")
            except Exception as error:  # noqa: BLE001
                if attempt >= attempts:
                    self._failed += 1
                    deps.activity.error("notify", f"推送到 {umo} 失败：{error}")
                    logger.warning(f"番剧中枢推送失败 umo={umo}: {error}")
                    return False
                # 指数退避：平台限流或网络抖动时，越往后等越久
                await asyncio.sleep(delay * (2 ** (attempt - 1)))
        return False

    async def build_chain(
        self,
        notification: Notification,
        umo: str,
        *,
        persona: bool | None = None,
    ) -> MessageChain:
        """通知 → 消息链（口播文字 + 卡片图）。"""
        deps = self._deps
        conf = deps.conf
        use_persona = conf.persona_reply_enabled if persona is None else persona
        spoken = await self._persona_line(notification, umo) if use_persona else ""

        card = await self._render(notification, umo, spoken)
        components: list = []
        if spoken:
            components.append(Plain(spoken))
        if card is not None and card.has_image:
            components.append(
                Image.fromFileSystem(card.image_path)
                if card.image_path
                else Image.fromURL(card.image_url)
            )
        elif card is not None and card.text:
            components.append(Plain(("\n" if spoken else "") + card.text))
        elif not spoken:
            components.append(Plain(notification.plain_text()))
        return MessageChain(chain=components)

    # ------------------------------------------------------------------
    # 人格口播
    # ------------------------------------------------------------------
    async def _persona_line(self, notification: Notification, umo: str) -> str:
        """让当前生效的人格用自己的口吻说一句。

        人格提示词走 「system_prompt」，任务指令走 「prompt」 —— 这样人格决定语气、
        指令只约束内容，不会把「你是某某」硬塞进任务里冲掉用户配的人格。
        """
        return await self.speak(notification.plain_text(), umo)

    async def speak(self, facts: str, umo: str) -> str:
        """给定事实文本，让人格用自己的口吻转述一句。"""
        deps = self._deps
        conf = deps.conf
        if not facts.strip():
            return ""
        persona_prompt = await self._persona_prompt(umo)
        prompt = f"{conf.persona_instruction}\n\n通知内容：\n{facts}"
        text = await llm_text(
            deps,
            prompt,
            provider_id=conf.persona_provider_id,
            system_prompt=persona_prompt,
            umo=umo,
            limit=max(40, conf.persona_max_chars),
        )
        return text.replace("\n", " ").strip()

    async def _persona_prompt(self, umo: str) -> str:
        """取 AstrBot 里配置的人格提示词。

        「persona_manager」 是运行时实例属性，不同版本可能缺席，所以全程 getattr 探测；
        用户没在插件里指定人格时，跟随该会话的默认人格。
        """
        manager = getattr(self._deps.context, "persona_manager", None)
        if manager is None:
            return ""
        chosen = self._deps.conf.persona_id
        persona = None
        if chosen:
            getter = getattr(manager, "get_persona_v3_by_id", None)
            if callable(getter):
                try:
                    persona = getter(chosen)
                except Exception:  # noqa: BLE001
                    persona = None
        if persona is None:
            getter = getattr(manager, "get_default_persona_v3", None)
            if callable(getter):
                try:
                    persona = await getter(umo)
                except Exception:  # noqa: BLE001
                    persona = None
        return _persona_text(persona)

    # ------------------------------------------------------------------
    # 卡片
    # ------------------------------------------------------------------
    async def _render(self, notification: Notification, umo: str, spoken: str):
        deps = self._deps
        conf = deps.conf
        theme, _ = await style_for(deps, umo)
        cover = await cover_uri(deps, notification.cover)
        items = _feed_items(notification.payload)
        if items:
            html = build_feed_card(
                theme,
                notification.title,
                items,
                width=conf.card_width,
                subtitle=notification.subtitle,
                cover=cover,
                persona_text=spoken,
            )
        else:
            html = build_notice_card(
                theme,
                eyebrow=KIND_EYEBROW.get(notification.kind, "NOTICE"),
                title=notification.title,
                lines=notification.lines,
                subtitle=notification.subtitle,
                persona_text=spoken,
                cover=cover,
                link=notification.link,
                width=conf.card_width,
                stamp="ALERT" if notification.kind in ERROR_KINDS else "NOTICE",
            )
        request = make_card(
            html,
            plain=notification.plain_text(),
            title=notification.title,
            eyebrow=KIND_EYEBROW.get(notification.kind, "NOTICE"),
            subtitle=notification.subtitle,
            theme=theme,
            width=conf.card_width,
        )
        try:
            return await deps.engine.render(request, conf)
        except Exception as error:  # noqa: BLE001 - 渲染全线失败时退回纯文本
            deps.activity.warn("notify", f"卡片渲染失败，退回文本：{error}")
            return None

    # ------------------------------------------------------------------
    # 去重
    # ------------------------------------------------------------------
    def _duplicate(self, notification: Notification) -> bool:
        """同一条通知在窗口期内只发一次。

        AutoBangumi 之类的上游会重复投递同一事件，Webhook 又没有幂等键，
        所以这里用「内容指纹 + 时间窗」自己兜。
        """
        window = max(0, self._deps.conf.dedup_window_seconds)
        if window <= 0:
            return False
        key = notification.dedup_key()
        now = time.time()
        self._prune(now, window)
        seen_at = self._recent.get(key)
        if seen_at is not None and now - seen_at < window:
            return True
        self._recent[key] = now
        return False

    def _prune(self, now: float, window: float) -> None:
        if len(self._recent) < DEDUP_CAPACITY:
            stale = [key for key, at in self._recent.items() if now - at > window]
        else:
            stale = sorted(self._recent, key=self._recent.get)[: DEDUP_CAPACITY // 4]
        for key in stale:
            self._recent.pop(key, None)

    # ------------------------------------------------------------------
    # 目标解析
    # ------------------------------------------------------------------
    def resolve_targets(self, raw: Iterable[str]) -> tuple[str, ...]:
        """把配置里各种写法都归一成 unified_msg_origin。

        允许三种写法，因为让用户去背 「aiocqhttp:GroupMessage:12345」 太苛刻：
        「群号」、「group:群号」/「friend:QQ号」、以及完整的三段式 umo。
        平台段一律取运行时解析出来的实例 id，完整 umo 也会过一遍纠正。
        """
        platform = self.platform_id()
        resolved: list[str] = []
        for entry in raw:
            token = str(entry).strip()
            if not token:
                continue
            if token.count(":") >= 2:
                resolved.append(self.normalize_umo(token))
                continue
            if ":" in token:
                kind, _, ident = token.partition(":")
                kind = kind.strip().lower()
                ident = ident.strip()
                if not ident:
                    continue
                if kind in {"group", "群", "g"}:
                    resolved.append(f"{platform}:GroupMessage:{ident}")
                elif kind in {"friend", "private", "私聊", "f"}:
                    resolved.append(f"{platform}:FriendMessage:{ident}")
                else:
                    resolved.append(token)
                continue
            resolved.append(f"{platform}:GroupMessage:{token}")
        return tuple(dict.fromkeys(resolved))

    def instances(self) -> Instances:
        """当前启用的平台实例表，取不到就是空元组。"""
        return live_platforms(self._deps.context)

    def platform_id(self) -> str:
        """配置里的 「default_platform_id」 → 真实可用的平台实例 id。

        用户几乎总会填适配器类型名（面板上显示的就是它），而 「send_message」
        按实例 id 匹配，所以这里必须换算一次；留空即表示「自动挑一个」。
        """
        preferred = self._deps.conf.default_platform_id.strip()
        instances = self.instances()
        resolved = pick_platform_id(instances, preferred)
        if not resolved:
            # 适配器还没起来（启动早期）时保持旧行为，别拼出空平台段
            return preferred or "aiocqhttp"
        if preferred and resolved != preferred:
            self._warn_remap(preferred, resolved, instances)
        return resolved

    def normalize_umo(self, umo: str) -> str:
        """会话标识的平台段对齐运行时实例表；对得上就原样返回。"""
        instances = self.instances()
        fixed = remap_umo(umo, instances, self._deps.conf.default_platform_id.strip())
        if fixed != umo:
            self._warn_remap(umo.partition(":")[0], fixed.partition(":")[0], instances)
        return fixed

    def _warn_remap(self, source: str, target: str, instances: Instances) -> None:
        """同一种误填只提示一次，方便用户去改配置又不刷屏。"""
        key = f"{source}->{target}"
        if key in self._remap_notified:
            return
        self._remap_notified.add(key)
        self._deps.activity.warn(
            "notify",
            f"平台标识 「{source}」 不是启用中的适配器实例，已改用 「{target}」"
            f"（当前实例：{describe(instances)}）",
        )
        logger.info(f"番剧中枢平台标识重映射 {source} -> {target}")

    def stats(self) -> dict[str, int]:
        return {
            "sent": self._sent,
            "failed": self._failed,
            "skipped": self._skipped,
            "dedup_cached": len(self._recent),
        }


def _persona_text(persona: object) -> str:
    """Personality 在不同版本里是 TypedDict 或对象，两种都兼容。"""
    if persona is None:
        return ""
    if isinstance(persona, Mapping):
        for key in ("prompt", "system_prompt"):
            value = persona.get(key)
            if value:
                return str(value).strip()
        return ""
    for key in ("system_prompt", "prompt"):
        value = getattr(persona, key, "")
        if value:
            return str(value).strip()
    return ""


def _feed_items(payload: Mapping[str, object]) -> tuple[FeedItem, ...]:
    raw = payload.get("feed_items") if payload else None
    if not raw:
        return ()
    return tuple(item for item in raw if isinstance(item, FeedItem))


__all__ = ["ERROR_KINDS", "KIND_EYEBROW", "Notifier"]
