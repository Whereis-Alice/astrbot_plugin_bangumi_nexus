"""番剧中枢 · Bangumi Nexus —— 插件入口。

这个文件刻意保持「薄」：真正的活儿全在 「nexus/」 包里。这里只做四件事

* 把 AstrBotConfig 解析成不可变的运行时快照，并在配置变更时同步各组件；
* 构造一整套服务（数据源 / 存储 / 渲染 / 匹配 / 调度 / 通知）并串成 「Deps」；
* 把 41 条聊天指令与 4 个 LLM 工具映射到服务层；
* 挂载 Dashboard WebUI 路由与可选的独立 Webhook 监听端口。

服务层不认识 AstrBot 的 「event」，只吃 「Deps」、吐 「Reply」；「_emit」 负责把
「Reply」 翻译成消息链。这样服务层能在 pytest 里裸跑，SDK 变动也只砸在本文件。

Copyright (C) 2026 Whereis-Alice and AstrBot Plugin Authors.
Licensed under the GNU Affero General Public License v3.0 or later.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.message.message_event_result import MessageChain

from .nexus import catalog
from .nexus.activity import ActivityLog
from .nexus.config import NexusConfig, load_config
from .nexus.constants import (
    LOG_PREFIX,
    PLUGIN_BRAND,
    PLUGIN_DISPLAY_NAME,
    PLUGIN_NAME,
    PLUGIN_VERSION,
    REPO_URL,
)
from .nexus.http import HttpClient
from .nexus.render import (
    HELP_CARD_WIDTH,
    CardEngine,
    asset_card_path,
    build_help_card,
    resolve_theme,
    theme_keys,
)
from .nexus.services.anirss import AUTH_LABEL, AniRssSyncService
from .nexus.services.base import (
    Deps,
    Reply,
    is_long_reply,
    make_card,
    numeric,
    style_for,
)
from .nexus.services.diagnostics import DiagnosticsService
from .nexus.services.gacha import GachaService
from .nexus.services.matcher import Matcher
from .nexus.services.notifier import Notifier
from .nexus.services.picker import PICK_NOTE
from .nexus.services.scheduler import Scheduler
from .nexus.services.search import SearchService
from .nexus.services.subscriptions import RAW_NOTE, SubscriptionService
from .nexus.services.watchlist import WatchlistService
from .nexus.services.webhook import WebhookService
from .nexus.sources.bangumi import TYPE_ANIME, TYPE_BOOK
from .nexus.sources.hub import SourceHub
from .nexus.store import Store
from .nexus.web import NexusService, NexusWebApi, WebhookListener, Wiring

#: 所有指令名与别名，按长度倒序 —— 「_args」 要用最长匹配剥掉触发词，
#: 否则 「/bgm番剧 迷宫饭」 会被 「bgm」 先吃掉一半。
TRIGGERS: tuple[str, ...] = tuple(
    sorted(
        {command.name for command in catalog.all_commands()}
        | {alias for command in catalog.all_commands() for alias in command.aliases},
        key=len,
        reverse=True,
    )
)

#: 单条指令的整体超时。数据源多、又要渲染图，给得比单次 HTTP 超时宽一些。
COMMAND_TIMEOUT_SECONDS = 120.0

BUSY_SEARCH = "\u2026 正在翻资料"
BUSY_RENDER = "\u2026 正在画卡片"
BUSY_PROBE = "\u2026 正在体检各数据源"


class BangumiNexusPlugin(Star):
    """番剧中枢：聊天指令 + 定时播报 + Webhook + Dashboard 工作台。"""

    def __init__(
        self,
        context: Context,
        config: AstrBotConfig | Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(context)
        self._config = config
        self._themes = theme_keys()
        self._registered_routes: list[str] = []
        self._webhook_route_mounted = ""

        conf = self._config_snapshot()
        self._activity = ActivityLog()
        self._data_dir = self._resolve_data_dir()

        self._http = HttpClient(
            user_agent=conf.user_agent,
            proxy=conf.proxy,
            timeout=float(conf.http_timeout_seconds),
            max_retries=conf.http_max_retries,
            cache_ttl=float(conf.cache_ttl_seconds),
            concurrency=conf.max_concurrency,
            activity=self._activity,
        )
        self._hub = SourceHub.build(self._http, bangumi_token=conf.bangumi_access_token)
        self._store = Store(self._data_dir / "nexus.db")
        self._engine = CardEngine(self, self._data_dir, self._activity)
        self._matcher = Matcher(self._hub, mikan_base=conf.mikan_base, activity=self._activity)

        self._deps = Deps(
            star=self,
            context=context,
            http=self._http,
            hub=self._hub,
            store=self._store,
            engine=self._engine,
            matcher=self._matcher,
            activity=self._activity,
            config=self._config_snapshot,
        )

        self._search = SearchService(self._deps)
        self._watchlist = WatchlistService(self._deps, self._search)
        self._subs = SubscriptionService(self._deps)
        self._gacha = GachaService(self._deps)
        self._notifier = Notifier(self._deps)
        self._diagnostics = DiagnosticsService(self._deps)
        self._webhook = WebhookService(
            self._deps, notifier=self._notifier, watchlist=self._watchlist
        )
        self._anirss = AniRssSyncService(self._deps, notifier=self._notifier)
        self._scheduler = Scheduler(
            self._deps,
            search=self._search,
            subscriptions=self._subs,
            notifier=self._notifier,
            watchlist=self._watchlist,
            anirss=self._anirss,
        )
        self._listener = self._build_listener(conf)

        self._wiring = Wiring(
            search=self._search,
            watchlist=self._watchlist,
            subs=self._subs,
            gacha=self._gacha,
            notifier=self._notifier,
            scheduler=self._scheduler,
            webhook=self._webhook,
            diagnostics=self._diagnostics,
            listener=self._listener,
            anirss=self._anirss,
            config_writer=self._apply_config if self._config_writable() else None,
        )
        self._service = NexusService(self._deps, self._wiring)
        self._webapi = NexusWebApi(self._service, logger=logger)
        self._mount_webapi(conf)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def initialize(self) -> None:
        await self._store.initialize()
        self._scheduler.start()
        listening = await self._listener.start() if self._listener is not None else False
        conf = self._config_snapshot()
        logger.info(
            "%s %s %s 就绪 — %d 条指令 / %d 个分类 / %d 个别名 / 主题 %s / WebUI %s / Webhook %s",
            LOG_PREFIX,
            PLUGIN_BRAND,
            PLUGIN_VERSION,
            catalog.command_count(),
            catalog.category_count(),
            catalog.alias_count(),
            conf.card_theme,
            f"{len(self._registered_routes)} 条路由" if self._registered_routes else "未挂载",
            f"独立端口 {self._listener.port}" if listening else "仅面板通道",
        )
        self._activity.info("plugin", f"{PLUGIN_BRAND} {PLUGIN_VERSION} 已启动")

    async def terminate(self) -> None:
        """按构造的反序拆掉：先停外部入口，再停后台循环，最后关连接。"""

        if self._listener is not None:
            await self._listener.stop()
        await self._scheduler.stop()
        await self._store.close()
        await self._http.close()
        self._activity.info("plugin", "已卸载")

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    def _config_snapshot(self) -> NexusConfig:
        """每次取用都重新解析。

        Dashboard 里改完配置会热生效，缓存快照就会拿到过期值；解析本身只是几十次
        字典读取，比引入一套失效通知便宜得多。
        """

        return load_config(self._config, themes=self._themes or ("midnight",))

    def _config_writable(self) -> bool:
        return callable(getattr(self._config, "save_config", None))

    def _resolve_data_dir(self) -> Path:
        """插件数据目录。宿主没给就退回插件自身目录下的 「data/」。"""

        try:
            return Path(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception:  # noqa: BLE001 - 老版本 SDK 或权限异常时不该起不来
            fallback = Path(__file__).resolve().parent / "data"
            fallback.mkdir(parents=True, exist_ok=True)
            return fallback

    async def _apply_config(self, patch: Mapping[str, Any]) -> Mapping[str, Any]:
        """WebUI 保存配置的落地实现，同时把改动同步给运行中的组件。

        AstrBot 只在插件重载时重新注入配置对象，所以这里必须自己把 HTTP 连接池、
        Bangumi Token、Mikan 基址、调度器与 Webhook 监听重新对齐，
        否则用户改完要重启才生效。
        """

        config = self._config
        if not self._config_writable():
            raise RuntimeError("当前宿主没有提供可写的插件配置对象")
        for key, value in patch.items():
            config[str(key)] = value  # type: ignore[index]
        save = getattr(config, "save_config", None)
        outcome = save() if callable(save) else None
        if inspect.isawaitable(outcome):
            await outcome
        return await self._sync_runtime()

    async def _sync_runtime(self) -> dict[str, Any]:
        """把最新配置推给各个有状态的组件，返回做了哪些动作（给 WebUI 回显）。"""

        conf = self._config_snapshot()
        actions: dict[str, Any] = {"saved": True}

        rebuild = self._http.reconfigure(
            user_agent=conf.user_agent,
            proxy=conf.proxy,
            timeout=float(conf.http_timeout_seconds),
            max_retries=conf.http_max_retries,
            cache_ttl=float(conf.cache_ttl_seconds),
            concurrency=conf.max_concurrency,
        )
        if rebuild:
            await self._http.close()
            actions["http_pool"] = "已重建"

        if getattr(self._hub.bangumi, "access_token", "") != conf.bangumi_access_token:
            self._hub.set_bangumi_token(conf.bangumi_access_token)
            actions["bangumi_token"] = "已更新"
        self._matcher.set_mikan_base(conf.mikan_base)

        if conf.push_enabled or conf.rss_enabled or conf.anime1_enabled:
            self._scheduler.start()
            actions["scheduler"] = "运行中"
        elif self._scheduler.running:
            await self._scheduler.stop()
            actions["scheduler"] = "已停止"

        actions["listener"] = await self._resync_listener(conf)
        return actions

    # ------------------------------------------------------------------
    # Webhook 独立通道 / Dashboard 路由
    # ------------------------------------------------------------------
    @staticmethod
    def _listener_key(conf: NexusConfig) -> tuple[Any, ...]:
        """决定独立监听是否需要重建的那几项配置。

        单独抽出来，是为了让 「_resync_listener」 能「配置没动就别重启 socket」——
        每次保存配置都重开端口会把正在推送的下载器连接打断。
        """

        return (
            conf.webhook_enabled,
            conf.webhook_port,
            conf.webhook_bind,
            conf.webhook_route,
            bool(conf.webhook_token),
        )

    def _build_listener(self, conf: NexusConfig) -> WebhookListener | None:
        """按配置决定要不要开独立 Webhook 端口。

        端口为 0（默认）时连对象都不建 —— 绝大多数人只用 Dashboard 同源通道，
        没必要凭空多一个监听 socket。「token_required」 取「令牌为空」，
        listener 在这种情况下会拒绝启动，避免开出一个谁都能 POST 的裸端点。
        """

        self._listener_signature = self._listener_key(conf)
        if not conf.webhook_enabled or conf.webhook_port <= 0:
            return None
        return WebhookListener(
            handler=self._webhook.handle,
            route=conf.webhook_route,
            host=conf.webhook_bind,
            port=conf.webhook_port,
            token_required=not conf.webhook_token,
            activity=self._activity,
        )

    async def _resync_listener(self, conf: NexusConfig) -> str:
        """配置改动后对齐独立监听，返回一句给 WebUI 回显的状态。"""

        if self._listener_key(conf) == getattr(self, "_listener_signature", None):
            if self._listener is None:
                return "未启用"
            return f"监听 {self._listener.port}" if self._listener.running else "未监听"

        if self._listener is not None:
            await self._listener.stop()
            self._listener = None
        self._listener = self._build_listener(conf)
        self._service.attach_listener(self._listener)
        if self._listener is None:
            return "已停止"
        started = await self._listener.start()
        return f"已监听 {self._listener.port}" if started else "启动失败（检查令牌与端口占用）"

    def _mount_webapi(self, conf: NexusConfig) -> None:
        """把 WebUI 与 Webhook 路由注册到 Dashboard。

        全程「能挂就挂、挂不上只记日志」：宿主版本差异不该让插件起不来，
        没有 WebUI 的实例照样能用全部聊天指令。
        """

        if not conf.webui_enabled:
            return
        if not self._webapi.available:
            logger.info("%s 当前 AstrBot 没有可用的 Web API 运行时，WebUI 已跳过", LOG_PREFIX)
            return
        register = getattr(self.context, "register_web_api", None)
        if not callable(register):
            logger.info("%s 宿主不支持 register_web_api，WebUI 已跳过", LOG_PREFIX)
            return

        for route, handler, methods, description in self._webapi.routes():
            try:
                register(route, handler, methods, description)
            except Exception:  # noqa: BLE001 - 宿主实现各异，单条失败不该连坐
                logger.exception("%s 注册 WebUI 路由失败：%s", LOG_PREFIX, route)
                continue
            self._registered_routes.append(route)

        if conf.webhook_enabled:
            route, handler, methods, description = self._webapi.webhook_route(conf.webhook_route)
            try:
                register(route, handler, methods, description)
            except Exception:  # noqa: BLE001 - 同上
                logger.exception("%s 注册 Webhook 路由失败：%s", LOG_PREFIX, route)
            else:
                self._webhook_route_mounted = route

        if self._registered_routes:
            logger.info(
                "%s WebUI 已挂载 %d 条路由（backend=%s）%s",
                LOG_PREFIX,
                len(self._registered_routes),
                self._webapi.backend,
                f"，Webhook 面板通道 /api/plug{self._webhook_route_mounted}"
                if self._webhook_route_mounted
                else "",
            )

    # ------------------------------------------------------------------
    # 消息层：参数解析 / 渲染 / 发送
    # ------------------------------------------------------------------
    @staticmethod
    def _clean(text: str) -> str:
        """去掉首尾空白与零宽字符。

        手机输入法经常在粘贴的番名里塞不可见字符，直接拿去搜会一条都搜不到。
        """

        stripped = str(text or "").strip()
        return "".join(char for char in stripped if char not in "\u200b\u200c\u200d\ufeff").strip()

    @classmethod
    def _args(cls, event: AstrMessageEvent) -> str:
        """剥掉唤醒前缀与触发词，返回参数尾巴。

        「TRIGGERS」 按长度倒序，所以 「/bgm番剧 迷宫饭」 会先匹配 「bgm番剧」
        而不是被 「bgm」 咬掉一半。
        """

        raw = cls._clean(getattr(event, "message_str", "") or "")
        if not raw:
            return ""
        body = raw.lstrip("/!#.。、 \t")
        for trigger in TRIGGERS:
            if body.startswith(trigger):
                return cls._clean(body[len(trigger) :])
        return body

    def _command_prefix(self) -> str:
        """取实例的第一个唤醒前缀，让帮助卡上的示例跟部署一致。"""

        try:
            config = self.context.get_config()
        except Exception:  # noqa: BLE001 - 拿不到配置就用默认前缀
            return "/"
        getter = getattr(config, "get", None)
        prefixes = getter("wake_prefix", None) if callable(getter) else None
        if isinstance(prefixes, str):
            return prefixes or "/"
        if isinstance(prefixes, (list, tuple)):
            for item in prefixes:
                text = str(item or "").strip()
                if text:
                    return text
        return "/"

    def _usage(self, name: str) -> str:
        """从指令清单生成用法提示，避免同一段说明在代码里再抄一遍。"""

        command = catalog.find(name)
        prefix = self._command_prefix()
        if command is None:
            return f"用法：{prefix}{name} <参数>"
        return f"用法：{prefix}{command.usage}\n{command.summary}"

    async def _session_config(self, umo: str) -> NexusConfig:
        """把会话级的主题 / 渲染器偏好叠加到全局配置上。

        服务层只会把主题写进 「CardRequest」，渲染器偏好没有承载位置，
        所以在这里用 「dataclasses.replace」 造一份临时配置交给渲染引擎。
        """

        conf = self._config_snapshot()
        theme, renderer = await style_for(self._deps, umo)
        if theme == conf.card_theme and renderer == conf.card_renderer:
            return conf
        return dataclasses.replace(conf, card_theme=theme, card_renderer=renderer)

    @staticmethod
    def _image_component(path_or_url: str) -> Any:
        """AstrBot 的 html_render 可能回本地路径也可能回 URL，这里统一分流。"""

        value = str(path_or_url or "")
        if value.startswith(("http://", "https://")):
            return Comp.Image.fromURL(value)
        return Comp.Image.fromFileSystem(value)

    async def _text_image(self, text: str) -> Any | None:
        """长文本转图（t2i）。失败返回 None，由调用方退回纯文本。"""

        try:
            result = await self.text_to_image(text)
        except Exception:  # noqa: BLE001 - t2i 未配置或渲染失败都只是降级
            logger.debug("%s text_to_image 失败", LOG_PREFIX, exc_info=True)
            return None
        return self._image_component(result) if result else None

    async def _compose(
        self,
        reply: Reply,
        *,
        conf: NexusConfig,
        extra: str = "",
    ) -> list[Any]:
        """把服务层的 「Reply」 翻译成一串消息组件。

        优先级：卡片图 → 卡片纯文本 → 回复纯文本；带 「RAW_NOTE」 的回复
        （比如 「/sub_export」 的 JSON）必须保持可复制，不转图。

        为什么要单独拆出组件列表：选源列表这类一次性消息选完要撤回，
        而 「event.chain_result」 交给框架发送后拿不到消息 id，只能自己发。
        """

        if reply.empty:
            return []
        tail = self._clean(extra)
        text = reply.text

        if reply.card is not None:
            card = await self._engine.render(reply.card, conf)
            if card.has_image:
                # 卡片补充文本只在真出图时才追加：退回纯文本时 「reply.text」 里已经有了
                tail = "\n\n".join(part for part in (self._clean(reply.caption), tail) if part)
                chain: list[Any] = [self._image_component(card.image_path or card.image_url)]
                if tail:
                    chain.append(Comp.Plain("\n" + tail))
                return chain
            text = card.text or reply.text

        body = "\n\n".join(part for part in (str(text or "").strip(), tail) if part)
        if not body:
            return []
        if RAW_NOTE not in reply.notes and conf.long_reply_as_card and is_long_reply(body):
            image = await self._text_image(body)
            if image is not None:
                return [image]
        return [Comp.Plain(body)]

    async def _emit(
        self,
        event: AstrMessageEvent,
        reply: Reply,
        *,
        conf: NexusConfig,
        extra: str = "",
    ) -> Any:
        """常规回复：交给框架发送，不关心消息 id。"""

        components = await self._compose(reply, conf=conf, extra=extra)
        return event.chain_result(components) if components else None

    async def _send_capture(self, event: AstrMessageEvent, components: list[Any]) -> str:
        """自己把消息发出去，并尽量把消息 id 带回来。

        「event.send」 与 「event.chain_result」 都不返回消息 id，所以这里绕到
        aiocqhttp 的原生接口。任何一步不成（换了平台、接口报错、协议端不返回 id）
        都退回普通发送并返回空串 —— 代价只是「选源列表留在聊天记录里」。
        """

        chain = MessageChain(chain=list(components))
        bot = getattr(event, "bot", None)
        parse = getattr(event, "_parse_onebot_json", None)
        if bot is None or parse is None:
            await event.send(chain)
            return ""
        try:
            payload = await parse(chain)
            group_id = str(event.get_group_id() or "")
            if group_id.isdigit():
                result = await bot.send_group_msg(group_id=int(group_id), message=payload)
            else:
                sender = str(event.get_sender_id() or "")
                if not sender.isdigit():
                    raise ValueError("拿不到数字会话 id")
                result = await bot.send_private_msg(user_id=int(sender), message=payload)
        except Exception:  # noqa: BLE001 - 拿不到 id 只影响事后撤回，正常发送兜底
            logger.debug("%s 原生发送失败，退回普通发送", LOG_PREFIX, exc_info=True)
            await event.send(chain)
            return ""
        message_id = result.get("message_id") if isinstance(result, Mapping) else None
        return str(message_id) if message_id else ""

    async def _recall(self, event: AstrMessageEvent, message_ids: Sequence[str]) -> int:
        """撤回一批消息，返回成功条数。失败只写调试日志。"""

        bot = getattr(event, "bot", None)
        if bot is None:
            return 0
        done = 0
        for raw in message_ids:
            token = str(raw or "").strip()
            if not token.lstrip("-").isdigit():
                continue
            try:
                await bot.call_action("delete_msg", message_id=int(token))
            except Exception:  # noqa: BLE001 - 撤回失败只是多留一条历史，不该影响主流程
                logger.debug("%s 撤回消息失败 id=%s", LOG_PREFIX, token, exc_info=True)
            else:
                done += 1
        return done

    async def _run(
        self,
        event: AstrMessageEvent,
        worker: Callable[[], Awaitable[Any]],
        *,
        action: str,
        busy: str | None = None,
        failure: str = "这一步没做成，稍后再试或换个关键词",
    ) -> AsyncIterator[Any]:
        """所有指令共用的驱动：忙提示 → 执行 → 友好报错 → 「stop_event」。

        集中在一处，是因为上游几个插件里有一半的处理器忘了 「stop_event」，
        导致同一条消息被别的插件重复处理。
        """

        try:
            if busy:
                yield event.plain_result(busy)
            try:
                result = await asyncio.wait_for(worker(), timeout=COMMAND_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                self._activity.warn(action, "超时")
                yield event.plain_result(
                    f"⌛ 超过 {int(COMMAND_TIMEOUT_SECONDS)} 秒还没出结果，"
                    "可能是数据源在抽风，过一会儿再来"
                )
            except (OSError, ValueError) as error:
                self._activity.warn(action, str(error))
                yield event.plain_result(f"⚠️ {error}")
            except Exception:  # noqa: BLE001 - 指令入口兜底，任何异常都不能让消息处理链崩掉
                logger.exception("%s 指令「%s」执行失败", LOG_PREFIX, action)
                self._activity.error(action, "执行失败，详见日志")
                yield event.plain_result(f"⚠️ {failure}")
            else:
                if result is not None:
                    yield result
        finally:
            event.stop_event()

    def _serve(
        self,
        event: AstrMessageEvent,
        action: str,
        call: Callable[[str, NexusConfig], Awaitable[Reply | tuple[Reply, str]]],
        *,
        busy: str | None = None,
        failure: str = "这一步没做成，稍后再试或换个关键词",
    ) -> AsyncIterator[Any]:
        """「取会话配置 → 调服务 → 发消息」的标准流程。

        「call」 可以只回 「Reply」，也可以回 「(Reply, 追加文本)」 —— 后者用于
        「/追番」 这种「主卡片 + 顺手推荐 RSS 源」的场景。
        """

        umo = event.unified_msg_origin

        async def worker() -> Any:
            conf = await self._session_config(umo)
            outcome = await call(umo, conf)
            reply, extra = outcome if isinstance(outcome, tuple) else (outcome, "")
            return await self._emit(event, reply, conf=conf, extra=extra)

        return self._run(event, worker, action=action, busy=busy, failure=failure)

    # ------------------------------------------------------------------
    # 指令：查番
    # ------------------------------------------------------------------
    @staticmethod
    def _split_limit(args: str) -> tuple[str, int]:
        """把 「/bgm 迷宫饭 5」 里结尾的数量参数摘出来。

        只在「不止一个 token」时才认，否则 「/bgm 12345」 会被误解成「要 12345 条」
        而不是「按条目 ID 查」。
        """

        tokens = args.split()
        if len(tokens) > 1:
            tail = numeric(tokens[-1])
            if tail:
                return " ".join(tokens[:-1]), tail
        return args, 0

    @filter.command("bgm", priority=10)
    async def cmd_bgm(self, event: AstrMessageEvent):
        """在 Bangumi 搜索条目；传纯数字按条目 ID 直接开卡。"""

        args = self._args(event)
        if not args or args.lower() in {"help", "帮助", "?", "？"}:
            async for item in self._help_stream(event, ""):
                yield item
            return
        keyword, limit = self._split_limit(args)
        async for item in self._serve(
            event,
            "bgm",
            lambda umo, conf: self._search.search(umo, keyword, limit=limit),
            busy=BUSY_SEARCH,
        ):
            yield item

    @filter.command("bgm番剧", alias={"动漫", "动画", "番", "动画片"}, priority=10)
    async def cmd_bgm_anime(self, event: AstrMessageEvent):
        """只搜 TV 动画，排除剧场版与三次元。"""

        keyword, limit = self._split_limit(self._args(event))
        if not keyword:
            yield event.plain_result(self._usage("bgm番剧"))
            event.stop_event()
            return
        async for item in self._serve(
            event,
            "bgm番剧",
            lambda umo, conf: self._search.search(umo, keyword, limit=limit, tv_only=True),
            busy=BUSY_SEARCH,
        ):
            yield item

    @filter.command("bgm剧场版", alias={"电影", "劇場版"}, priority=10)
    async def cmd_bgm_movie(self, event: AstrMessageEvent):
        """只搜剧场版 / 电影。"""

        keyword, limit = self._split_limit(self._args(event))
        if not keyword:
            yield event.plain_result(self._usage("bgm剧场版"))
            event.stop_event()
            return
        async for item in self._serve(
            event,
            "bgm剧场版",
            lambda umo, conf: self._search.search(umo, keyword, limit=limit, movie_only=True),
            busy=BUSY_SEARCH,
        ):
            yield item

    @filter.command("bgm漫画", alias={"漫画"}, priority=10)
    async def cmd_bgm_book(self, event: AstrMessageEvent):
        """搜漫画与轻小说条目。"""

        keyword, limit = self._split_limit(self._args(event))
        if not keyword:
            yield event.plain_result(self._usage("bgm漫画"))
            event.stop_event()
            return
        async for item in self._serve(
            event,
            "bgm漫画",
            lambda umo, conf: self._search.search(
                umo, keyword, limit=limit, subject_type=TYPE_BOOK
            ),
            busy=BUSY_SEARCH,
        ):
            yield item

    @filter.command("查番", priority=10)
    async def cmd_subject(self, event: AstrMessageEvent):
        """跨源聚合卡：评分、倒计时、制作组、声优、观看入口一次给全。"""

        query = self._args(event)
        if not query:
            yield event.plain_result(self._usage("查番"))
            event.stop_event()
            return
        async for item in self._serve(
            event,
            "查番",
            lambda umo, conf: self._search.detail(umo, query=query, include_moegirl=True),
            busy=BUSY_SEARCH,
        ):
            yield item

    @filter.command("放送时间", priority=10)
    async def cmd_air_time(self, event: AstrMessageEvent):
        """下一集什么时候播、还差几天。"""

        query = self._args(event)
        if not query:
            yield event.plain_result(self._usage("放送时间"))
            event.stop_event()
            return
        async for item in self._serve(
            event, "放送时间", lambda umo, conf: self._search.air_time(umo, query), busy=BUSY_SEARCH
        ):
            yield item

    @filter.command("在线观看", priority=10)
    async def cmd_watch_links(self, event: AstrMessageEvent):
        """汇总 anime1、正版平台与官网入口。"""

        query = self._args(event)
        if not query:
            yield event.plain_result(self._usage("在线观看"))
            event.stop_event()
            return
        async for item in self._serve(
            event,
            "在线观看",
            lambda umo, conf: self._search.watch_links(umo, query),
            busy=BUSY_SEARCH,
        ):
            yield item

    @filter.command("萌娘百科", priority=10)
    async def cmd_moegirl(self, event: AstrMessageEvent):
        """查萌娘百科词条摘要。"""

        keyword = self._args(event)
        if not keyword:
            yield event.plain_result(self._usage("萌娘百科"))
            event.stop_event()
            return
        async for item in self._serve(
            event,
            "萌娘百科",
            lambda umo, conf: self._search.moegirl(umo, keyword),
            busy=BUSY_SEARCH,
        ):
            yield item

    # ------------------------------------------------------------------
    # 指令：日历
    # ------------------------------------------------------------------
    @staticmethod
    def _today_index() -> int:
        """ISO 周几（周一=1 … 周日=7），跟 Bot 所在机器的本地时区一致。"""

        return datetime.now().isoweekday()

    @filter.command("calendar", alias={"每日放送"}, priority=10)
    async def cmd_calendar(self, event: AstrMessageEvent):
        """一张七天放送总表，今天那一列会高亮。"""

        today = self._today_index()
        async for item in self._serve(
            event,
            "calendar",
            lambda umo, conf: self._search.calendar(umo, today_index=today),
            busy=BUSY_RENDER,
        ):
            yield item

    @filter.command("today", alias={"今日放送", "今日新番"}, priority=10)
    async def cmd_today(self, event: AstrMessageEvent):
        """今天播出的番：封面、放送钟点、评分，外加今天也在播的年番。"""

        weekday = self._today_index()
        async for item in self._serve(
            event,
            "today",
            lambda umo, conf: self._search.today(umo, weekday=weekday),
            busy=BUSY_RENDER,
        ):
            yield item

    @filter.command("季度新番", priority=10)
    async def cmd_season(self, event: AstrMessageEvent):
        """整季新番总览，默认当前季度，可传 202607 这样的季度码。"""

        code = self._args(event).replace("-", "").replace("/", "")
        async for item in self._serve(
            event, "季度新番", lambda umo, conf: self._search.season(umo, code), busy=BUSY_RENDER
        ):
            yield item

    @filter.command("新番", alias={"bangumi"}, priority=10)
    async def cmd_bangumi_group(self, event: AstrMessageEvent):
        """兼容上游的三合一指令：「新番 today / push / status」。

        没做成 command_group，是因为 AstrBot 的指令组会占用 「新番」 这个词本身，
        而上游用户习惯直接发 「/新番」 看今天播什么。
        """

        args = self._args(event)
        token = (args.split() or [""])[0].lower()

        if token in {"push", "推送"}:
            if not event.is_admin():
                yield event.plain_result("「新番 push」 只有管理员能用")
                event.stop_event()
                return
            async for item in self._serve(
                event, "新番push", self._push_now, busy="\u2026 正在生成今日播报"
            ):
                yield item
            return

        if token in {"status", "状态"}:
            if not event.is_admin():
                yield event.plain_result("「新番 status」 只有管理员能用")
                event.stop_event()
                return
            async for item in self._serve(event, "新番status", self._status_reply):
                yield item
            return

        weekday = self._today_index()
        async for item in self._serve(
            event,
            "新番",
            lambda umo, conf: self._search.today(umo, weekday=weekday),
            busy=BUSY_RENDER,
        ):
            yield item

    async def _push_now(self, umo: str, conf: NexusConfig) -> Reply:
        """立刻跑一次每日播报，并把结果说清楚。"""

        targets = await self._scheduler.push_targets()
        if not targets:
            return Reply.plain(
                "还没有任何推送目标。两种办法：\n"
                "· 管理员在插件配置里填 push_targets\n"
                f"· 在想收播报的群里发 {self._command_prefix()}日历订阅 开"
            )
        sent = await self._scheduler.run_daily(targets=targets)
        return Reply.plain(f"每日播报已发出：成功 {sent} / 目标 {len(targets)}")

    async def _status_reply(self, umo: str, conf: NexusConfig) -> Reply:
        """推送与订阅的运行状态，复用订阅服务的排版。"""

        return await self._subs.status(umo, self._scheduler.snapshot())

    # ------------------------------------------------------------------
    # 指令：追番
    # ------------------------------------------------------------------
    @filter.command("追番", priority=10)
    async def cmd_watch_add(self, event: AstrMessageEvent):
        """把一部番加进追番列表，并顺手给出可用的更新订阅源。"""

        query = self._args(event)
        if not query:
            yield event.plain_result(self._usage("追番"))
            event.stop_event()
            return

        async def call(umo: str, conf: NexusConfig) -> tuple[Reply, str]:
            reply, match = await self._watchlist.add(umo, query)
            if match is None:
                return reply, ""
            # 顺手开一个「回序号订阅」的会话，提示文本跟在追番卡后面一起发
            return reply, await self._subs.offer_from_match(umo, match)

        async for item in self._serve(event, "追番", call, busy=BUSY_SEARCH):
            yield item

    @filter.command("弃坑", priority=10)
    async def cmd_watch_drop(self, event: AstrMessageEvent):
        """把一部番从追番列表里移除。"""

        query = self._args(event)
        if not query:
            yield event.plain_result(self._usage("弃坑"))
            event.stop_event()
            return
        async for item in self._serve(
            event, "弃坑", lambda umo, conf: self._watchlist.drop(umo, query)
        ):
            yield item

    @filter.command("追番列表", alias={"我的追番"}, priority=10)
    async def cmd_watch_list(self, event: AstrMessageEvent):
        """本会话的追番进度总览。"""

        status = self._args(event)
        async for item in self._serve(
            event,
            "追番列表",
            lambda umo, conf: self._watchlist.overview(umo, status=status),
            busy=BUSY_RENDER,
        ):
            yield item

    @filter.command("看到", priority=10)
    async def cmd_watch_progress(self, event: AstrMessageEvent):
        """更新追番进度：「看到 迷宫饭 12」。"""

        tokens = self._args(event).split()
        if len(tokens) < 2:
            yield event.plain_result(self._usage("看到"))
            event.stop_event()
            return
        name = " ".join(tokens[:-1])
        episode = tokens[-1]
        async for item in self._serve(
            event, "看到", lambda umo, conf: self._watchlist.progress(umo, name, episode)
        ):
            yield item

    # ------------------------------------------------------------------
    # 指令：订阅推送
    # ------------------------------------------------------------------
    @staticmethod
    def _action_value(args: str) -> tuple[str, str]:
        """把 「get」 / 「set 值」 这类参数拆成 (动作, 值)。"""

        tokens = args.split(maxsplit=1)
        action = tokens[0].strip().lower() if tokens else ""
        value = tokens[1].strip() if len(tokens) > 1 else ""
        return action, value

    @filter.command("sub", priority=10)
    async def cmd_sub_add(self, event: AstrMessageEvent):
        """订阅一个更新源；只给番名时会先列出字幕组让你回序号。"""

        args = self._args(event)
        if not args:
            yield event.plain_result(self._usage("sub"))
            event.stop_event()
            return
        umo = event.unified_msg_origin

        async def worker() -> Any:
            conf = await self._session_config(umo)
            reply = await self._subs.add(umo, args)
            if PICK_NOTE not in reply.notes:
                return await self._emit(event, reply, conf=conf)
            # 选源列表要能事后撤回，所以自己发、自己记消息 id
            components = await self._compose(reply, conf=conf)
            if not components:
                return None
            message_id = await self._send_capture(event, components)
            if message_id:
                self._deps.picker.note_message(umo, message_id)
            return None

        async for item in self._run(event, worker, action="sub", busy=BUSY_SEARCH):
            yield item

    @filter.event_message_type(filter.EventMessageType.ALL, priority=1)
    async def on_pick_answer(self, event: AstrMessageEvent):
        """接住选源列表后面那个纯数字回复。

        判定条件收得很紧：当前会话确实有待选列表，且整条消息就是一个范围内的序号。
        不满足就直接放行 —— 否则这个 「ALL」 钩子会吞掉群里所有聊天，
        这是同类插件最常见的翻车方式。
        """

        umo = event.unified_msg_origin
        hit = self._deps.picker.resolve(umo, event.message_str or "")
        if hit is None:
            return
        session, option = hit
        try:
            conf = await self._session_config(umo)
            reply = await self._subs.choose(umo, option.index)
            await self._recall(event, session.message_ids)
            result = await self._emit(event, reply, conf=conf)
        except Exception:  # noqa: BLE001 - 选源兜底，异常不能让消息处理链崩掉
            logger.exception("%s 选源落库失败", LOG_PREFIX)
            self._activity.error("sub", "选源落库失败，详见日志")
            yield event.plain_result("⚠️ 这个源没订上，稍后再试")
        else:
            if result is not None:
                yield result
        event.stop_event()

    @filter.command("unsub", priority=10)
    async def cmd_sub_remove(self, event: AstrMessageEvent):
        """取消一个订阅。"""

        name = self._args(event)
        if not name:
            yield event.plain_result(self._usage("unsub"))
            event.stop_event()
            return
        async for item in self._serve(
            event, "unsub", lambda umo, conf: self._subs.remove(umo, name)
        ):
            yield item

    @filter.command("sub_list", alias={"订阅列表"}, priority=10)
    async def cmd_sub_list(self, event: AstrMessageEvent):
        """列出本会话的所有订阅及其健康状况。"""

        async for item in self._serve(
            event, "sub_list", lambda umo, conf: self._subs.listing(umo), busy=BUSY_RENDER
        ):
            yield item

    @filter.command("sub_exclude", alias={"排除词"}, priority=10)
    async def cmd_sub_exclude(self, event: AstrMessageEvent):
        """管理全局排除项：命中的发布直接丢掉。"""

        args = self._args(event)
        async for item in self._serve(
            event, "sub_exclude", lambda umo, conf: self._subs.excludes(umo, args)
        ):
            yield item

    @filter.command("sub_test", priority=10)
    async def cmd_sub_test(self, event: AstrMessageEvent):
        """拉一次源看看通不通，不会把结果计入去重表。"""

        token = self._args(event)
        if not token:
            yield event.plain_result(self._usage("sub_test"))
            event.stop_event()
            return
        async for item in self._serve(
            event, "sub_test", lambda umo, conf: self._subs.test(umo, token), busy=BUSY_SEARCH
        ):
            yield item

    @filter.command("sub_stop", alias={"rss_stop", "停止RSS", "停止推送"}, priority=10)
    async def cmd_sub_stop(self, event: AstrMessageEvent):
        """暂停本会话的全部订阅推送（订阅本身还留着）。"""

        async for item in self._serve(
            event, "sub_stop", lambda umo, conf: self._subs.set_enabled(umo, False)
        ):
            yield item

    @filter.command("sub_status", alias={"推送状态", "任务状态"}, priority=10)
    async def cmd_sub_status(self, event: AstrMessageEvent):
        """调度器与订阅的整体状态。"""

        async for item in self._serve(event, "sub_status", self._status_reply):
            yield item

    @filter.command("sub_state", alias={"订阅状态"}, priority=10)
    async def cmd_sub_state(self, event: AstrMessageEvent):
        """看单个订阅的最近一次抓取结果。"""

        name = self._args(event)
        if not name:
            yield event.plain_result(self._usage("sub_state"))
            event.stop_event()
            return
        async for item in self._serve(
            event, "sub_state", lambda umo, conf: self._subs.state(umo, name)
        ):
            yield item

    @filter.command("activate_subs", priority=10)
    async def cmd_sub_activate(self, event: AstrMessageEvent):
        """恢复本会话的全部订阅推送。"""

        async for item in self._serve(
            event, "activate_subs", lambda umo, conf: self._subs.set_enabled(umo, True)
        ):
            yield item

    @filter.command("deactivate_subs", priority=10)
    async def cmd_sub_deactivate(self, event: AstrMessageEvent):
        """暂停本会话的全部订阅推送，与 「sub_stop」 等价。"""

        async for item in self._serve(
            event, "deactivate_subs", lambda umo, conf: self._subs.set_enabled(umo, False)
        ):
            yield item

    @filter.command("unsub_all", priority=10)
    async def cmd_sub_clear(self, event: AstrMessageEvent):
        """清空本会话的所有订阅，需要显式确认。

        用显式参数二次确认而不是 「session_waiter」：等待态会吃掉用户的下一条消息，
        在群里体验很差，而且插件重载时等待态会丢。
        """

        args = self._args(event).lower()
        if args not in {"确认", "confirm", "yes", "y"}:
            prefix = self._command_prefix()
            yield event.plain_result(
                "这会删掉本会话的全部订阅，且无法撤销。\n"
                f"确定的话请发：{prefix}unsub_all 确认\n"
                f"想先留个备份：{prefix}sub_export"
            )
            event.stop_event()
            return
        async for item in self._serve(event, "unsub_all", lambda umo, conf: self._subs.clear(umo)):
            yield item

    @filter.command("sub_export", priority=10)
    async def cmd_sub_export(self, event: AstrMessageEvent):
        """导出订阅与追番为 JSON，方便迁移或备份。"""

        async for item in self._serve(
            event, "sub_export", lambda umo, conf: self._subs.export(umo)
        ):
            yield item

    @filter.command("sub_import", priority=10)
    async def cmd_sub_import(self, event: AstrMessageEvent):
        """从 「sub_export」 的 JSON 恢复订阅与追番。"""

        raw = self._args(event)
        if not raw:
            yield event.plain_result(self._usage("sub_import"))
            event.stop_event()
            return
        async for item in self._serve(
            event, "sub_import", lambda umo, conf: self._subs.import_payload(umo, raw)
        ):
            yield item

    @filter.command("sub_profile", priority=10)
    async def cmd_sub_profile(self, event: AstrMessageEvent):
        """查看或设置本会话的卡片主题与渲染器。"""

        action, value = self._action_value(self._args(event))
        async for item in self._serve(
            event, "sub_profile", lambda umo, conf: self._subs.profile(umo, action, value)
        ):
            yield item

    @filter.command("sub_session", priority=10)
    async def cmd_sub_session(self, event: AstrMessageEvent):
        """查看或改写本会话的推送落点（把更新转发到别的群）。"""

        action, value = self._action_value(self._args(event))
        async for item in self._serve(
            event, "sub_session", lambda umo, conf: self._subs.session(umo, action, value)
        ):
            yield item

    @filter.command("日历订阅", priority=10)
    async def cmd_daily_digest(self, event: AstrMessageEvent):
        """本会话自助开关每日新番播报。"""

        switch = self._args(event)
        async for item in self._serve(
            event, "日历订阅", lambda umo, conf: self._subs.daily(umo, switch)
        ):
            yield item

    @filter.command("rsshelp", alias={"RSS帮助"}, priority=10)
    async def cmd_rss_help(self, event: AstrMessageEvent):
        """订阅源写法速查：mikan: / rsshub: / dmhy: 各种简写。"""

        async for item in self._serve(event, "rsshelp", lambda umo, conf: self._subs.help(umo)):
            yield item

    @filter.command("anirss", alias={"同步追番"}, priority=10)
    async def cmd_anirss(self, event: AstrMessageEvent):
        """ani-rss 同步：「/anirss」 看状态，「/anirss sync」 立刻同步，「/anirss test」 测连接。

        没拆成三条独立指令，是因为这三件事只有管理员会用，占三个词反而
        更容易和别的插件撞名。
        """

        token = (self._args(event).split() or [""])[0].lower()

        if token in {"sync", "同步"}:
            if not event.is_admin():
                yield event.plain_result("「anirss sync」 只有管理员能用")
                event.stop_event()
                return
            async for item in self._serve(
                event, "anirss sync", self._anirss_sync, busy="\u2026 正在同步 ani-rss"
            ):
                yield item
            return

        if token in {"test", "测试"}:
            if not event.is_admin():
                yield event.plain_result("「anirss test」 只有管理员能用")
                event.stop_event()
                return
            async for item in self._serve(event, "anirss test", self._anirss_test):
                yield item
            return

        async for item in self._serve(
            event, "anirss", lambda umo, conf: self._anirss.card(umo), busy=BUSY_RENDER
        ):
            yield item

    async def _anirss_sync(self, umo: str, conf: NexusConfig) -> Reply:
        """手动同步一次。目标会话缺省时就地用当前会话，免得管理员在群里点了没反应。"""

        targets = conf.anirss_sync_targets or (umo,)
        result = await self._anirss.sync(targets=tuple(targets), force=True)
        if not result.get("ok"):
            return Reply.plain(f"ani-rss 同步失败：{result.get('error') or '未知原因'}")
        return Reply.plain(
            "ani-rss 同步完成\n"
            f"· 远端条目 {result.get('total', 0)} / 在追 {result.get('active', 0)}\n"
            f"· 新增追番 {result.get('added', 0)} · 更新进度 {result.get('updated', 0)}"
            f" · 建订阅 {result.get('subscribed', 0)}\n"
            f"· 会话 {'、'.join(result.get('sessions') or ()) or '无'}"
        )

    async def _anirss_test(self, umo: str, conf: NexusConfig) -> Reply:
        """连通性自检。"""

        result = await self._anirss.test()
        if not result.get("ok"):
            return Reply.plain(
                f"连不上 ani-rss：{result.get('error') or '未知原因'}\n"
                f"地址 {result.get('base') or '未填'}"
                f" · 鉴权 {AUTH_LABEL.get(str(result.get('auth') or ''), '未设置')}"
            )
        return Reply.plain(
            "ani-rss 连接正常\n"
            f"· 地址 {result.get('base') or ''}\n"
            f"· 鉴权 {AUTH_LABEL.get(str(result.get('auth') or ''), '未设置')}\n"
            f"· 远端条目 {result.get('total', 0)}"
        )

    # ------------------------------------------------------------------
    # 指令：娱乐
    # ------------------------------------------------------------------
    @filter.command("抽番", alias={"随机番剧"}, priority=10)
    async def cmd_gacha(self, event: AstrMessageEvent):
        """从当季新番里随机抽一部，可以带题材过滤。"""

        genre = self._args(event)
        async for item in self._serve(
            event, "抽番", lambda umo, conf: self._gacha.draw(umo, genre), busy=BUSY_SEARCH
        ):
            yield item

    @filter.command("番剧推荐", priority=10)
    async def cmd_recommend(self, event: AstrMessageEvent):
        """AGE 动漫首页的近期推荐。"""

        async for item in self._serve(
            event, "番剧推荐", lambda umo, conf: self._search.recommend(umo), busy=BUSY_SEARCH
        ):
            yield item

    # ------------------------------------------------------------------
    # 指令：数据与维护
    # ------------------------------------------------------------------
    def _help_text(self) -> str:
        """帮助的纯文本版本，卡片渲染彻底失败时兜底。"""

        prefix = self._command_prefix()
        lines = [f"{PLUGIN_DISPLAY_NAME} · {PLUGIN_BRAND} {PLUGIN_VERSION}"]
        for category in catalog.CATEGORIES:
            lines.append("")
            lines.append(f"{category.icon} {category.title} —— {category.blurb}")
            for command in category.commands:
                mark = "  [管理员]" if command.admin else ""
                lines.append(f"  {prefix}{command.usage}{mark}")
                lines.append(f"     {command.summary}")
        lines.append("")
        lines.append(f"主题：{' / '.join(self._themes)}")
        lines.append(REPO_URL)
        return "\n".join(lines)

    def _help_reply(self, theme: Any) -> Reply:
        """构造帮助卡。指令数据全部来自 「catalog」，避免在这里重复维护第二份指令表。"""

        html = build_help_card(
            theme,
            prefix=self._command_prefix(),
            version=PLUGIN_VERSION,
            width=HELP_CARD_WIDTH,
            columns=3,
            footnote=REPO_URL,
        )
        text = self._help_text()
        card = make_card(
            html,
            plain=text,
            title=f"{PLUGIN_DISPLAY_NAME} 指令速查",
            eyebrow=PLUGIN_BRAND,
            subtitle=(
                f"{catalog.command_count()} 条指令 · "
                f"{catalog.category_count()} 个分类 · "
                f"{catalog.alias_count()} 个别名"
            ),
            theme=getattr(theme, "key", str(theme)),
            width=HELP_CARD_WIDTH,
        )
        return Reply(text=text, card=card)

    def _help_stream(self, event: AstrMessageEvent, requested: str) -> AsyncIterator[Any]:
        """帮助卡的完整流程，含预烘焙图快路径。

        「assets/cards/help_<主题>.webp」 是构建期就渲染好的成品，命中时直接发文件：
        帮助是最常被调用的指令，没必要每次都启一个 Chromium。
        """

        note = ""
        if requested and requested not in self._themes:
            note = f"没有「{requested}」这个主题，可选：{' / '.join(self._themes)}"

        async def worker() -> Any:
            conf = await self._session_config(event.unified_msg_origin)
            theme = resolve_theme(requested or conf.card_theme)
            if conf.card_renderer in {"auto", "html"}:
                asset = asset_card_path(theme.key)
                if asset is not None:
                    chain: list[Any] = [Comp.Image.fromFileSystem(str(asset))]
                    if note:
                        chain.append(Comp.Plain("\n" + note))
                    return event.chain_result(chain)
            return await self._emit(event, self._help_reply(theme), conf=conf, extra=note)

        return self._run(event, worker, action="帮助")

    @filter.command("番剧中枢", alias={"番剧帮助"}, priority=10)
    async def cmd_help(self, event: AstrMessageEvent):
        """指令速查卡，可以直接指定主题预览：「番剧中枢 sakura」。"""

        async for item in self._help_stream(event, self._args(event)):
            yield item

    @filter.command("番剧诊断", priority=10)
    @filter.permission_type(filter.PermissionType.ADMIN, raise_error=True)
    async def cmd_diagnose(self, event: AstrMessageEvent):
        """逐个体检数据源、渲染链与本地数据库。"""

        async for item in self._serve(
            event, "番剧诊断", lambda umo, conf: self._diagnostics.diagnose(umo), busy=BUSY_PROBE
        ):
            yield item

    @filter.command("anime_update", priority=10)
    async def cmd_anime_update(self, event: AstrMessageEvent):
        """强制刷新 anime1 番剧表缓存。"""

        async for item in self._serve(
            event,
            "anime_update",
            lambda umo, conf: self._diagnostics.refresh_anime1(umo),
            busy="\u2026 正在刷新 anime1 番剧表",
        ):
            yield item

    @filter.command("检查番剧数据", priority=10)
    async def cmd_check_data(self, event: AstrMessageEvent):
        """看各数据源缓存了多少条、什么时候更新的。"""

        async for item in self._serve(
            event, "检查番剧数据", lambda umo, conf: self._diagnostics.check_data(umo)
        ):
            yield item

    @filter.command("更新番剧数据", priority=10)
    @filter.permission_type(filter.PermissionType.ADMIN, raise_error=True)
    async def cmd_update_data(self, event: AstrMessageEvent):
        """重新拉取季度数据，可指定季度码：「更新番剧数据 202607」。"""

        code = self._args(event).replace("-", "").replace("/", "")
        async for item in self._serve(
            event,
            "更新番剧数据",
            lambda umo, conf: self._diagnostics.update_data(umo, code),
            busy="\u2026 正在重建季度数据",
        ):
            yield item

    @filter.command("bgm模板", priority=10)
    async def cmd_template(self, event: AstrMessageEvent):
        """切换搜索结果版式：1 详情卡 / 2 紧凑卡 / 3 纯文本。"""

        value = self._args(event)
        async for item in self._serve(
            event, "bgm模板", lambda umo, conf: self._search.set_template(umo, value)
        ):
            yield item

    # ------------------------------------------------------------------
    # LLM 函数工具
    # ------------------------------------------------------------------
    # 这些工具让人格化对话也能查番：用户随口问「XX 更新到第几话了」时，
    # 大模型可以自己调下面的函数，而不需要用户去记指令名。
    @staticmethod
    def _parse_anime1_range(text: str) -> tuple[str, str]:
        """把 「2026夏」/「202607」/「夏」 解析成 anime1 的 (年份, 季节)。"""

        token = str(text or "").strip()
        digits = "".join(char for char in token if char.isdigit())
        year = digits[:4] if len(digits) >= 4 else ""
        season = next((char for char in ("冬", "春", "夏", "秋") if char in token), "")
        if not season and len(digits) >= 6:
            season = _MONTH_TO_SEASON.get(digits[4:6], "")
        return year, season

    @filter.llm_tool(name="get_anime_list")
    async def tool_anime_list(
        self,
        event: AstrMessageEvent,
        time_range: str = "",
        limit: str = "",
    ) -> str:
        """Get the currently airing anime list with episode progress from anime1.me.

        Args:
            time_range(string): Optional season filter such as "2026夏" or "202607". Leave empty for the newest airing shows.
            limit(string): Optional maximum number of entries to return. Defaults to 12.
        """

        count = numeric(str(limit)) or 12
        count = max(1, min(40, count))
        year, season = self._parse_anime1_range(time_range)
        source = self._hub.anime1
        entries = (
            await source.season(year, season, limit=count)
            if (year or season)
            else await source.latest(limit=count)
        )
        if not entries:
            return "anime1.me 上没有找到符合条件的番剧。"
        header = f"anime1.me 番剧表（{year}{season} 共 {len(entries)} 条）"
        lines = [
            f"- {entry.title} | 状态 {entry.status} | {entry.year}{entry.season} | anime_id={entry.cat}"
            for entry in entries
        ]
        return "\n".join([header, *lines])

    @filter.llm_tool(name="get_watch_url")
    async def tool_watch_url(self, event: AstrMessageEvent, anime_id: str) -> str:
        """Resolve the anime1.me playback page URL for one anime id.

        Args:
            anime_id(string): The anime_id value returned by get_anime_list.
        """

        cat = numeric(str(anime_id))
        if not cat:
            return "anime_id 必须是 get_anime_list 返回的那个数字。"
        url = await self._hub.anime1.watch_url(cat)
        return f"播放页：{url}" if url else "没能解析出播放页地址。"

    @filter.llm_tool(name="search_moegirl")
    async def tool_search_moegirl(self, event: AstrMessageEvent, key_word: str) -> str:
        """Search the Moegirlpedia wiki and return the summary of the best matching entry.

        Args:
            key_word(string): Character name, anime title or any other term to look up.
        """

        keyword = self._clean(key_word)
        if not keyword:
            return "要查什么词条？"
        hit = await self._hub.moegirl.lookup(keyword)
        if hit is None:
            return f"萌娘百科上没找到「{keyword}」。"
        return f"{hit.title}\n{hit.summary}\n{hit.url}".strip()

    @filter.llm_tool(name="search_bangumi_subject")
    async def tool_search_bangumi(self, event: AstrMessageEvent, keyword: str) -> str:
        """Search Bangumi (bgm.tv) for anime subjects and return titles, scores and air dates.

        Args:
            keyword(string): Anime title or keyword to search for.
        """

        token = self._clean(keyword)
        if not token:
            return "要搜什么番？"
        subjects = await self._hub.bangumi.search(token, limit=5, subject_type=TYPE_ANIME)
        if not subjects:
            return f"Bangumi 上没搜到「{token}」。"
        lines = [
            f"- {subject.display_name} | 评分 {subject.score_label} | "
            f"开播 {subject.air_date or '未定'} | {subject.eps or subject.total_episodes or '?'} 话 | "
            f"subject_id={subject.id}"
            for subject in subjects
        ]
        return "\n".join([f"Bangumi 搜索「{token}」命中 {len(subjects)} 条", *lines])


#: 月份 → 季节字符，anime1 的番剧表按「2026 夏」这样分季。
_MONTH_TO_SEASON = {
    "01": "冬",
    "02": "冬",
    "03": "冬",
    "04": "春",
    "05": "春",
    "06": "春",
    "07": "夏",
    "08": "夏",
    "09": "夏",
    "10": "秋",
    "11": "秋",
    "12": "秋",
}

__all__ = ["BangumiNexusPlugin"]
