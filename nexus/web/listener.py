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
READ_TIMEOUT = 15.0

Handler = Callable[..., Awaitable[Mapping[str, Any]]]

_REASON = {
    200: "OK",
    204: "No Content",
    400: "Bad Request",
    401: "Unauthorized",
    404: "Not Found",
    405: "Method Not Allowed",
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
        token_required: bool = True,
        activity: ActivityLog | None = None,
    ) -> None:
        self._handler = handler
        self._route = _normalize_route(route)
        self._host = (host or "0.0.0.0").strip() or "0.0.0.0"
        self._port = int(port or 0)
        self._token_required = token_required
        self._activity = activity
        self._server: asyncio.AbstractServer | None = None
        self._serve_task: asyncio.Task[None] | None = None
        self._requests = 0
        self._errors = 0
        self._started_at = 0.0

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
        if self._token_required:
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

    def stats(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "host": self._host,
            "port": self._port,
            "route": self._route,
            "requests": self._requests,
            "errors": self._errors,
            "uptime": int(time.time() - self._started_at) if self._started_at else 0,
        }

    # ------------------------------------------------------------------
    # 连接处理
    # ------------------------------------------------------------------
    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = ""
        try:
            info = writer.get_extra_info("peername")
            if isinstance(info, tuple) and info:
                peer = str(info[0])
        except Exception:  # noqa: BLE001 - 拿不到对端地址不影响服务
            peer = ""
        try:
            status, body = await asyncio.wait_for(self._dispatch(reader), timeout=READ_TIMEOUT)
        except asyncio.TimeoutError:
            status, body = 400, {"ok": False, "error": "请求读取超时"}
        except asyncio.IncompleteReadError:
            return  # 对端提前断开，直接收工
        except ValueError as exc:  # 请求行/请求头畸形
            status, body = 431, {"ok": False, "error": str(exc)}
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

    async def _dispatch(self, reader: asyncio.StreamReader) -> tuple[int, dict[str, Any]]:
        """解析一个请求并给出 (状态码, JSON 响应体)。"""
        request_line = await _read_line(reader)
        if not request_line:
            raise asyncio.IncompleteReadError(b"", None)
        parts = request_line.split()
        if len(parts) < 2:
            return 400, {"ok": False, "error": "请求行无法解析"}
        method = parts[0].upper()
        target = parts[1]
        path = target.split("?", 1)[0].split("#", 1)[0]
        headers = await _read_headers(reader)

        # 无论路径对不对，都要把 body 读掉，否则某些客户端会卡在写入上。
        length = _content_length(headers)
        if length > MAX_BODY_BYTES:
            return 413, {"ok": False, "error": "请求体过大"}
        raw_body = await reader.readexactly(length) if length > 0 else b""

        if method == "OPTIONS":
            return 204, {}
        if _normalize_route(path) != self._route:
            return 404, {"ok": False, "error": "路径不存在"}
        if method in {"GET", "HEAD"}:
            # 给下载器和运维一个不需要令牌的存活探针，不泄露任何业务信息。
            return 200, {"ok": True, "service": PLUGIN_BRAND, "ready": True}
        if method != "POST":
            return 405, {"ok": False, "error": "只接受 POST"}

        try:
            payload = json.loads(raw_body.decode("utf-8", "replace") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return 400, {"ok": False, "error": "请求体不是合法 JSON"}

        self._requests += 1
        token = _token_from(headers)
        try:
            result = await self._handler(payload, token=token, headers=headers)
        except Exception as exc:  # noqa: BLE001 - 统一分类成 HTTP 状态
            return _classify_error(exc)
        return 200, dict(result)

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
