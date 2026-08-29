"""指令入口的参数解析单测。

这些静态方法决定了「用户到底输入了什么」，
解析偏一点就会表现成「明明打了番名却说参数为空」，
所以连零宽字符、纯数字这类边角输入都要锁住。
"""

from __future__ import annotations

import pytest
from conftest import plugin_module

main = plugin_module("main")
TRIGGERS = main.TRIGGERS
Plugin = main.BangumiNexusPlugin


class FakeEvent:
    """只提供 「message_str」 的最小事件替身，避免拉起整个 SDK 事件体系。"""

    def __init__(self, message_str: str) -> None:
        self.message_str = message_str


class TestClean:
    def test_strips_zero_width(self) -> None:
        assert Plugin._clean(" \u200b葬送的芙莉莲 ") == "葬送的芙莉莲"

    def test_strips_bom_and_joiners(self) -> None:
        assert Plugin._clean("\ufeff迷宫\u200d饭") == "迷宫饭"

    def test_none_safe(self) -> None:
        assert Plugin._clean("") == ""
        assert Plugin._clean(None) == ""


class TestArgs:
    """剥前缀与触发词后剩下的参数尾巴。"""

    def test_strips_slash_and_trigger(self) -> None:
        assert Plugin._args(FakeEvent("/查番 葬送的芙莉莲")) == "葬送的芙莉莲"

    @pytest.mark.parametrize("prefix", ["/", "!", "#", ".", "。", "、", ""])
    def test_all_wake_prefixes(self, prefix: str) -> None:
        assert Plugin._args(FakeEvent(f"{prefix}查番 迷宫饭")) == "迷宫饭"

    def test_longest_trigger_wins(self) -> None:
        """「bgm番剧」 必须先于 「bgm」 匹配，否则参数里会残留「番剧」。"""

        assert Plugin._args(FakeEvent("/bgm番剧 迷宫饭")) == "迷宫饭"

    def test_no_args(self) -> None:
        assert Plugin._args(FakeEvent("/查番")) == ""

    def test_empty_message(self) -> None:
        assert Plugin._args(FakeEvent("")) == ""

    def test_unknown_command_returns_body(self) -> None:
        assert Plugin._args(FakeEvent("/未知指令 参数")) == "未知指令 参数"

    def test_triggers_sorted_by_length_desc(self) -> None:
        lengths = [len(t) for t in TRIGGERS]
        assert lengths == sorted(lengths, reverse=True)


class TestSplitLimit:
    """「名称 + 数量」 切分：单个 token 永远是名称，不能被当成数量。"""

    def test_name_and_limit(self) -> None:
        assert Plugin._split_limit("迷宫饭 5") == ("迷宫饭", 5)

    def test_single_numeric_token_is_name(self) -> None:
        assert Plugin._split_limit("12345") == ("12345", 0)

    def test_name_only(self) -> None:
        assert Plugin._split_limit("迷宫饭") == ("迷宫饭", 0)

    def test_empty(self) -> None:
        assert Plugin._split_limit("") == ("", 0)

    def test_trailing_non_numeric_stays_in_name(self) -> None:
        assert Plugin._split_limit("孤独摇滚 第二季") == ("孤独摇滚 第二季", 0)


class TestActionValue:
    """「get / set 值」 子命令切分，动作统一小写。"""

    def test_set_with_value(self) -> None:
        assert Plugin._action_value("set 08:30") == ("set", "08:30")

    def test_get_uppercase(self) -> None:
        assert Plugin._action_value("GET") == ("get", "")

    def test_empty(self) -> None:
        assert Plugin._action_value("") == ("", "")


class TestParseAnime1Range:
    """anime1 的年 / 季筛选：接受 「2026夏」 「202607」 「夏」 三种写法。"""

    def test_year_plus_season(self) -> None:
        assert Plugin._parse_anime1_range("2026夏") == ("2026", "夏")

    def test_season_code(self) -> None:
        assert Plugin._parse_anime1_range("202607") == ("2026", "夏")

    def test_season_only(self) -> None:
        assert Plugin._parse_anime1_range("夏") == ("", "夏")

    def test_year_only(self) -> None:
        year, season = Plugin._parse_anime1_range("2026")
        assert year == "2026"
        assert season == ""

    def test_empty(self) -> None:
        assert Plugin._parse_anime1_range("") == ("", "")


class TestTodayIndex:
    def test_in_range(self) -> None:
        assert 1 <= Plugin._today_index() <= 7
