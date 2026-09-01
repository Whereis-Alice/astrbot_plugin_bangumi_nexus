"""Webhook 事件归类与独立监听端口解析单测。

上游 Webhook 字段五花八门，「classify」是唯一的收敛点，错判会导致推送文案
完全对不上事件。ani-rss 更极端：它的 body 只能塞占位符，事件名出来是中文
动作名或 emoji，所以别名表一旦漏项，「下载完成」 就会被当成 「新集更新」，
进度回填静静失效。这里把这些边界全部钉死。

后半段测的是独立监听端口本身：裸 HTTP 解析、超时分工（读用 「READ_TIMEOUT」、
办事用 「ACK_TIMEOUT」）、以及「慢事件先回 202、后台照样发完」这条线上踩过的坑。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, cast

import pytest

from nexus.config import NexusConfig
from nexus.services import webhook
from nexus.web import listener


class _Activity:
    def info(self, scope: str, text: str) -> None: ...

    def warn(self, scope: str, text: str) -> None: ...

    def error(self, scope: str, text: str) -> None: ...


class _Bangumi:
    """封面一律搜不到 —— 解析层用例不该碰网络。"""

    async def search(self, keyword: str, *, limit: int = 1) -> list[Any]:
        return []


def _service(conf: NexusConfig) -> webhook.WebhookService:
    """只装配解析层用得到的依赖，「notifier」/「store」 都不参与。"""

    deps = cast(
        Any,
        SimpleNamespace(
            conf=conf,
            activity=_Activity(),
            hub=SimpleNamespace(bangumi=_Bangumi()),
        ),
    )
    return webhook.WebhookService(deps, notifier=cast(Any, SimpleNamespace()))


class TestClassify:
    """按显式事件名 → 错误文本 → 字段特征三级推断。"""

    @pytest.mark.parametrize("alias", ["new_episode", "new", "update", "rss_update"])
    def test_explicit_new_episode(self, alias: str) -> None:
        assert webhook.classify({"event": alias}) == "new_episode"

    @pytest.mark.parametrize(
        ("alias", "kind"),
        [
            ("download_start", "download_start"),
            ("download_started", "download_start"),
            ("start", "download_start"),
            ("download_complete", "download_complete"),
            ("download_completed", "download_complete"),
            ("download", "download_complete"),
            ("complete", "download_complete"),
            ("rename_complete", "rename_complete"),
            ("rename_completed", "rename_complete"),
            ("rename", "rename_complete"),
            ("download_error", "download_error"),
            ("download_failed", "download_error"),
            ("error", "download_error"),
            ("rss_error", "rss_error"),
            ("rss_failed", "rss_error"),
        ],
    )
    def test_all_aliases(self, alias: str, kind: str) -> None:
        assert webhook.classify({"type": alias}) == kind

    def test_alias_is_case_insensitive(self) -> None:
        assert webhook.classify({"type": "Download_Complete"}) == "download_complete"

    def test_rss_error_from_message(self) -> None:
        assert webhook.classify({"error_msg": "RSS 抓取失败"}) == "rss_error"

    def test_generic_error_from_message(self) -> None:
        assert webhook.classify({"error_msg": "磁盘满了"}) == "download_error"

    def test_file_name_implies_download_complete(self) -> None:
        assert webhook.classify({"file_name": "x.mkv"}) == "download_complete"

    def test_rename_hint_wins_over_file_name(self) -> None:
        payload = {"file_name": "x.mkv", "note": "rename done"}
        assert webhook.classify(payload) == "rename_complete"

    def test_torrent_status_start(self) -> None:
        assert webhook.classify({"torrent_name": "x", "status": "start"}) == "download_start"

    def test_empty_defaults_to_new_episode(self) -> None:
        assert webhook.classify({}) == "new_episode"


class TestKindTables:
    """文案表与进度回填集合必须与事件枚举保持同步。"""

    def test_every_kind_has_phrase(self) -> None:
        for kind in set(webhook.EVENT_ALIASES.values()):
            assert kind in webhook.KIND_PHRASE

    def test_progress_kinds(self) -> None:
        assert {"rename_complete", "download_complete"} == webhook.PROGRESS_KINDS

    def test_token_headers(self) -> None:
        assert webhook.TOKEN_HEADERS == (
            "x-webhook-token",
            "x-token",
            "authorization",
        )


class TestListenerHelpers:
    """独立端口监听器的裸 HTTP 解析：路由、长度、令牌。"""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Nexus/Notify/", "/nexus/notify"),
            ("/a?b=c", "/a"),
            ("", "/"),
        ],
    )
    def test_normalize_route(self, raw: str, expected: str) -> None:
        assert listener._normalize_route(raw) == expected

    def test_content_length(self) -> None:
        assert listener._content_length({"content-length": "12"}) == 12

    def test_content_length_missing_or_invalid(self) -> None:
        assert listener._content_length({}) == 0
        assert listener._content_length({"content-length": "abc"}) == 0

    def test_content_length_is_clamped(self) -> None:
        """超大声明值要被夹住，避免恶意 header 触发巨额分配。"""

        huge = {"content-length": str(listener.MAX_BODY_BYTES * 10)}
        assert listener._content_length(huge) == listener.MAX_BODY_BYTES + 1

    def test_token_from_bearer(self) -> None:
        assert listener._token_from({"authorization": "Bearer abc"}) == "abc"

    def test_token_from_custom_header(self) -> None:
        assert listener._token_from({"x-webhook-token": "abc"}) == "abc"

    def test_token_missing(self) -> None:
        assert listener._token_from({}) == ""


class TestPayloadShape:
    """确保 classify 能吃下真实 JSON 串反序列化出来的结构。"""

    def test_from_json_blob(self) -> None:
        raw = json.loads('{"event":"Download Completed","file_name":"a.mkv"}')
        assert webhook.classify(raw) == "download_complete"


class TestAniRssAliases:
    """ani-rss 的 「${action}」/「${emoji}」 必须能直接当事件名用。"""

    @pytest.mark.parametrize(
        ("action", "kind"),
        [
            ("开始下载", "download_start"),
            ("下载完成", "download_complete"),
            ("缺少集数", "episode_missing"),
            ("发生错误", "download_error"),
            ("订阅完结", "series_completed"),
            ("摸鱼检测", "idle_warning"),
        ],
    )
    def test_chinese_action(self, action: str, kind: str) -> None:
        assert webhook.classify({"event": action}) == kind

    @pytest.mark.parametrize(
        ("emoji", "kind"),
        [
            ("🎈", "download_start"),
            ("🎉", "download_complete"),
            ("⛔", "episode_missing"),
            ("❌", "download_error"),
            ("🎊", "series_completed"),
            ("🐟", "idle_warning"),
        ],
    )
    def test_emoji_action(self, emoji: str, kind: str) -> None:
        assert webhook.classify({"event": emoji}) == kind

    def test_completed_still_means_download(self) -> None:
        """「订阅完结」 的英文别名不能把 「complete」 系列从下载完成那边抢走。"""

        assert webhook.classify({"event": "complete"}) == "download_complete"
        assert webhook.classify({"event": "download_completed"}) == "download_complete"

    def test_series_completed_english(self) -> None:
        assert webhook.classify({"event": "series_completed"}) == "series_completed"


class TestEpisodeNumbers:
    """集数解析：ani-rss 只保证正文里有 「S01E05」，字段可能一个都没有。"""

    def test_marker_from_text(self) -> None:
        assert webhook.parse_episode_marker("药屋少女的呢喃 S02E07 下载完成") == (2, 7)

    def test_marker_is_case_insensitive(self) -> None:
        assert webhook.parse_episode_marker("s1e12") == (1, 12)

    def test_marker_absent(self) -> None:
        assert webhook.parse_episode_marker("没有集数信息") == (0, 0)

    def test_marker_empty(self) -> None:
        assert webhook.parse_episode_marker("") == (0, 0)

    def test_half_episode_truncates(self) -> None:
        """半集（总集篇）ani-rss 给 「5.5」，截断成 5 总比丢成 0 好。"""

        assert webhook._as_int("5.5") == 5

    def test_int_string_with_spaces(self) -> None:
        assert webhook._as_int(" 7 ") == 7

    def test_non_numeric(self) -> None:
        assert webhook._as_int("第七集") == 0

    def test_blank(self) -> None:
        assert webhook._as_int("") == 0


class TestBuildFromAniRss:
    """最小化的 ani-rss body 也要能长出一张完整卡片。"""

    async def test_episode_falls_back_to_message(self) -> None:
        service = _service(NexusConfig(enable_cross_match=False))
        note = await service.build(
            {
                "event": "下载完成",
                "title": "药屋少女的呢喃",
                "message": "🎉 药屋少女的呢喃 S02E07 下载完成",
            }
        )
        assert note.kind == "download_complete"
        assert note.payload["episode"] == 7
        assert note.payload["season"] == 2
        assert "进度：第 2 季第 07 集" in note.lines

    async def test_text_key_is_read(self) -> None:
        """ani-rss 的 「${text}」 常被直接塞进 「text」 字段。"""

        service = _service(NexusConfig(enable_cross_match=False))
        note = await service.build({"event": "开始下载", "title": "X", "text": "X S01E03"})
        assert note.payload["episode"] == 3

    async def test_placeholder_cover_dropped(self) -> None:
        """ani-rss 没封面时会塞占位图，原样渲染会开天窗。"""

        service = _service(NexusConfig(enable_cross_match=False))
        note = await service.build(
            {
                "event": "开始下载",
                "title": "X",
                "poster_url": "https://docs.wushuo.top/null.png",
            }
        )
        assert note.cover == ""

    async def test_real_cover_kept(self) -> None:
        service = _service(NexusConfig(enable_cross_match=False))
        note = await service.build(
            {"event": "开始下载", "title": "X", "poster_url": "https://img/a.jpg"}
        )
        assert note.cover == "https://img/a.jpg"

    async def test_subgroup_and_score_lines(self) -> None:
        service = _service(NexusConfig(enable_cross_match=False))
        note = await service.build(
            {"event": "下载完成", "title": "X", "subgroup": "Baha", "score": "8.1"}
        )
        assert "字幕组：Baha" in note.lines
        assert "评分：8.1" in note.lines

    async def test_series_completed_subtitle(self) -> None:
        service = _service(NexusConfig(enable_cross_match=False))
        note = await service.build({"event": "订阅完结", "title": "X"})
        assert note.subtitle == "本季完结"


class TestSilentKinds:
    """静默事件：只回填进度、不发卡片，写法要容错。"""

    def test_internal_id(self) -> None:
        service = _service(NexusConfig(webhook_silent_kinds=("download_complete",)))
        assert service.silent_kinds() == frozenset({"download_complete"})

    def test_chinese_name_is_folded(self) -> None:
        service = _service(NexusConfig(webhook_silent_kinds=("下载完成",)))
        assert service.silent_kinds() == frozenset({"download_complete"})

    def test_mixed_case_and_blanks(self) -> None:
        service = _service(
            NexusConfig(webhook_silent_kinds=("Download_Start", "", "  ", "开始下载"))
        )
        assert service.silent_kinds() == frozenset({"download_start"})

    def test_default_is_empty(self) -> None:
        assert _service(NexusConfig()).silent_kinds() == frozenset()

    def test_unknown_token_kept_verbatim(self) -> None:
        """认不出的写法原样留着，至少不会误伤别的事件。"""

        service = _service(NexusConfig(webhook_silent_kinds=("whatever",)))
        assert service.silent_kinds() == frozenset({"whatever"})


class TestFoldEvent:
    """事件名归一化。真实用户会在模板里写出各种意想不到的花样。"""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("🎈开始下载", "download_start"),
            ("🎉下载完成", "download_complete"),
            ("⛔缺少集数", "episode_missing"),
            ("❌发生错误", "download_error"),
            ("🎊订阅完结", "series_completed"),
            ("🐟摸鱼检测", "idle_warning"),
            ("🎈 开始下载", "download_start"),
            ("[下载完成]", "download_complete"),
            ("【下载完成】", "download_complete"),
        ],
    )
    def test_emoji_prefixed_action(self, text: str, expected: str) -> None:
        """「${emoji}${action}」 拼出来的串必须照样认得出。"""

        assert webhook.fold_event(text) == expected
        assert webhook.classify({"event": text}) == expected

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("download complete", "download_complete"),
            ("Download-Complete", "download_complete"),
            ("download started", "download_start"),
            ("  DOWNLOAD_ERROR  ", "download_error"),
        ],
    )
    def test_separator_variants(self, text: str, expected: str) -> None:
        """空格、连字符、大小写都归一化到下划线小写。"""

        assert webhook.fold_event(text) == expected

    def test_longer_alias_wins(self) -> None:
        """「download_error」 不能被更短的 「download」 抢走。"""

        assert webhook.fold_event("[download_error]") == "download_error"
        assert webhook.fold_event("rss_error") == "rss_error"
        assert webhook.fold_event("rss_update") == "new_episode"

    def test_generic_alias_needs_exact_hit(self) -> None:
        """通用短词只精确命中，一整段标题文本不该被误判成事件名。"""

        assert webhook.fold_event("start") == "download_start"
        assert webhook.fold_event("【ANi】某番 - 05 [1080P]") == ""
        assert webhook.fold_event("restart the container") == ""

    def test_unknown_returns_blank(self) -> None:
        assert webhook.fold_event("") == ""
        assert webhook.fold_event("   ") == ""
        assert webhook.fold_event("test") == ""

    def test_classify_falls_back_when_unfolded(self) -> None:
        """折不出来时还是走字段推断，不能因为归一化失败就崩。"""

        assert webhook.classify({"event": "test"}) == "new_episode"
        assert webhook.classify({"event": "unknown", "file_name": "x.mkv"}) == "download_complete"


class _Recorder:
    """把活动日志收下来，好断言 202 分支与后台失败真的留了痕迹。"""

    def __init__(self) -> None:
        self.infos: list[str] = []
        self.warns: list[str] = []

    def info(self, scope: str, text: str) -> None:
        self.infos.append(text)

    def warn(self, scope: str, text: str) -> None:
        self.warns.append(text)

    def error(self, scope: str, text: str) -> None:
        self.warns.append(text)


class _Writer:
    """「asyncio.StreamWriter」 的最小替身，只记下写出去的字节。"""

    def __init__(self, peer: str = "203.0.113.9") -> None:
        self.chunks: list[bytes] = []
        self.closed = False
        self._peer = (peer, 54321)

    def get_extra_info(self, name: str) -> Any:
        return self._peer if name == "peername" else None

    def write(self, data: bytes) -> None:
        self.chunks.append(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    @property
    def text(self) -> str:
        return b"".join(self.chunks).decode("utf-8", "replace")


def _reader(raw: bytes, *, eof: bool = True) -> asyncio.StreamReader:
    """喂真字节的流。不 「feed_eof」 就能模拟「写一半就发呆」的对端。"""

    reader = asyncio.StreamReader()
    reader.feed_data(raw)
    if eof:
        reader.feed_eof()
    return reader


def _raw(
    method: str = "POST",
    path: str = "/nexus/notify",
    *,
    headers: dict[str, str] | None = None,
    body: bytes = b"",
) -> bytes:
    """手搓一条 HTTP 请求。用真字节而不是打桩，才盯得住解析层。"""

    fields = dict(headers or {})
    if body and not any(key.lower() == "content-length" for key in fields):
        fields["Content-Length"] = str(len(body))
    head = method + " " + path + " HTTP/1.1\r\n"
    for key, value in fields.items():
        head += key + ": " + value + "\r\n"
    return head.encode("latin-1") + b"\r\n" + body


async def _ok_handler(payload: Any, *, token: str, headers: Any) -> dict[str, Any]:
    return {"ok": True, "echo": payload, "token": token}


def _listen(handler: Any, *, activity: Any = None) -> listener.WebhookListener:
    """构造一个不真正 bind 端口的监听器：这几组用例只走内部方法。"""

    return listener.WebhookListener(
        handler=handler,
        route="nexus/notify",
        port=19520,
        token_missing=False,
        activity=cast(Any, activity if activity is not None else _Activity()),
    )


class TestReadRequest:
    """裸字节 → 「_Request」 的解析边界。"""

    async def test_parses_request(self) -> None:
        raw = _raw(headers={"X-Webhook-Token": "abc"}, body=b'{"a":1}')
        request = await listener._read_request(_reader(raw))
        assert request.method == "POST"
        assert request.path == "/nexus/notify"
        assert request.headers["x-webhook-token"] == "abc"
        assert request.body == b'{"a":1}'

    async def test_query_and_fragment_dropped(self) -> None:
        request = await listener._read_request(_reader(_raw(path="/nexus/notify?x=1#f")))
        assert request.path == "/nexus/notify"

    async def test_method_is_upper_cased(self) -> None:
        request = await listener._read_request(_reader(_raw(method="get")))
        assert request.method == "GET"

    async def test_declared_body_too_large(self) -> None:
        raw = _raw(headers={"Content-Length": str(listener.MAX_BODY_BYTES * 4)})
        with pytest.raises(listener._RequestTooLarge):
            await listener._read_request(_reader(raw))

    async def test_malformed_request_line(self) -> None:
        with pytest.raises(listener._MalformedRequest):
            await listener._read_request(_reader(b"POST\r\n\r\n"))

    async def test_header_line_too_long(self) -> None:
        raw = _raw(headers={"X-Pad": "p" * (listener.MAX_LINE_BYTES + 16)})
        with pytest.raises(ValueError):
            await listener._read_request(_reader(raw))

    async def test_too_many_headers(self) -> None:
        headers = {"X-Pad-" + str(i): "v" for i in range(listener.MAX_HEADERS + 4)}
        with pytest.raises(ValueError):
            await listener._read_request(_reader(_raw(headers=headers)))

    async def test_empty_stream_is_incomplete(self) -> None:
        with pytest.raises(asyncio.IncompleteReadError):
            await listener._read_request(_reader(b""))


class TestServeOnce:
    """请求 → 「(状态码, 响应体)」 的路由与错误映射。"""

    async def test_probe_needs_no_token(self) -> None:
        lst = _listen(_ok_handler)
        status, body = await lst._serve_once(_reader(_raw(method="GET")))
        assert status == 200
        assert body["ready"] is True

    async def test_options_is_no_content(self) -> None:
        lst = _listen(_ok_handler)
        status, _ = await lst._serve_once(_reader(_raw(method="OPTIONS", path="/anything")))
        assert status == 204

    async def test_unknown_path_is_404(self) -> None:
        lst = _listen(_ok_handler)
        status, _ = await lst._serve_once(_reader(_raw(path="/nope")))
        assert status == 404

    async def test_put_is_405(self) -> None:
        lst = _listen(_ok_handler)
        status, _ = await lst._serve_once(_reader(_raw(method="PUT")))
        assert status == 405

    async def test_bad_json_is_400(self) -> None:
        lst = _listen(_ok_handler)
        status, body = await lst._serve_once(_reader(_raw(body=b"not-json")))
        assert status == 400
        assert "JSON" in body["error"]

    async def test_empty_body_becomes_object(self) -> None:
        lst = _listen(_ok_handler)
        status, body = await lst._serve_once(_reader(_raw()))
        assert status == 200
        assert body["echo"] == {}

    async def test_oversized_body_is_413(self) -> None:
        raw = _raw(headers={"Content-Length": str(listener.MAX_BODY_BYTES * 4)})
        lst = _listen(_ok_handler)
        status, _ = await lst._serve_once(_reader(raw))
        assert status == 413

    async def test_bad_request_line_is_400(self) -> None:
        lst = _listen(_ok_handler)
        status, _ = await lst._serve_once(_reader(b"OOPS\r\n\r\n"))
        assert status == 400

    async def test_long_header_is_431(self) -> None:
        raw = _raw(headers={"X-Pad": "p" * (listener.MAX_LINE_BYTES + 16)})
        lst = _listen(_ok_handler)
        status, _ = await lst._serve_once(_reader(raw))
        assert status == 431

    async def test_slow_client_is_408(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """读超时要回 408 而不是 400：是对端没写完，不是内容不合法。"""

        monkeypatch.setattr(listener, "READ_TIMEOUT", 0.05)
        lst = _listen(_ok_handler)
        reader = _reader(b"POST /nexus/notify HTTP/1.1\r\n", eof=False)
        status, body = await lst._serve_once(reader)
        assert status == 408
        assert body["error"] == "请求读取超时"

    async def test_token_and_payload_are_forwarded(self) -> None:
        seen: dict[str, Any] = {}

        async def handler(payload: Any, *, token: str, headers: Any) -> dict[str, Any]:
            seen["payload"] = payload
            seen["token"] = token
            seen["custom"] = headers.get("x-custom", "")
            return {"ok": True}

        raw = _raw(
            headers={"Authorization": "Bearer secret", "X-Custom": "1"},
            body=b'{"event":"download_complete"}',
        )
        lst = _listen(handler)
        status, _ = await lst._serve_once(_reader(raw))
        assert status == 200
        assert seen["token"] == "secret"
        assert seen["payload"]["event"] == "download_complete"
        assert seen["custom"] == "1"
        assert lst.stats()["requests"] == 1

    async def test_auth_error_is_401(self) -> None:
        async def handler(payload: Any, *, token: str, headers: Any) -> dict[str, Any]:
            raise webhook.WebhookAuthError("令牌不对")

        lst = _listen(handler)
        status, body = await lst._serve_once(_reader(_raw(body=b"{}")))
        assert status == 401
        assert body["error"] == "令牌不对"

    async def test_value_error_is_400(self) -> None:
        async def handler(payload: Any, *, token: str, headers: Any) -> dict[str, Any]:
            raise ValueError("缺少标题")

        lst = _listen(handler)
        status, body = await lst._serve_once(_reader(_raw(body=b"{}")))
        assert status == 400
        assert body["error"] == "缺少标题"


class TestDeferredInvoke:
    """这一组盯的是线上真出过的事故：人格 LLM 比读超时还慢。

    旧代码把「读请求」和「办事情」塞进同一个 15 秒 「wait_for」，于是 LLM 一慢
    就同时踩两个坑：既回 400 让推送端以为失败（可能重推），又把正在投递的
    协程取消掉（通知发一半）。现在必须是「先回 202，后台跑完」。
    """

    async def test_slow_handler_gets_202(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(listener, "ACK_TIMEOUT", 0.05)
        finished = asyncio.Event()

        async def handler(payload: Any, *, token: str, headers: Any) -> dict[str, Any]:
            await asyncio.sleep(0.15)
            finished.set()
            return {"ok": True, "late": True}

        recorder = _Recorder()
        lst = _listen(handler, activity=recorder)
        status, body = await lst._serve_once(_reader(_raw(body=b"{}")))
        assert status == 202
        assert body["pending"] is True
        assert lst.stats()["deferred"] == 1
        assert lst.stats()["pending"] == 1
        task = next(iter(lst._pending))

        # 关键断言：回过 202 之后任务必须还活着，不能被 「wait_for」 顺手取消。
        assert await asyncio.wait_for(task, timeout=2.0) == {"ok": True, "late": True}
        assert finished.is_set()
        assert lst.stats()["pending"] == 0
        assert any("202" in text for text in recorder.infos)

    async def test_deferred_failure_is_logged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """202 之后再炸，就只剩活动日志能兜底，别让它静默消失。"""

        monkeypatch.setattr(listener, "ACK_TIMEOUT", 0.05)

        async def handler(payload: Any, *, token: str, headers: Any) -> dict[str, Any]:
            await asyncio.sleep(0.1)
            raise ValueError("番剧标题为空")

        recorder = _Recorder()
        lst = _listen(handler, activity=recorder)
        status, _ = await lst._serve_once(_reader(_raw(body=b"{}")))
        assert status == 202
        task = next(iter(lst._pending))
        with pytest.raises(ValueError):
            await task
        assert any("后台处理被拒绝" in text for text in recorder.warns)

    async def test_fast_handler_is_not_deferred(self) -> None:
        lst = _listen(_ok_handler)
        status, _ = await lst._serve_once(_reader(_raw(body=b"{}")))
        assert status == 200
        assert lst.stats()["deferred"] == 0
        assert lst.stats()["pending"] == 0


class TestDrainOnStop:
    """热重载时已受理的通知宁可多等几秒，也不要留下半截状态。"""

    async def test_stop_waits_for_pending(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(listener, "ACK_TIMEOUT", 0.02)
        monkeypatch.setattr(listener, "DRAIN_TIMEOUT", 3.0)
        finished = asyncio.Event()

        async def handler(payload: Any, *, token: str, headers: Any) -> dict[str, Any]:
            await asyncio.sleep(0.1)
            finished.set()
            return {"ok": True}

        lst = _listen(handler)
        status, _ = await lst._serve_once(_reader(_raw(body=b"{}")))
        assert status == 202
        await lst.stop()
        assert finished.is_set()
        assert lst.stats()["pending"] == 0

    async def test_stop_cancels_stuck_pending(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(listener, "ACK_TIMEOUT", 0.02)
        monkeypatch.setattr(listener, "DRAIN_TIMEOUT", 0.05)

        async def handler(payload: Any, *, token: str, headers: Any) -> dict[str, Any]:
            await asyncio.sleep(30)
            return {"ok": True}

        lst = _listen(handler)
        status, _ = await lst._serve_once(_reader(_raw(body=b"{}")))
        assert status == 202
        task = next(iter(lst._pending))
        await lst.stop()
        with pytest.raises(asyncio.CancelledError):
            await task


class TestOnClient:
    """连接层：响应真写回去了，被拒的请求也真记了一笔。"""

    async def test_writes_response_and_closes(self) -> None:
        lst = _listen(_ok_handler)
        writer = _Writer()
        await lst._on_client(cast(Any, _reader(_raw(body=b"{}"))), cast(Any, writer))
        assert writer.text.startswith("HTTP/1.1 200 OK")
        assert "application/json" in writer.text
        assert writer.closed is True

    async def test_rejection_is_counted_with_peer(self) -> None:
        recorder = _Recorder()
        lst = _listen(_ok_handler, activity=recorder)
        writer = _Writer("198.51.100.7")
        await lst._on_client(cast(Any, _reader(_raw(path="/nope"))), cast(Any, writer))
        assert writer.text.startswith("HTTP/1.1 404 ")
        assert lst.stats()["errors"] == 1
        assert any("198.51.100.7" in text for text in recorder.warns)

    async def test_client_hangup_writes_nothing(self) -> None:
        """对端半途断开时别硬写响应，也别记成服务端错误。"""

        raw = b"POST /nexus/notify HTTP/1.1\r\nContent-Length: 20\r\n\r\nabc"
        lst = _listen(_ok_handler)
        writer = _Writer()
        await lst._on_client(cast(Any, _reader(raw)), cast(Any, writer))
        assert writer.chunks == []
        assert lst.stats()["errors"] == 0


class TestListenerStartGuards:
    """裸端点绝不开：这条约束比「功能可用」优先。"""

    async def test_refuses_without_token(self) -> None:
        recorder = _Recorder()
        lst = listener.WebhookListener(
            handler=_ok_handler,
            route="nexus/notify",
            port=19521,
            token_missing=True,
            activity=cast(Any, recorder),
        )
        assert await lst.start() is False
        assert lst.running is False
        assert any("webhook_token" in text for text in recorder.warns)

    async def test_port_zero_is_opt_out(self) -> None:
        lst = listener.WebhookListener(
            handler=_ok_handler, route="nexus/notify", port=0, token_missing=False
        )
        assert await lst.start() is False
        assert lst.stats()["uptime"] == 0


class TestResponseBytes:
    """裸 HTTP 响应的字节形状。"""

    def test_reason_covers_every_used_code(self) -> None:
        for code in (200, 202, 204, 400, 401, 404, 405, 408, 413, 431, 500, 503):
            assert listener._REASON[code]

    def test_no_content_has_empty_body(self) -> None:
        raw = listener._response(204, {"ignored": True}).decode("utf-8")
        assert raw.startswith("HTTP/1.1 204 No Content")
        assert "Content-Length: 0" in raw
        assert raw.endswith("\r\n\r\n")

    def test_accepted_carries_json(self) -> None:
        raw = listener._response(202, {"ok": True}).decode("utf-8")
        assert raw.startswith("HTTP/1.1 202 Accepted")
        assert raw.endswith('{"ok": true}')

    def test_peer_of_survives_broken_writer(self) -> None:
        class _Broken:
            def get_extra_info(self, name: str) -> Any:
                raise RuntimeError("连接已经没了")

        assert listener._peer_of(cast(Any, _Broken())) == ""
        assert listener._peer_of(cast(Any, _Writer("192.0.2.5"))) == "192.0.2.5"


class _Notifier:
    """会记账的 「Notifier」 替身。

    形状必须和真 Notifier 一致：「resolve_targets」 是同步的、「dispatch」 是 async 的。
    写反了的话 「targets_for」 会 await 到一个 tuple —— 线上直接炸，单测却照过。
    """

    def __init__(self) -> None:
        self.sent: list[tuple[str, tuple[str, ...]]] = []

    def resolve_targets(self, targets: Any) -> tuple[str, ...]:
        return tuple(str(item).strip() for item in targets if str(item).strip())

    async def dispatch(self, notification: Any, targets: Any) -> int:
        picked = tuple(targets)
        self.sent.append((notification.kind, picked))
        return len(picked)


class _Store:
    """只提供 「list_watch」 —— 路由层唯一用到的存储接口。"""

    def __init__(self, rows: tuple[tuple[str, str, str], ...] = ()) -> None:
        self._rows = rows

    async def list_watch(self, umo: str = "") -> list[Any]:
        return [SimpleNamespace(umo=row[0], title=row[1], status=row[2]) for row in self._rows]


GROUP_UMO = "default:GroupMessage:1091576468"
FRIEND_UMO = "default:FriendMessage:2127074778"
OTHER_UMO = "default:GroupMessage:1078946249"


def _routed(
    conf: NexusConfig,
    *,
    rows: tuple[tuple[str, str, str], ...] = (),
) -> tuple[webhook.WebhookService, _Notifier]:
    """装上记账 notifier 与追番表的服务实例，用来验证「卡片发给谁」。"""

    notifier = _Notifier()
    deps = cast(
        Any,
        SimpleNamespace(
            conf=conf,
            activity=_Activity(),
            hub=SimpleNamespace(bangumi=_Bangumi()),
            store=_Store(rows),
        ),
    )
    return webhook.WebhookService(deps, notifier=cast(Any, notifier)), notifier


class TestFixedTargets:
    """Webhook 链的固定收件人：「webhook_targets」 优先，留空退回 「push_targets」。

    拆成两个字段是为了让「下载通知进群、每日播报留私聊」这种常见搭配不用互相
    牵扯；而退回规则保证老配置（根本没有这个字段）行为一字不变。
    """

    def test_专用名单优先(self) -> None:
        service, _ = _routed(NexusConfig(webhook_targets=(GROUP_UMO,), push_targets=(FRIEND_UMO,)))
        assert service.fixed_targets() == (GROUP_UMO,)

    def test_留空退回播报名单(self) -> None:
        service, _ = _routed(NexusConfig(push_targets=(FRIEND_UMO,)))
        assert service.fixed_targets() == (FRIEND_UMO,)

    def test_两个都空就是空(self) -> None:
        service, _ = _routed(NexusConfig())
        assert service.fixed_targets() == ()

    def test_stats_报告名单与来源(self) -> None:
        own, _ = _routed(NexusConfig(webhook_targets=(GROUP_UMO,), push_targets=(FRIEND_UMO,)))
        assert own.stats()["targets"] == [GROUP_UMO]
        assert own.stats()["targets_own"] is True
        fallback, _ = _routed(NexusConfig(push_targets=(FRIEND_UMO,)))
        assert fallback.stats()["targets"] == [FRIEND_UMO]
        assert fallback.stats()["targets_own"] is False

    async def test_关掉追番联动只发固定名单(self) -> None:
        service, _ = _routed(
            NexusConfig(
                webhook_targets=(GROUP_UMO,),
                push_targets=(FRIEND_UMO,),
                webhook_notify_watchers=False,
                enable_cross_match=False,
            ),
            rows=((FRIEND_UMO, "药屋少女的呢喃", "watching"),),
        )
        note = await service.build({"event": "下载完成", "title": "药屋少女的呢喃"})
        assert await service.targets_for(note) == (GROUP_UMO,)

    async def test_打开追番联动会并上追番会话(self) -> None:
        service, _ = _routed(
            NexusConfig(
                webhook_targets=(GROUP_UMO,),
                webhook_notify_watchers=True,
                enable_cross_match=False,
            ),
            rows=(
                (FRIEND_UMO, "药屋少女的呢喃", "watching"),
                (OTHER_UMO, "跃动青春", "watching"),
            ),
        )
        note = await service.build({"event": "下载完成", "title": "药屋少女的呢喃"})
        assert await service.targets_for(note) == (GROUP_UMO, FRIEND_UMO)

    async def test_rss_错误不打扰追番会话(self) -> None:
        """抓取失败属于运维信息，只报给固定名单。"""

        service, _ = _routed(
            NexusConfig(
                webhook_targets=(GROUP_UMO,),
                webhook_notify_watchers=True,
                enable_cross_match=False,
            ),
            rows=((FRIEND_UMO, "药屋少女的呢喃", "watching"),),
        )
        note = await service.build(
            {"event": "rss_error", "title": "药屋少女的呢喃", "error_msg": "RSS 抓取失败"}
        )
        assert note.kind == "rss_error"
        assert await service.targets_for(note) == (GROUP_UMO,)

    async def test_自检发给专用名单(self) -> None:
        service, notifier = _routed(
            NexusConfig(webhook_targets=(GROUP_UMO,), push_targets=(FRIEND_UMO,))
        )
        result = await service.selftest()
        assert result == {"ok": True, "delivered": 1, "targets": 1}
        assert notifier.sent == [("test", (GROUP_UMO,))]
