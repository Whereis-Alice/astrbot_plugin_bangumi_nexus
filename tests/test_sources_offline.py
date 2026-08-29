"""离线 HTML 解析单测（AGE 动漫 / 長門番堂）。

这两个源没有 API，只能爬手写页面，是整个插件最脆的地方。
fixture 覆盖了上游真实出现过的三种封面写法与两种详情布局，
一旦解析器被改坏，这里会先炸而不是等到线上发出空卡。
"""

from __future__ import annotations

import pytest

from nexus.sources.age import parse_recommend
from nexus.sources.yuc import parse_season, weekday_label


@pytest.fixture(scope="module")
def age_items(read_fixture):
    return parse_recommend(read_fixture("age_recommend.html"))


@pytest.fixture(scope="module")
def season(read_fixture):
    return parse_season(read_fixture("yuc_season.html"), "202607")


class TestAgeRecommend:
    def test_count(self, age_items) -> None:
        assert len(age_items) == 3

    def test_title_and_progress(self, age_items) -> None:
        first = age_items[0]
        assert first.title == "葬送的芙莉莲"
        assert first.progress == "更新至第 12 集"

    def test_relative_url_gets_site_prefix(self, age_items) -> None:
        assert age_items[0].url == "https://www.agedm.io/detail/123"

    def test_absolute_url_untouched(self, age_items) -> None:
        assert age_items[1].url == "https://www.agedm.io/detail/456"

    def test_protocol_relative_cover(self, age_items) -> None:
        """「//img/x.jpg」 要补成 https，否则下载封面直接失败。"""

        assert age_items[0].cover == "https://img.example/frieren.jpg"

    def test_site_relative_cover(self, age_items) -> None:
        assert age_items[1].cover == "https://www.agedm.io/uploads/dungeon.jpg"

    def test_fallback_title_anchor(self, age_items) -> None:
        """没有 title 容器时退回第一个链接文本。"""

        assert age_items[2].title == "孤独摇滚 第二季"
        assert age_items[2].progress == ""

    def test_garbage_html(self) -> None:
        assert parse_recommend("<html><body>什么都没有</body></html>") == ()
        assert parse_recommend("") == ()


class TestYucCalendar:
    def test_titles_group_by_weekday(self, season) -> None:
        assert season.days[1] == ("葬送的芙莉莲", "迷宫饭")
        assert season.days[3] == ("孤独摇滚 第二季",)

    def test_future_titles(self, season) -> None:
        assert season.future == ("未定档新番",)

    def test_covers(self, season) -> None:
        assert season.covers["葬送的芙莉莲"] == "https://img.example/frieren.jpg"
        assert season.covers["迷宫饭"] == "https://img.example/dungeon.jpg"

    def test_weekday_label(self) -> None:
        assert weekday_label(1) == "周一"
        assert weekday_label(7) == "周日"


class TestYucDetails:
    def test_total(self, season) -> None:
        assert season.total == 2

    def test_names(self, season) -> None:
        entry = season.entries[0]
        assert entry.title_cn == "葬送的芙莉莲"
        assert entry.title_jp == "葬送のフリーレン"

    def test_category_and_genres(self, season) -> None:
        entry = season.entries[0]
        assert entry.category == "TV动画"
        assert entry.genres == ("奇幻", "冒险", "日常")

    def test_staff_pairs(self, season) -> None:
        assert season.entries[0].staff == (
            ("原作", "山田鐘人"),
            ("动画制作", "MADHOUSE"),
        )

    def test_cast_pairs(self, season) -> None:
        assert season.entries[0].cast[0] == ("芙莉莲", "种田梨沙")

    def test_official_site(self, season) -> None:
        assert season.entries[0].official_site == "https://frieren-anime.jp/"

    def test_broadcast_joins_time_and_station(self, season) -> None:
        assert season.entries[0].broadcast == "2026/07/01 23:00 日本テレビ"

    def test_broadcast_never_falls_back_to_link_text(self, season) -> None:
        """上游布局里官网与放送信息共格，兜底会把「官网」当成首播时间。"""

        for entry in season.entries:
            assert entry.broadcast != "官网"

    def test_broadcast_without_link_cell(self, season) -> None:
        assert season.entries[1].broadcast == "2026/07/02 22:30"

    def test_cover_is_joined_from_calendar(self, season) -> None:
        assert season.entries[0].cover == "https://img.example/frieren.jpg"

    def test_studio_shortcut(self, season) -> None:
        assert season.entries[0].studio == "MADHOUSE"

    def test_staff_of(self, season) -> None:
        assert season.entries[0].staff_of("原作") == "山田鐘人"


class TestYucLookup:
    def test_fuzzy_find(self, season) -> None:
        hit = season.find("芙莉莲")
        assert hit is not None
        assert hit.title_cn == "葬送的芙莉莲"

    def test_find_by_japanese_alias(self, season) -> None:
        hit = season.find("ダンジョン飯")
        assert hit is not None
        assert hit.title_cn == "迷宫饭"

    def test_find_miss(self, season) -> None:
        assert season.find("完全不存在的番") is None

    def test_weekday_of(self, season) -> None:
        assert season.weekday_of("葬送的芙莉莲") == 1
        assert season.weekday_of("孤独摇滚 第二季") == 3
        assert season.weekday_of("不存在") == 0

    def test_by_weekday_keeps_calendar_only_titles(self, season) -> None:
        """日历里有、详情表里没有的番也要占位，否则季度卡会莫名少几部。"""

        grouped = season.by_weekday()
        assert [e.display_name for e in grouped[1]] == ["葬送的芙莉莲", "迷宫饭"]
        assert [e.display_name for e in grouped[3]] == ["孤独摇滚 第二季"]

    def test_empty_page(self) -> None:
        table = parse_season("", "202607")
        assert table.total == 0
        assert table.days == {}
        assert table.find("任意") is None
