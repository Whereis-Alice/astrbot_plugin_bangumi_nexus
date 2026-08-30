"""共享 HTTP 客户端：一个 httpx.AsyncClient + 重试 + TTL 缓存 + 并发闸门。

上游插件里每个数据源各自 「requests.get」 一把，既阻塞事件循环，又在站点抖动时
直接把整条指令打挂。这里把网络访问收敛到一处，于是：

* 只建一个连接池，代理 / UA / 超时改一处就全生效；
* 指数退避重试，只对「值得重试」的错误重试（超时、5xx、429），4xx 立即放弃；
* 内存 TTL 缓存，日历 / 季度表这类一天变一次的资源不会被反复抓；
* 「asyncio.Semaphore」 限制并发，避免几十个封面同时下载把站点惹恼；
* 封面统一转成 base64 data URI —— 无头浏览器加载外链图片经常超时，内联最稳。

Copyright (C) 2026 Whereis-Alice and AstrBot Plugin Authors.
Licensed under the GNU Affero General Public License v3.0 or later.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import random
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .activity import ActivityLog
from .constants import BROWSER_USER_AGENT, COVER_MAX_BYTES, DEFAULT_USER_AGENT

RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 524})
# 「资源不存在」而不是「站点故障」：bangumi-data 的未来月份文件（下个季度还没发布）、
# 長門番堂 不存在的季度页都会返回 404。重试没有意义，也不该按 error 级别刷活动日志。
ABSENT_STATUS = frozenset({404, 410})
# 「上游拒绝」：风控拦截（AGE 站在 Cloudflare 后面会 403 掉机房 IP）、地区限制。
# 同样不该重试 —— 换几次都是同一个答案，只会把每次刷新拖慢好几秒。
REFUSED_STATUS = frozenset({401, 403, 451})
# 这两类一起构成「安静失败」：不重试、日志降级、结果进负缓存。
QUIET_STATUS = ABSENT_STATUS | REFUSED_STATUS
# 安静失败的负缓存时长：默认 6 小时。
# 每日播报一轮会把同一个 404 打 N 遍，负缓存能把噪音直接压成一次。
QUIET_CACHE_SECONDS = 6 * 3600
# 证书过期 / 校验失败的特征串。上游换证期间会短暂出现，单独归类才能给出
# 「这不是你的网络问题」这种有用提示，而不是混在超时里。
SSL_MARKERS = (
    "certificate verify failed",
    "certificate has expired",
    "ssl: certificate",
    "sslcertverificationerror",
    "self signed certificate",
    "hostname mismatch",
)
IMAGE_MIME = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
    "bmp": "image/bmp",
    "avif": "image/avif",
}


class FetchError(RuntimeError):
    """网络访问最终失败。带上人话描述，方便直接回给用户。"""

    def __init__(
        self,
        message: str,
        *,
        status: int = 0,
        url: str = "",
        absent: bool = False,
        refused: bool = False,
        ssl_error: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.url = url
        # 「absent」＝上游明确说没有这个资源（404 等），属预期结果不是故障
        self.absent = absent
        # 「refused」＝上游拒绝服务（风控 / 地区限制），换成提示用户配代理更有用
        self.refused = refused
        # 「ssl_error」＝证书问题，调用方可以据此提示用户而不是笼统说「网络失败」
        self.ssl_error = ssl_error

    @property
    def quiet(self) -> bool:
        """是否属于「安静失败」：不值得重试、不值得报 error。"""
        return self.absent or self.refused


def is_ssl_error(error: BaseException) -> bool:
    """判断一个异常是否属于证书问题。

    httpx 把 SSL 错误包在 「ConnectError」 里，类型上和普通连接失败没区别，
    只能看消息文本。写死在一处，避免各数据源各自 「in str(error)」。
    """
    text = f"{type(error).__name__} {error}".lower()
    return any(marker in text for marker in SSL_MARKERS)


def browser_headers(referer: str = "") -> dict[str, str]:
    """伪装成浏览器的一组请求头，供抓 HTML 的数据源使用。

    只带最基本的几项：UA、Accept、语言、以及可选的 Referer。
    Sec-Fetch-* 之类在 HTTP/2 下反而容易和 httpx 自己的头冲突，不加。
    """
    headers = {
        "User-Agent": BROWSER_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,ja;q=0.8,en;q=0.7",
        "Upgrade-Insecure-Requests": "1",
    }
    if referer:
        headers["Referer"] = referer
    return headers


@dataclass
class _CacheEntry:
    value: Any
    expires_at: float


@dataclass(frozen=True, slots=True)
class _QuietMiss:
    """被负缓存记下来的一次「安静失败」。

    存异常对象本身会连着 traceback 一起留在内存里，所以只存重建所需的几个字段。
    """

    message: str
    status: int
    url: str
    absent: bool
    refused: bool

    def rebuild(self) -> FetchError:
        return FetchError(
            self.message,
            status=self.status,
            url=self.url,
            absent=self.absent,
            refused=self.refused,
        )


class TTLCache:
    """朴素的内存缓存。容量有上限，满了先扔最早过期的那批。"""

    def __init__(self, capacity: int = 512) -> None:
        self._data: dict[str, _CacheEntry] = {}
        self._capacity = max(16, capacity)
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Any | None:
        entry = self._data.get(key)
        if entry is None:
            self.misses += 1
            return None
        if entry.expires_at <= time.time():
            self._data.pop(key, None)
            self.misses += 1
            return None
        self.hits += 1
        return entry.value

    def set(self, key: str, value: Any, ttl: float) -> None:
        if ttl <= 0:
            return
        if len(self._data) >= self._capacity:
            self._evict()
        self._data[key] = _CacheEntry(value, time.time() + ttl)

    def _evict(self) -> None:
        now = time.time()
        stale = [key for key, entry in self._data.items() if entry.expires_at <= now]
        for key in stale:
            self._data.pop(key, None)
        if len(self._data) < self._capacity:
            return
        ordered = sorted(self._data.items(), key=lambda pair: pair[1].expires_at)
        for key, _ in ordered[: max(1, len(ordered) // 4)]:
            self._data.pop(key, None)

    def invalidate(self, prefix: str = "") -> int:
        keys = [key for key in self._data if not prefix or key.startswith(prefix)]
        for key in keys:
            self._data.pop(key, None)
        return len(keys)

    def stats(self) -> dict[str, int]:
        total = self.hits + self.misses
        return {
            "entries": len(self._data),
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hits * 100 / total) if total else 0,
        }


class HttpClient:
    """插件内所有出网请求的唯一入口。"""

    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        proxy: str = "",
        timeout: float = 20.0,
        max_retries: int = 3,
        cache_ttl: float = 1800.0,
        concurrency: int = 5,
        activity: ActivityLog | None = None,
    ) -> None:
        self.user_agent = user_agent or DEFAULT_USER_AGENT
        self.proxy = proxy or ""
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        self.cache_ttl = max(0.0, cache_ttl)
        self.cache = TTLCache()
        self._activity = activity
        self._semaphore = asyncio.Semaphore(max(1, concurrency))
        self._client: httpx.AsyncClient | None = None
        # 不校验证书的备用池，只在调用方显式要求时才建（见 「client」 的注释）
        self._insecure_client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()
        self.requests = 0
        self.failures = 0

    # -- 生命周期 -----------------------------------------------------------

    def reconfigure(
        self,
        *,
        user_agent: str,
        proxy: str,
        timeout: float,
        max_retries: int,
        cache_ttl: float,
        concurrency: int,
    ) -> bool:
        """配置变更时调用。返回 True 表示连接池需要重建（由调用方 await close）。"""

        needs_rebuild = (
            user_agent != self.user_agent
            or proxy != self.proxy
            or abs(timeout - self.timeout) > 0.01
        )
        self.user_agent = user_agent or DEFAULT_USER_AGENT
        self.proxy = proxy or ""
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        self.cache_ttl = max(0.0, cache_ttl)
        self._semaphore = asyncio.Semaphore(max(1, concurrency))
        return needs_rebuild

    async def client(self, *, insecure: bool = False) -> httpx.AsyncClient:
        """取连接池。「insecure」 会拿到一个独立的、不校验证书的池。

        两个池分开而不是共用一个 「verify=False」：绝大多数请求都该校验证书，
        只有明确被用户加进白名单的站点才降级，否则就是给自己开后门。
        """
        existing = self._insecure_client if insecure else self._client
        if existing is not None and not existing.is_closed:
            return existing
        async with self._lock:
            existing = self._insecure_client if insecure else self._client
            if existing is not None and not existing.is_closed:
                return existing
            kwargs: dict[str, Any] = {
                "timeout": httpx.Timeout(self.timeout, connect=min(10.0, self.timeout)),
                "follow_redirects": True,
                "headers": {
                    "User-Agent": self.user_agent,
                    "Accept-Language": "zh-CN,zh;q=0.9,ja;q=0.8,en;q=0.7",
                },
                "limits": httpx.Limits(max_connections=16, max_keepalive_connections=8),
            }
            if self.proxy:
                kwargs["proxy"] = self.proxy
            if insecure:
                kwargs["verify"] = False
                created = httpx.AsyncClient(**kwargs)
                self._insecure_client = created
            else:
                created = httpx.AsyncClient(**kwargs)
                self._client = created
            return created

    async def close(self) -> None:
        clients = [self._client, self._insecure_client]
        self._client = None
        self._insecure_client = None
        for client in clients:
            if client is not None and not client.is_closed:
                try:
                    await client.aclose()
                except Exception:  # noqa: BLE001 # pragma: no cover - 关闭失败无所谓
                    pass

    # -- 核心请求 -----------------------------------------------------------

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
        data: Any | None = None,
        retries: int | None = None,
        expect_status: bool = True,
        allow_redirects: bool = True,
        insecure: bool = False,
    ) -> httpx.Response:
        """发一个请求，按错误性质决定重试还是当场放弃。

        「insecure」 只在调用方明确知道对方证书有问题时才置真（如上游换证期间
        证书过期），走一个独立的不校验客户端，不影响其它请求的安全性。
        """
        attempts = (self.max_retries if retries is None else max(0, retries)) + 1
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                async with self._semaphore:
                    client = await self.client(insecure=insecure)
                    self.requests += 1
                    response = await client.request(
                        method.upper(),
                        url,
                        headers=headers,
                        params=params,
                        json=json_body,
                        content=data,
                        follow_redirects=allow_redirects,
                    )
                if expect_status and response.status_code >= 400:
                    raise _status_error(response.status_code, url)
                return response
            except FetchError as error:
                last_error = error
                # 安静失败（404 / 403）当场返回：重试改变不了答案，只会拖慢调用方
                if error.quiet:
                    self._log("http", f"{url} {error}", "debug")
                    raise
                if error.status and error.status not in RETRY_STATUS:
                    self.failures += 1
                    break
            except (httpx.TimeoutException, httpx.TransportError) as error:
                last_error = error
                if is_ssl_error(error):
                    # 证书问题重试同样没用，但要单独归类好让上层给出准确提示
                    self.failures += 1
                    self._log("http", f"{url} 证书校验失败：{error}", "warn")
                    raise FetchError(f"上游证书异常（{error}）", url=url, ssl_error=True) from error
            except Exception as error:  # noqa: BLE001 # pragma: no cover - 兜底
                last_error = error
                break
            if attempt < attempts:
                delay = min(8.0, 0.6 * (2 ** (attempt - 1))) * (0.75 + random.random() * 0.5)
                self._log(
                    "http",
                    f"{url} 第 {attempt} 次失败（{last_error}），{delay:.1f}s 后重试",
                    "warn",
                )
                await asyncio.sleep(delay)
        self.failures += 1
        message = str(last_error) if last_error else "未知网络错误"
        status = getattr(last_error, "status", 0) or 0
        self._log("http", f"{url} 放弃：{message}", "error")
        raise FetchError(message, status=status, url=url)

    # -- 便捷封装 -----------------------------------------------------------

    async def fetch_json(
        self,
        url: str,
        *,
        cache_key: str | None = None,
        ttl: float | None = None,
        **kwargs: Any,
    ) -> Any:
        key = self._cache_key("json", cache_key or url, kwargs)
        cached = self.cache.get(key)
        if cached is not None:
            if isinstance(cached, _QuietMiss):
                raise cached.rebuild()
            return cached
        response = await self._quiet_cached(key, kwargs.pop("method", "GET"), url, kwargs)
        try:
            value = response.json()
        except ValueError as error:
            raise FetchError(f"返回的不是合法 JSON：{error}", url=url) from error
        self.cache.set(key, value, self.cache_ttl if ttl is None else ttl)
        return value

    async def fetch_text(
        self,
        url: str,
        *,
        cache_key: str | None = None,
        ttl: float | None = None,
        **kwargs: Any,
    ) -> str:
        key = self._cache_key("text", cache_key or url, kwargs)
        cached = self.cache.get(key)
        if cached is not None:
            if isinstance(cached, _QuietMiss):
                raise cached.rebuild()
            return cached
        response = await self._quiet_cached(key, kwargs.pop("method", "GET"), url, kwargs)
        text = response.text
        self.cache.set(key, text, self.cache_ttl if ttl is None else ttl)
        return text

    async def _quiet_cached(
        self, key: str, method: str, url: str, kwargs: dict[str, Any]
    ) -> httpx.Response:
        """发请求，并把「安静失败」也写进缓存。

        没有负缓存的话，一轮播报里同一个未发布月份的 404 会被打十几遍：
        既拖慢响应，又把活动日志刷满。既然上游明确说了「没有」，
        那这个答案在几小时内不会变。
        """
        try:
            return await self.request(method, url, **kwargs)
        except FetchError as error:
            if error.quiet:
                self.cache.set(
                    key,
                    _QuietMiss(str(error), error.status, url, error.absent, error.refused),
                    QUIET_CACHE_SECONDS,
                )
            raise

    async def fetch_bytes(self, url: str, *, limit: int = COVER_MAX_BYTES, **kwargs: Any) -> bytes:
        response = await self.request("GET", url, **kwargs)
        payload = response.content
        if len(payload) > limit:
            raise FetchError(f"文件过大（{len(payload)} 字节）", url=url)
        return payload

    async def resolve_redirect(self, url: str) -> str:
        """只取重定向目标，不下载正文（anime1 的 「?cat=」 就靠这个拿到真地址）。"""

        try:
            response = await self.request(
                "GET", url, retries=1, expect_status=False, allow_redirects=False
            )
        except FetchError:
            return url
        location = response.headers.get("location", "")
        return location or str(response.url) or url

    async def data_uri(self, url: str, *, ttl: float = 86400.0) -> str:
        """下载图片并转成 base64 data URI；失败时返回空串，调用方自行降级。"""

        if not url or url.startswith("data:"):
            return url
        key = self._cache_key("cover", url, None)
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        try:
            payload = await self.fetch_bytes(url, retries=1)
        except Exception:  # noqa: BLE001 - 封面抓不到就记一条空缓存，别每次都重试
            self.cache.set(key, "", 600.0)
            return ""
        mime = _guess_mime(url, payload)
        encoded = f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}"
        self.cache.set(key, encoded, ttl)
        return encoded

    async def data_uris(self, urls: list[str]) -> dict[str, str]:
        """批量转封面，全部并发但受同一个 Semaphore 约束。"""

        unique = [url for url in dict.fromkeys(urls) if url]
        if not unique:
            return {}
        results = await asyncio.gather(
            *(self.data_uri(url) for url in unique), return_exceptions=True
        )
        return {
            url: value
            for url, value in zip(unique, results, strict=False)
            if isinstance(value, str) and value
        }

    # -- 杂项 ---------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "failures": self.failures,
            "proxy": bool(self.proxy),
            "cache": self.cache.stats(),
        }

    @staticmethod
    def _cache_key(kind: str, seed: str, extra: Any) -> str:
        digest = hashlib.sha1(f"{seed}|{extra!r}".encode()).hexdigest()[:20]
        return f"{kind}:{digest}"

    def _log(self, scope: str, message: str, level: str = "info") -> None:
        if self._activity is not None:
            self._activity.add(scope, message, level=level)


def _status_error(status: int, url: str) -> FetchError:
    """把 HTTP 状态码翻成一条带语义标记的 「FetchError」。

    文案面向普通用户：403 直接点出「可能被风控」并给出可操作建议，
    比一句「上游返回 403」有用得多。
    """
    if status in ABSENT_STATUS:
        return FetchError(f"上游没有这个资源（{status}）", status=status, url=url, absent=True)
    if status in REFUSED_STATUS:
        return FetchError(
            f"上游拒绝访问（{status}），可能被风控或地区限制拦截，可在插件配置里填 「proxy」",
            status=status,
            url=url,
            refused=True,
        )
    return FetchError(f"上游返回 {status}", status=status, url=url)


def _guess_mime(url: str, payload: bytes) -> str:
    """先看魔数再看扩展名 —— 有些站点扩展名和真实格式并不一致。"""

    if payload[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if payload[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if payload[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return "image/webp"
    suffix = url.rsplit(".", 1)[-1].split("?")[0].lower()
    return IMAGE_MIME.get(suffix, "image/jpeg")
