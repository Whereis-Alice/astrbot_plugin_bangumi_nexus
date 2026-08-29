"""服务层纯函数单测：文本判定、开关解析、订阅入参切分。

这些小工具散落在各指令入口，行为错一点点就会表现成
「用户明明打了开还是关着」这类难查的问题。
"""

from __future__ import annotations

import pytest

from nexus.services import base
from nexus.services.subscriptions import FEED_PREFIXES, split_target


class TestIsLongReply:
    """长文本转图判定：超过 30 字或含换行就走卡片。"""

    def test_short_stays_text(self) -> None:
        assert base.is_long_reply("今天没有新番") is False

    def test_long_becomes_card(self) -> None:
        assert base.is_long_reply("字" * 31) is True

    def test_newline_becomes_card(self) -> None:
        assert base.is_long_reply("第一行\n第二行") is True

    def test_empty(self) -> None:
        assert base.is_long_reply("") is False


class TestNumeric:
    def test_digits(self) -> None:
        assert base.numeric("12345") == 12345

    def test_non_digits(self) -> None:
        assert base.numeric("abc") == 0
        assert base.numeric("") == 0


class TestPrefBool:
    @pytest.mark.parametrize("value", ["1", "on", "true", "yes", "开", "开启"])
    def test_truthy(self, value: str) -> None:
        assert base.pref_bool(value) is True

    @pytest.mark.parametrize("value", ["0", "off", "", "关", "随便"])
    def test_falsy(self, value: str) -> None:
        assert base.pref_bool(value) is False


class TestParseSwitch:
    """三态开关：无法判定时返回 None，让调用方去回显当前状态。"""

    @pytest.mark.parametrize("value", ["开", "开启", "on", "true", "1", "启用"])
    def test_on(self, value: str) -> None:
        assert base.parse_switch(value) is True

    @pytest.mark.parametrize("value", ["关", "关闭", "off", "false", "0", "停用", "禁用"])
    def test_off(self, value: str) -> None:
        assert base.parse_switch(value) is False

    @pytest.mark.parametrize("value", ["", "也许", "toggle"])
    def test_unknown(self, value: str) -> None:
        assert base.parse_switch(value) is None


class TestMappingGet:
    """同一段代码要同时吃 dict 和对象（SDK 里两种都出现过）。"""

    def test_dict(self) -> None:
        assert base.mapping_get({"a": 1}, "a", 0) == 1

    def test_object(self) -> None:
        class Holder:
            a = 1

        assert base.mapping_get(Holder(), "a", 0) == 1

    def test_default(self) -> None:
        assert base.mapping_get({}, "a", "fallback") == "fallback"


class TestLooksJapanese:
    def test_kana(self) -> None:
        assert base.looks_japanese("ダンジョン飯") is True

    def test_chinese_only(self) -> None:
        assert base.looks_japanese("迷宫饭") is False


class TestSplitTarget:
    """「sub 名称 地址」的切分：尾段像地址才算地址，否则整串都是番名。"""

    def test_name_and_feed(self) -> None:
        assert split_target("葬送的芙莉莲 mikan:3141") == (
            "葬送的芙莉莲",
            "mikan:3141",
        )

    def test_name_only(self) -> None:
        assert split_target("葬送的芙莉莲") == ("葬送的芙莉莲", "")

    def test_multiword_name_is_kept(self) -> None:
        assert split_target("孤独摇滚 第二季") == ("孤独摇滚 第二季", "")

    def test_http_feed(self) -> None:
        name, feed = split_target("迷宫饭 https://example.com/rss.xml")
        assert name == "迷宫饭"
        assert feed == "https://example.com/rss.xml"

    def test_empty(self) -> None:
        assert split_target("") == ("", "")

    def test_every_prefix_is_recognized(self) -> None:
        for prefix in FEED_PREFIXES:
            name, feed = split_target(f"番名 {prefix}x")
            assert name == "番名"
            assert feed == f"{prefix}x"
