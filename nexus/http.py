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
from .constants import COVER_MAX_BYTES, DEFAULT_USER_AGENT

RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 524})
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

    def __init__(self, message: str, *, status: int = 0, url: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.url = url


@dataclass
class _CacheEntry:
    value: Any
    expires_at: float


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

    async def client(self) -> httpx.AsyncClient:
        if self._client is not None and not self._client.is_closed:
            return self._client
        async with self._lock:
            if self._client is not None and not self._client.is_closed:
                return self._client
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
            self._client = httpx.AsyncClient(**kwargs)
            return self._client

    async def close(self) -> None:
        client, self._client = self._client, None
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
    ) -> httpx.Response:
        attempts = (self.max_retries if retries is None else max(0, retries)) + 1
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                async with self._semaphore:
                    client = await self.client()
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
                if expect_status and response.status_code in RETRY_STATUS:
                    raise FetchError(
                        f"上游返回 {response.status_code}",
                        status=response.status_code,
                        url=url,
                    )
                if expect_status and response.status_code >= 400:
                    self.failures += 1
                    raise FetchError(
                        f"上游返回 {response.status_code}",
                        status=response.status_code,
                        url=url,
                    )
                return response
            except FetchError as error:
                last_error = error
                if error.status and error.status not in RETRY_STATUS:
                    break
            except (httpx.TimeoutException, httpx.TransportError) as error:
                last_error = error
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
            return cached
        response = await self.request(kwargs.pop("method", "GET"), url, **kwargs)
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
            return cached
        response = await self.request(kwargs.pop("method", "GET"), url, **kwargs)
        text = response.text
        self.cache.set(key, text, self.cache_ttl if ttl is None else ttl)
        return text

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
