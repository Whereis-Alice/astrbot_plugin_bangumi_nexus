"""HTTP 层错误分类与负缓存的回归锁。

锁的是三个实机问题：
1. bangumi-data 未来月份 404 被当故障重试并刷 error 日志；
2. AGE 站 Cloudflare 403 被反复重试，每次刷新白等好几秒；
3. 長門番堂 证书过期时报「网络失败」，看不出真实原因；
4. 長門番堂 证书链不全时整条「新番数据」链路哑掉，且不能因此放宽所有站点。
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
    host_of,
    is_ssl_error,
    tls_relaxed,
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


def test_host_of_去端口并小写() -> None:
    """宽松名单是按主机名匹配的，端口和大小写必须先规整掉。"""
    assert host_of("https://YUC.wiki:8443/2026/07") == "yuc.wiki"
    assert host_of("not a url") == ""


def test_tls_relaxed_只放宽名单内主机及其子域() -> None:
    """锁死「后缀匹配要带点」这条：否则 「evilyuc.wiki」 也会被当成自家子域放行。"""
    assert tls_relaxed("https://yuc.wiki/2026/07") is True
    assert tls_relaxed("https://www.yuc.wiki/x") is True
    assert tls_relaxed("https://evilyuc.wiki/x") is False
    assert tls_relaxed("https://bgm.tv/subject/1") is False


class _SslThenOk:
    """第一次握手抛证书错误，之后正常返回 —— 模拟「安全池失败、宽松池成功」。"""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        raise httpx.ConnectError(
            "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed:"
            " unable to get local issuer certificate"
        )


@pytest.mark.asyncio
async def test_名单内站点证书异常时降级重试一次() -> None:
    """長門番堂 常年只挂半条证书链，降级一次就能救回整条新番数据链路。"""
    http = HttpClient(max_retries=0)
    secure = _SslThenOk()
    http._client = httpx.AsyncClient(transport=httpx.MockTransport(secure))
    http._insecure_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text="<html>ok</html>"))
    )
    text = await http.fetch_text("https://yuc.wiki/2026/07")
    assert "ok" in text
    assert secure.calls == 1  # 安全池只试一次，之后直接走宽松池
    await http.close()


@pytest.mark.asyncio
async def test_名单外站点证书异常不降级() -> None:
    """降级是给人工确认过的公开只读站点开的口子，绝不能推广到全站。"""
    http = HttpClient(max_retries=0)
    secure = _SslThenOk()
    http._client = httpx.AsyncClient(transport=httpx.MockTransport(secure))
    http._insecure_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text="leak"))
    )
    with pytest.raises(FetchError) as caught:
        await http.fetch_text("https://bgm.tv/subject/1")
    assert caught.value.ssl_error is True
    assert secure.calls == 1
    await http.close()
