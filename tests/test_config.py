"""配置加载与取值收敛单测。

配置来自 AstrBot 面板，用户输入千奇百怪（中文逗号、换行、全角冒号），
「load_config / parse_times / parse_hours」必须永不抛异常，只做尽力收敛。
"""

from __future__ import annotations

import pytest

from nexus.config import GACHA_SOURCES, RENDERERS, SORT_KEYS, load_config, parse_hours, parse_times
from nexus.constants import EPISODE_PREFER_DEFAULT
from nexus.render.themes import theme_keys


class TestParseTimes:
    """播报时刻表：支持逗号 / 换行 / 中文逗号混排，非法项丢弃。"""

    def test_single(self) -> None:
        assert parse_times("08:30") == ("08:30",)

    def test_multiple_separators(self) -> None:
        assert parse_times("08:30, 20:00\n12:05") == ("08:30", "12:05", "20:00")

    def test_pads_short_form(self) -> None:
        assert parse_times("8:5") == ("08:05",)

    def test_sequence_input(self) -> None:
        assert parse_times(["08:30", "20:00"]) == ("08:30", "20:00")

    def test_invalid_falls_back_to_default(self) -> None:
        assert parse_times("不是时间") == ("08:30",)
        assert parse_times("") == ("08:30",)
        assert parse_times(None) == ("08:30",)

    def test_out_of_range_dropped(self) -> None:
        assert parse_times("25:00") == ("08:30",)
        assert parse_times("12:99") == ("08:30",)

    def test_custom_default(self) -> None:
        assert parse_times("", default=("09:00",)) == ("09:00",)


class TestParseHours:
    """anime1 刷新小时点：0-23 之外的一律丢掉，且去重排序。"""

    def test_basic(self) -> None:
        assert parse_hours("3, 15") == (3, 15)

    def test_dedupe_and_sort(self) -> None:
        assert parse_hours("15,3,15") == (3, 15)

    def test_drops_out_of_range(self) -> None:
        assert parse_hours("3, 99") == (3,)

    def test_drops_signed_tokens(self) -> None:
        """「-1」不能被抽成「1」，否则会静默变成凌晨 1 点刷新。"""

        assert parse_hours("3, -1, +5") == (3,)

    def test_tolerates_chinese_suffix(self) -> None:
        assert parse_hours("3时, 15点") == (3, 15)

    def test_empty(self) -> None:
        assert parse_hours("") == ()
        assert parse_hours(None) == ()


class TestLoadConfig:
    """空配置也要能产出完整默认值对象，这是插件首次加载的必经路径。"""

    def test_defaults(self) -> None:
        conf = load_config({}, themes=theme_keys())
        assert conf.card_theme in theme_keys()
        assert conf.card_renderer in RENDERERS
        assert conf.push_sort_by in SORT_KEYS
        assert conf.gacha_source in GACHA_SOURCES
        assert conf.card_width > 0

    def test_unknown_theme_falls_back(self) -> None:
        conf = load_config({"card_theme": "不存在的主题"}, themes=theme_keys())
        assert conf.card_theme in theme_keys()

    def test_unknown_renderer_falls_back(self) -> None:
        conf = load_config({"card_renderer": "webgl"}, themes=theme_keys())
        assert conf.card_renderer in RENDERERS

    def test_payload_is_json_friendly(self) -> None:
        payload = load_config({}, themes=theme_keys()).payload()
        assert isinstance(payload, dict)
        for key, value in payload.items():
            assert isinstance(key, str)
            assert isinstance(value, (str, int, float, bool, list, dict)), key

    def test_cache_ttl_seconds_is_positive(self) -> None:
        conf = load_config({}, themes=theme_keys())
        assert conf.cache_ttl_seconds > 0

    def test_webhook_route_strips_slashes(self) -> None:
        """路由统一存成无首尾斜杠形态，拼装 URL 时才不会出现「//」。"""

        conf = load_config({"webhook_path": "/nexus/notify/"}, themes=theme_keys())
        assert conf.webhook_route == "nexus/notify"

    def test_webhook_route_has_default(self) -> None:
        conf = load_config({"webhook_path": "   "}, themes=theme_keys())
        assert conf.webhook_route == "bangumi_nexus/notify"

    @pytest.mark.parametrize("raw", [None, "", 0, []])
    def test_survives_garbage_container(self, raw: object) -> None:
        """面板偶发传入非映射对象时不能炸，否则插件直接加载失败。"""

        conf = load_config(raw, themes=theme_keys())
        assert conf.card_theme in theme_keys()


class Test全局排除与同集归并:
    """v1.1.5 新增的三项：全局排除项 / 同集归并开关 / 归并优选顺序。"""

    def test_默认值(self) -> None:
        conf = load_config({}, themes=theme_keys())
        assert conf.global_excludes == ()
        assert conf.rss_episode_dedup is True
        assert conf.rss_episode_prefer == EPISODE_PREFER_DEFAULT

    def test_全局排除项接受字符串与数组(self) -> None:
        """面板的 list 类型有时回传字符串（用户在文本框里手打逗号），都要吃下。"""

        assert load_config(
            {"global_excludes": ["简体", " 720p "]}, themes=theme_keys()
        ).global_excludes == (
            "简体",
            "720p",
        )
        assert load_config(
            {"global_excludes": "简体, 720p"}, themes=theme_keys()
        ).global_excludes == (
            "简体",
            "720p",
        )

    def test_优选顺序里的非法标记被丢掉(self) -> None:
        conf = load_config({"rss_episode_prefer": ["baha", "不存在", "1080P"]}, themes=theme_keys())
        assert conf.rss_episode_prefer == ("Baha", "1080p")

    def test_优选顺序全非法时回落默认(self) -> None:
        """留空或全填错等于「没表态」，此时必须回落到默认序，否则归并会退化成
        「谁新留谁」，用户会莫名收到繁体版。"""

        conf = load_config({"rss_episode_prefer": ["???"]}, themes=theme_keys())
        assert conf.rss_episode_prefer == EPISODE_PREFER_DEFAULT

    def test_归并开关可关(self) -> None:
        conf = load_config({"rss_episode_dedup": False}, themes=theme_keys())
        assert conf.rss_episode_dedup is False

    def test_新字段进得了payload(self) -> None:
        """payload 是 WebUI 配置页的数据源，漏一项面板上就是空白。"""

        payload = load_config({"global_excludes": ["简体"]}, themes=theme_keys()).payload()
        assert payload["global_excludes"] == ["简体"]
        assert payload["rss_episode_prefer"] == list(EPISODE_PREFER_DEFAULT)
        assert payload["rss_episode_dedup"] is True
