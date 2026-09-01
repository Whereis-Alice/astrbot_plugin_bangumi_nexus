"""独立 Webhook 监听服务（零额外依赖）。

**为什么需要它**：AstrBot 4.25 的 Dashboard 会给所有 「/api/...」 路由挂上 JWT
鉴权中间件，豁免名单是写死的几条。插件通过 「register_web_api」 注册的路由会落到
「/api/plug/<你的路径>」 上，同样受保护 —— 也就是说 AutoBangumi 这类下载器直接
POST 过来只会拿到 401。上游 「astrbot_plugin_autobangumi_notify」 就踩在这里。

于是这里用标准库 「asyncio.start_server」 手写一个极小 HTTP 服务，只做一件事：
把指定路径上的 POST JSON 交给回调。不引入 aiohttp/fastapi，不碰 Dashboard 的
端口与鉴权，两条通道互不干扰：

* 面板通道 「/api/plug/<route>」：带登录态，适合 WebUI 里点「自测」；
* 独立通道 「http://<host>:<port>/<route>」：无登录态，给下载器直连。

**安全约束**：独立通道天然暴露在网络上，因此 「webhook_token」 为空时直接拒绝
启动并打错误日志，绝不开一个谁都能 POST 的裸端点。

Copyright (C) 2026 Whereis-Alice and AstrBot Plugin Authors.
Licensed under the GNU Affero General Public License v3.0 or later.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from astrbot.api import logger

from ..activity import ActivityLog
from ..constants import LOG_PREFIX, PLUGIN_BRAND

# 请求体上限 256KB。AutoBangumi 的事件 JSON 通常不到 2KB，
# 给足余量同时避免被大 body 拖垮内存。
MAX_BODY_BYTES = 256 * 1024

# 请求行 / 单条请求头的长度上限，防止畸形请求刷爆缓冲区。
MAX_LINE_BYTES = 8 * 1024

# 单个连接允许的请求头条数。
MAX_HEADERS = 64

# 读一整个请求的超时（秒）。慢连接不值得占着协程。
# 注意：这个上限只管「把字节读进来」，不含业务处理，见下面的 ACK_TIMEOUT。
READ_TIMEOUT = 15.0

# 业务处理的「先应答」阈值（秒）。
#
# 为什么要跟 READ_TIMEOUT 分开：一条下载完成事件的完整链路是
# 拉封面 → 调人格 LLM 转述 → 渲染卡片 → 逐会话发送，慢的时候 20~30 秒很正常；
# 而 ani-rss 这类推送端只关心「有没有被收下」，超时就判失败甚至重推。
# 早先两件事共用一个 15 秒超时，于是 LLM 一慢就同时踩两个坑：
# 既回了 400（推送端以为失败），又把投递协程取消掉（通知发一半）。
# 现在等到这个点还没跑完就先回 202「已受理」，任务用 asyncio.shield 保住，
# 后台继续跑完 —— 宁可响应少点信息，也不能把已经受理的事件丢掉。
ACK_TIMEOUT = 8.0

# 停机时等后台任务收尾的上限（秒）。超了就取消，别把插件卸载流程拖住。
DRAIN_TIMEOUT = 5.0

Handler = Callable[..., Awaitable[Mapping[str, Any]]]

_REASON = {
    200: "OK",
    202: "Accepted",
    204: "No Content",
    400: "Bad Request",
    401: "Unauthorized",
    404: "Not Found",
    405: "Method Not Allowed",
    408: "Request Timeout",
    413: "Payload Too Large",
    431: "Request Header Fields Too Large",
    500: "Internal Server Error",
    503: "Service Unavailable",
}


class WebhookListener:
    """只服务一条 Webhook 路径的极简 HTTP 服务器。

    刻意不做通用 Web 框架：不支持 keep-alive 之外的花活、不支持分块编码、
    不支持文件上传。范围越小越不容易出安全问题。
    """

    def __init__(
        self,
        *,
        handler: Handler,
        route: str,
        host: str = "0.0.0.0",
        port: int = 0,
        token_missing: bool = True,
        activity: ActivityLog | None = None,
    ) -> None:
        self._handler = handler
        self._route = _normalize_route(route)
        self._host = (host or "0.0.0.0").strip() or "0.0.0.0"
        self._port = int(port or 0)
        self._token_missing = token_missing
        self._activity = activity
        self._server: asyncio.AbstractServer | None = None
        self._serve_task: asyncio.Task[None] | None = None
        self._requests = 0
        self._errors = 0
        self._deferred = 0
        self._started_at = 0.0
        # 后台仍在跑的业务任务。必须持强引用，否则会被 GC 掉，
        # Python 只会留下一句「Task was destroyed but it is pending」。
        self._pending: set[asyncio.Task[Any]] = set()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def route(self) -> str:
        return self._route

    @property
    def port(self) -> int:
        return self._port

    async def start(self) -> bool:
        """尝试启动监听。返回是否真的在监听。

        端口为 0 表示用户没开启这条通道，属于正常情况，不打警告。
        """
        if self._server is not None:
            return True
        if self._port <= 0:
            return False
        if self._token_missing:
            logger.error(
                "%s Webhook 独立端口已配置为 %s，但没有设置 webhook_token；"
                "为避免暴露无鉴权接口，已拒绝启动。请先填写令牌。",
                LOG_PREFIX,
                self._port,
            )
            self._warn("独立监听未启动：缺少 webhook_token")
            return False
        try:
            self._server = await asyncio.start_server(
                self._on_client, host=self._host, port=self._port
            )
        except OSError as exc:  # 端口占用 / 权限不足
            logger.error("%s Webhook 端口 %s 启动失败：%s", LOG_PREFIX, self._port, exc)
            self._warn("独立监听启动失败：" + str(exc))
            return False
        self._started_at = time.time()
        self._serve_task = asyncio.create_task(self._serve())
        logger.info(
            "%s Webhook 独立监听已启动 → http://%s:%s%s",
            LOG_PREFIX,
            self._host,
            self._port,
            self._route,
        )
        self._info("独立监听已启动，端口 " + str(self._port))
        return True

    async def _serve(self) -> None:
        server = self._server
        if server is None:
            return
        try:
            async with server:
                await server.serve_forever()
        except asyncio.CancelledError:  # 正常停机
            raise
        except Exception as exc:  # noqa: BLE001 # pragma: no cover - 兜底，避免任务静默死亡
            logger.exception("%s Webhook 监听异常退出：%s", LOG_PREFIX, exc)
            self._warn("监听异常退出：" + str(exc))

    async def stop(self) -> None:
        """关闭监听。可重复调用。"""
        server, self._server = self._server, None
        task, self._serve_task = self._serve_task, None
        if server is not None:
            server.close()
            try:
                await asyncio.wait_for(server.wait_closed(), timeout=5.0)
            except Exception:  # noqa: BLE001 - 停机流程不该往外抛
                pass
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                pass
        await self._drain()

    async def _drain(self) -> None:
        """给后台通知一点收尾时间，超时才取消。

        插件热重载时会走到这里。已经受理的事件宁可多等几秒发出去，
        也不要留下「进度回填了但卡片没发」的半截状态。
        """
        pending, self._pending = set(self._pending), set()
        if not pending:
            return
        try:
            _, alive = await asyncio.wait(pending, timeout=DRAIN_TIMEOUT)
        except Exception:  # noqa: BLE001 - 停机流程不该往外抛
            return
        for item in alive:
            item.cancel()

    def stats(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "host": self._host,
            "port": self._port,
            "route": self._route,
            "requests": self._requests,
            "errors": self._errors,
            "deferred": self._deferred,
            "pending": len(self._pending),
            "uptime": int(time.time() - self._started_at) if self._started_at else 0,
        }

    # ------------------------------------------------------------------
    # 连接处理
    # ------------------------------------------------------------------
    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = _peer_of(writer)
        try:
            status, body = await self._serve_once(reader)
        except asyncio.IncompleteReadError:
            return  # 对端提前断开，直接收工
        except Exception as exc:  # noqa: BLE001 - 任何意外都要回一个响应
            logger.exception("%s Webhook 请求处理失败：%s", LOG_PREFIX, exc)
            status, body = 500, {"ok": False, "error": "内部错误"}
        if status >= 400:
            self._errors += 1
            if peer:
                self._warn("来自 " + peer + " 的请求被拒绝：" + str(status))
        try:
            writer.write(_response(status, body))
            await writer.drain()
        except Exception:  # noqa: BLE001 - 对端可能已经走了
            pass
        finally:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    async def _serve_once(self, reader: asyncio.StreamReader) -> tuple[int, dict[str, Any]]:
        """读一个请求，决定怎么回。

        「读」和「办」分开计时是这里的关键设计，别再合回去：
        读用 READ_TIMEOUT（对端慢就断），办用 ACK_TIMEOUT（业务慢就先应答）。
        """
        try:
            request = await asyncio.wait_for(_read_request(reader), timeout=READ_TIMEOUT)
        except asyncio.TimeoutError:
            # 408 比 400 更准确：是对端没把请求写完，不是内容不合法。
            return 408, {"ok": False, "error": "请求读取超时"}
        except _RequestTooLarge as exc:
            return 413, {"ok": False, "error": str(exc)}
        except _MalformedRequest as exc:
            return 400, {"ok": False, "error": str(exc)}
        except ValueError as exc:  # 请求行/请求头过长
            return 431, {"ok": False, "error": str(exc)}

        early = self._early_reply(request)
        if early is not None:
            return early
        try:
            payload = json.loads(request.body.decode("utf-8", "replace") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return 400, {"ok": False, "error": "请求体不是合法 JSON"}
        self._requests += 1
        return await self._invoke(payload, _token_from(request.headers), request.headers)

    def _early_reply(self, request: _Request) -> tuple[int, dict[str, Any]] | None:
        """纯路由判断，不碰业务。返回 None 表示「该交给业务层了」。"""
        if request.method == "OPTIONS":
            return 204, {}
        if _normalize_route(request.path) != self._route:
            return 404, {"ok": False, "error": "路径不存在"}
        if request.method in {"GET", "HEAD"}:
            # 给下载器和运维一个不需要令牌的存活探针，不泄露任何业务信息。
            return 200, {"ok": True, "service": PLUGIN_BRAND, "ready": True}
        if request.method != "POST":
            return 405, {"ok": False, "error": "只接受 POST"}
        return None

    async def _invoke(
        self,
        payload: Any,
        token: str,
        headers: Mapping[str, str],
    ) -> tuple[int, dict[str, Any]]:
        """把事件交给业务层，最多等 ACK_TIMEOUT 秒。

        超时不取消任务（asyncio.shield 挡住了 wait_for 的取消），改回 202：
        推送端只需要知道「收下了」，剩下的封面、转述、卡片可以慢慢来。
        """
        task = asyncio.ensure_future(self._handler(payload, token=token, headers=headers))
        self._pending.add(task)
        # 无论走哪条分支都要从 _pending 里摘掉，包括「等待方自己被取消」这种情况。
        task.add_done_callback(self._pending.discard)
        try:
            result = await asyncio.wait_for(asyncio.shield(task), timeout=ACK_TIMEOUT)
        except asyncio.TimeoutError:
            self._deferred += 1
            self._info(f"处理超过 {ACK_TIMEOUT:.0f} 秒，已先回 202「已受理」，通知继续在后台生成")
            # 只有转入后台的任务才需要 _settle：响应已经发走了，
            # 之后的成败没人接收，只能靠日志兜底。在 ACK_TIMEOUT 内出结果的
            # 任务由下面的返回值/异常分支负责，别在这里重复记一次账。
            task.add_done_callback(self._settle)
            return 202, {
                "ok": True,
                "accepted": True,
                "pending": True,
                "note": "已受理，正在后台生成通知",
            }
        except Exception as exc:  # noqa: BLE001 - 统一分类成 HTTP 状态
            return _classify_error(exc)
        return 200, dict(result)

    def _settle(self, task: asyncio.Task[Any]) -> None:
        """已转入后台（回过 202）的任务收尾。

        这类任务的成败没有 HTTP 响应可以承载，异常如果不在这里落一条日志
        就彻底消失了。成功则什么都不做 —— 业务层自己会往活动日志里记账。
        """
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            return
        if type(exc).__name__ == "WebhookAuthError" or isinstance(exc, ValueError):
            self._warn("后台处理被拒绝：" + str(exc))
            return
        logger.error("%s Webhook 后台处理失败：%s", LOG_PREFIX, exc, exc_info=exc)
        self._warn("后台处理失败：" + str(exc))

    # ------------------------------------------------------------------
    def _info(self, message: str) -> None:
        if self._activity is not None:
            self._activity.info("webhook", message)

    def _warn(self, message: str) -> None:
        if self._activity is not None:
            self._activity.warn("webhook", message)


# ----------------------------------------------------------------------
# 纯函数工具
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class _Request:
    """读完的一个 HTTP 请求。刻意只留业务需要的四样东西。"""

    method: str
    path: str
    headers: dict[str, str]
    body: bytes


class _MalformedRequest(Exception):
    """请求行拼不出来 —— 单独一个异常，好让上层回 400 而不是 431。"""


class _RequestTooLarge(Exception):
    """请求体超过 MAX_BODY_BYTES —— 上层回 413。"""


async def _read_request(reader: asyncio.StreamReader) -> _Request:
    """只把一个 HTTP 请求读进内存，不做任何业务判断。

    单独拆出来是为了让 READ_TIMEOUT 名副其实：它只该管慢连接，
    不该把后面调 LLM、渲染卡片的时间也算进去。
    """
    request_line = await _read_line(reader)
    if not request_line:
        raise asyncio.IncompleteReadError(b"", None)
    parts = request_line.split()
    if len(parts) < 2:
        raise _MalformedRequest("请求行无法解析")
    target = parts[1]
    headers = await _read_headers(reader)

    # 无论路径对不对，都要把 body 读掉，否则某些客户端会卡在写入上。
    length = _content_length(headers)
    if length > MAX_BODY_BYTES:
        raise _RequestTooLarge("请求体过大")
    body = await reader.readexactly(length) if length > 0 else b""
    return _Request(
        method=parts[0].upper(),
        path=target.split("?", 1)[0].split("#", 1)[0],
        headers=headers,
        body=body,
    )


def _peer_of(writer: asyncio.StreamWriter) -> str:
    """取对端 IP，仅用于日志。拿不到就返回空串，不影响服务。"""
    try:
        info = writer.get_extra_info("peername")
    except Exception:  # noqa: BLE001 - 连接可能已经没了
        return ""
    if isinstance(info, tuple) and info:
        return str(info[0])
    return ""


def _normalize_route(route: str) -> str:
    """统一成「单个前导斜杠、无尾斜杠、小写」，便于比较。"""
    text = (route or "").strip()
    text = text.split("?", 1)[0]
    text = "/" + text.strip("/")
    return text.lower()


async def _read_line(reader: asyncio.StreamReader) -> str:
    try:
        line = await reader.readuntil(b"\n")
    except asyncio.LimitOverrunError:
        raise ValueError("请求行过长") from None
    if len(line) > MAX_LINE_BYTES:
        raise ValueError("请求行过长")
    return line.decode("latin-1").strip()


async def _read_headers(reader: asyncio.StreamReader) -> dict[str, str]:
    headers: dict[str, str] = {}
    for _ in range(MAX_HEADERS):
        line = await _read_line(reader)
        if not line:
            return headers
        name, _, value = line.partition(":")
        if not _:
            continue
        headers[name.strip().lower()] = value.strip()
    raise ValueError("请求头过多")


def _content_length(headers: Mapping[str, str]) -> int:
    raw = headers.get("content-length", "0")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 0
    return max(0, min(value, MAX_BODY_BYTES + 1))


def _token_from(headers: Mapping[str, str]) -> str:
    """从常见请求头里取令牌，交给业务层做实际比对。"""
    for name in ("x-webhook-token", "x-token", "authorization"):
        value = headers.get(name, "").strip()
        if value:
            return value.removeprefix("Bearer ").removeprefix("bearer ").strip()
    return ""


def _classify_error(exc: Exception) -> tuple[int, dict[str, Any]]:
    """业务异常 → HTTP 状态码。

    用类名判断而不是 isinstance，是为了让 web 层不必反向依赖 services 层。
    """
    name = type(exc).__name__
    if name == "WebhookAuthError":
        return 401, {"ok": False, "error": str(exc) or "鉴权失败"}
    if isinstance(exc, ValueError):
        return 400, {"ok": False, "error": str(exc) or "请求内容不合法"}
    logger.exception("%s Webhook 业务处理失败：%s", LOG_PREFIX, exc)
    return 500, {"ok": False, "error": "处理失败，请查看 AstrBot 日志"}


def _response(status: int, body: Mapping[str, Any]) -> bytes:
    payload = b"" if status == 204 else json.dumps(body, ensure_ascii=False).encode("utf-8")
    lines = [
        "HTTP/1.1 " + str(status) + " " + _REASON.get(status, "OK"),
        "Content-Type: application/json; charset=utf-8",
        "Content-Length: " + str(len(payload)),
        "Connection: close",
        "Cache-Control: no-store",
        "X-Content-Type-Options: nosniff",
        "",
        "",
    ]
    return "\r\n".join(lines).encode("latin-1") + payload
