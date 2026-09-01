"""Webhook 事件归类与独立监听端口解析单测。

上游 Webhook 字段五花八门，「classify」是唯一的收敛点，错判会导致推送文案
完全对不上事件。ani-rss 更极端：它的 body 只能塞占位符，事件名出来是中文
动作名或 emoji，所以别名表一旦漏项，「下载完成」 就会被当成 「新集更新」，
进度回填静静失效。这里把这些边界全部钉死。
"""

from __future__ import annotations

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
