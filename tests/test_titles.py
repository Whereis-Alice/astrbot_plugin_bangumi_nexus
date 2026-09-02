"""标题归一化 / 季度编码 / 放送解析的纯函数单测。

这些函数是跨源匹配的地基：一旦归一化规则漂移，
「anime1 ↔ Bangumi ↔ 長門番堂」三方匹配会整体错位，
所以这里用真实番名做回归锁定。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nexus import titles


class TestNormalize:
    """归一化：全角转半角、去噪声符号、季数收敛成阿拉伯数字。"""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("葬送的芙莉莲", "葬送的芙莉莲"),
            ("進撃の巨人　The Final Season", "進撃の巨人thefinalseason"),
            ("刀劍神域 第二季", "刀劍神域2"),
            ("SPY×FAMILY Season 2", "spy×family2"),
        ],
    )
    def test_normalize(self, raw: str, expected: str) -> None:
        assert titles.normalize(raw) == expected

    def test_normalize_empty(self) -> None:
        assert titles.normalize("") == ""
        assert titles.normalize("   ") == ""

    def test_half_width(self) -> None:
        assert titles.half_width("ＳＰＹ×ＦＡＭＩＬＹ ①") == "SPY×FAMILY 1"


class TestBaseTitle:
    """去季数后的主标题，用于「第 N 季」与本体互相召回。"""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("孤独摇滚！第2季", "孤独摇滚"),
            ("葬送的芙莉莲 第2期", "葬送的芙莉莲"),
            ("Blue Lock Season 2", "bluelock"),
            ("药屋少女的呢喃", "药屋少女的呢喃"),
        ],
    )
    def test_strips_season_suffix(self, raw: str, expected: str) -> None:
        """季数必须连数字一起消失。

        这里曾经期望 「孤独摇滚2」 —— 那是在给一个 bug 背书：归一化会把季度标记的
        「#」 当标点抹掉，于是 「#2」 退化成裸的 「2」，「base_title」 的正则一个都
        匹配不上，季数原封不动留在「主干」里。主干带着季数，「第 N 季与本体互相召回」
        这个用途从第一天起就是失效的。
        """

        assert titles.base_title(raw) == expected


class TestAliasKeys:
    """别名集合：过短的碎片会污染索引，必须被丢掉。"""

    def test_drops_short_alias(self) -> None:
        assert sorted(titles.alias_keys("迷宫饭", "ダンジョン飯", "a")) == [
            "ダンジョン飯",
            "迷宫饭",
        ]

    def test_dedupes(self) -> None:
        assert titles.alias_keys("迷宫饭", "迷宫饭") == {"迷宫饭"}


class TestSimilarity:
    """相似度用于阈值匹配，数值必须稳定。"""

    def test_partial_overlap(self) -> None:
        assert round(titles.similarity("进击的巨人", "进击的巨人 最终季"), 3) == 0.825

    def test_disjoint(self) -> None:
        assert titles.similarity("迷宫饭", "孤独摇滚") == 0.0

    def test_empty(self) -> None:
        assert titles.similarity("", "迷宫饭") == 0.0


class TestBestMatch:
    """从候选里挑最像的一条，返回「(候选, 分数)」。"""

    def test_picks_highest(self) -> None:
        match, score = titles.best_match("芙莉莲", ["葬送的芙莉莲", "迷宫饭"], key=lambda s: s)
        assert match == "葬送的芙莉莲"
        assert score == pytest.approx(0.72, abs=0.01)

    def test_no_candidates(self) -> None:
        assert titles.best_match("芙莉莲", [], key=lambda s: s)[0] is None


class TestSeasonCode:
    """季度编码：把任意日期映射到「YYYYMM」四季锚点。"""

    @pytest.mark.parametrize(
        ("month", "expected"),
        [(1, "202601"), (4, "202604"), (8, "202607"), (11, "202610")],
    )
    def test_season_code(self, month: int, expected: str) -> None:
        moment = datetime(2026, month, 5, tzinfo=UTC)
        assert titles.season_code(moment) == expected

    def test_season_code_current(self) -> None:
        code = titles.season_code()
        assert len(code) == 6
        assert code[4:] in {"01", "04", "07", "10"}

    def test_next_season_code(self) -> None:
        assert titles.next_season_code("202610") == "202701"
        assert titles.next_season_code("202601") == "202604"

    def test_parse_season_code(self) -> None:
        assert titles.parse_season_code("202607") == (2026, 7)
        assert titles.parse_season_code("2026夏") == (0, 0)
        assert titles.parse_season_code("bad") == (0, 0)

    def test_season_label(self) -> None:
        assert titles.season_label("202607") == "2026 年夏季"
        assert titles.season_label("202601") == "2026 年冬季"

    def test_season_codes_around(self) -> None:
        assert titles.season_codes_around("202607", 1) == ("202604", "202607", "202610")

    def test_data_months(self) -> None:
        assert titles.data_months("202607") == ((2026, 7), (2026, 8), (2026, 9))


class TestParseDatetime:
    """多源时间字符串：ISO8601 带毫秒 Z、斜杠日期都要吃得下。"""

    def test_iso_with_millis(self) -> None:
        assert titles.parse_datetime("2026-07-01T13:00:00.000Z") == datetime(
            2026, 7, 1, 13, tzinfo=UTC
        )

    def test_slash_date(self) -> None:
        assert titles.parse_datetime("2026/07/01") == datetime(2026, 7, 1, tzinfo=UTC)

    def test_garbage(self) -> None:
        assert titles.parse_datetime("x") is None
        assert titles.parse_datetime("") is None


class TestParseBroadcast:
    """bangumi-data 的「R/起始/P7D」重复规则。"""

    def test_weekly(self) -> None:
        broadcast = titles.parse_broadcast("R/2026-07-01T13:00:00.000Z/P7D")
        assert broadcast is not None
        assert broadcast.interval_days == 7
        assert broadcast.next_after(datetime(2026, 7, 10, tzinfo=UTC)) == datetime(
            2026, 7, 15, 13, tzinfo=UTC
        )

    def test_display_fields_follow_japan_time(self) -> None:
        """展示字段一律按日本时间，不跟运行机器的时区漂移。

        写死期望值就是为了防止有人把它改回 「astimezone()」：放送表的星期
        本来就按日本当地日期算，展示的钟点必须跟它同一个口径。
        """

        broadcast = titles.parse_broadcast("R/2026-07-01T13:00:00.000Z/P7D")
        assert broadcast is not None
        assert broadcast.air_weekday == 3  # UTC 周三 13:00 = 日本周三 22:00
        assert broadcast.jst_time == "22:00"
        assert broadcast.slot_label == "22:00"
        assert broadcast.label() == "周三 22:00"

    def test_invalid(self) -> None:
        assert titles.parse_broadcast("") is None
        assert titles.parse_broadcast("not-a-rule") is None


class TestHumanizeDelta:
    """相对时间文案；已经播完的场次不该再显示「多久后」。"""

    def test_future(self) -> None:
        now = datetime(2026, 7, 1, 12, tzinfo=UTC)
        target = datetime(2026, 7, 1, 13, tzinfo=UTC)
        assert titles.humanize_delta(target, now=now) == "1 小时后"

    def test_past(self) -> None:
        now = datetime(2026, 7, 1, 14, tzinfo=UTC)
        target = datetime(2026, 7, 1, 13, tzinfo=UTC)
        assert titles.humanize_delta(target, now=now) == ""
