"""HTTP 层错误分类与负缓存的回归锁。

锁的是三个实机问题：
1. bangumi-data 未来月份 404 被当故障重试并刷 error 日志；
2. AGE 站 Cloudflare 403 被反复重试，每次刷新白等好几秒；
3. 長門番堂 证书过期时报「网络失败」，看不出真实原因。
"""

from __future__ import annotations

import httpx
import pytest

from nexus.http import (
    ABSENT_STATUS,
    QUIET_STATUS,
    REFUSED_STATUS,
    FetchError,
    HttpClient,
    browser_headers,
    is_ssl_error,
)


def test_状态码分类互不重叠() -> None:
    """404 是「没有」，403 是「不给」，两者提示语不同，不能混。"""
    assert not ABSENT_STATUS & REFUSED_STATUS
    assert QUIET_STATUS == ABSENT_STATUS | REFUSED_STATUS


def test_fetch_error_quiet_标记() -> None:
    assert FetchError("x", absent=True).quiet is True
    assert FetchError("x", refused=True).quiet is True
    assert FetchError("x", status=500).quiet is False


def test_is_ssl_error_认得证书过期() -> None:
    """httpx 把证书错误塞进 ConnectError，只能靠文本认。"""
    err = httpx.ConnectError(
        "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: certificate has expired"
    )
    assert is_ssl_error(err) is True
    assert is_ssl_error(httpx.ConnectError("Connection refused")) is False
    assert is_ssl_error(TimeoutError("timed out")) is False


def test_browser_headers_带_referer() -> None:
    """机器人 UA 会被 Cloudflare 直接 403，所以必须是浏览器 UA。"""
    headers = browser_headers("https://example.com/")
    assert "Mozilla/5.0" in headers["User-Agent"]
    assert headers["Referer"] == "https://example.com/"
    assert "Referer" not in browser_headers()


class _Recorder:
    """记录每个 URL 被请求了几次，用来证明「没有重试」和「命中负缓存」。"""

    def __init__(self, status: int) -> None:
        self.status = status
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        return httpx.Response(self.status, text="nope")


def _client_with(handler: object, client: HttpClient) -> None:
    """把 httpx 的 MockTransport 塞进已建好的连接池。"""
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_404_不重试() -> None:
    """未来月份的分片就是不存在，重试三次只是把日志刷三倍。"""
    http = HttpClient(max_retries=3)
    recorder = _Recorder(404)
    _client_with(recorder, http)
    with pytest.raises(FetchError) as caught:
        await http.fetch_json("https://example.com/2026/12.json")
    assert caught.value.absent is True
    assert recorder.calls == 1
    await http.close()


@pytest.mark.asyncio
async def test_403_不重试且提示配代理() -> None:
    """Cloudflare 拦机房 IP 时，能操作的建议只有「配代理」。"""
    http = HttpClient(max_retries=3)
    recorder = _Recorder(403)
    _client_with(recorder, http)
    with pytest.raises(FetchError) as caught:
        await http.fetch_text("https://example.com/recommend/1")
    assert caught.value.refused is True
    assert "proxy" in str(caught.value)
    assert recorder.calls == 1
    await http.close()


@pytest.mark.asyncio
async def test_安静失败进负缓存() -> None:
    """同一个 404 在缓存期内只该真的打一次网络。"""
    http = HttpClient(max_retries=2)
    recorder = _Recorder(404)
    _client_with(recorder, http)
    for _ in range(4):
        with pytest.raises(FetchError):
            await http.fetch_json("https://example.com/2026/12.json")
    assert recorder.calls == 1
    await http.close()


@pytest.mark.asyncio
async def test_500_仍然重试() -> None:
    """真故障要重试 —— 别把韧性一起优化掉了。"""
    http = HttpClient(max_retries=2)
    recorder = _Recorder(500)
    _client_with(recorder, http)
    with pytest.raises(FetchError):
        await http.fetch_text("https://example.com/boom", retries=2)
    assert recorder.calls == 3
    await http.close()
