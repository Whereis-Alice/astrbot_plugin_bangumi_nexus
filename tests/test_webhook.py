"""Webhook 事件归类与独立监听端口解析单测。

AutoBangumi 之类的上游 Webhook 字段五花八门，
「classify」是唯一的收敛点，错判会导致推送文案完全对不上事件。
"""

from __future__ import annotations

import json

import pytest

from nexus.services import webhook
from nexus.web import listener


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
