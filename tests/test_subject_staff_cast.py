"""查番卡的制作阵容 / 声优兜底逻辑。

为什么单独锁一组测试：截图里查番卡的「制作阵容」「主要声优」两栏空白，
根因是 「長門番堂」 当天 TLS 挂了、人工整理的 「SeasonEntry」 拿不到。
补上的兜底走 Bangumi 自己的 「infobox」 与 「/characters」，而这两份原始数据
非常脏（岗位前缀、十几人并列、客串角色排在主角前面、同一声优兼多角），
所以这里逐条锁住「脏数据进来之后卡片上应该长什么样」，避免以后重构悄悄退化。
"""

from __future__ import annotations

from astrbot_plugin_bangumi_nexus.nexus.render.template import (
    _dedupe_facts,
    _merge_staff,
)
from astrbot_plugin_bangumi_nexus.nexus.services.search import _watch_caption
from astrbot_plugin_bangumi_nexus.nexus.sources.bangumi import (
    _trim_staff,
    cast_from_characters,
    staff_from_infobox,
)


class TestTrimStaff:
    """「_trim_staff」 负责把 infobox 一格压成一行能读的短文本。"""

    def test_strips_role_prefix(self) -> None:
        """infobox 常写成 「主美术：濱野英次」，冒号前缀在卡片上是噪音。"""

        assert _trim_staff("主美术：濱野英次") == "濱野英次"

    def test_keeps_first_three_and_counts_rest(self) -> None:
        """十几人并列时一行放不下，只留前三人并标注总数，读者仍知道规模。"""

        result = _trim_staff("甲、乙、丙、丁、戊")
        assert result == "甲、乙、丙 等 5 人"

    def test_accepts_mixed_separators(self) -> None:
        """同一字段里顿号 / 全角逗号 / 斜杠混用都出现过，必须一起切开。"""

        assert _trim_staff("甲，乙／丙") == "甲、乙、丙"

    def test_newlines_become_separators(self) -> None:
        """多值字段被拍平成换行时，不能整段当成一个人名。"""

        assert _trim_staff("甲\n乙") == "甲、乙"

    def test_blank_returns_empty(self) -> None:
        """只有分隔符的脏值不应该在卡片上留一个空行。"""

        assert _trim_staff("、、") == ""

    def test_clips_long_line(self) -> None:
        """单个超长人名也要截断，否则会把卡片撑破。"""

        assert len(_trim_staff("x" * 200)) == 80


class TestStaffFromInfobox:
    """「staff_from_infobox」 决定卡片「制作阵容」栏的内容与行序。"""

    def test_maps_japanese_keys_to_chinese_labels(self) -> None:
        """日文原文条目占多数，必须映射到统一的中文展示标签。"""

        rows, studio = staff_from_infobox(
            {"監督": "土田豊", "アニメーション制作": "東映アニメーション"}
        )
        assert rows == (("导演", "土田豊"), ("动画制作", "東映アニメーション"))
        assert studio == "東映アニメーション"

    def test_row_order_follows_staff_labels(self) -> None:
        """行序由 「STAFF_LABELS」 固定，不跟随 infobox 的随机顺序。"""

        rows, _ = staff_from_infobox({"音乐": "高梨康治", "原作": "东堂泉", "监督": "土田豊"})
        assert [label for label, _ in rows] == ["原作", "导演", "音乐"]

    def test_first_matching_alias_wins(self) -> None:
        """同一岗位有多种异写时取优先级最高的那个，不重复出行。"""

        rows, _ = staff_from_infobox({"导演": "甲", "監督": "乙", "総監督": "丙"})
        assert rows == (("导演", "甲"),)

    def test_missing_keys_are_skipped(self) -> None:
        """缺岗位就整行不出现，不留 「导演 -」 这种空位。"""

        rows, studio = staff_from_infobox({"话数": "48"})
        assert rows == ()
        assert studio == ""

    def test_studio_reuses_trimmed_value(self) -> None:
        """副标题用的动画制作要跟卡片上那行完全一致，避免同页两种写法。"""

        rows, studio = staff_from_infobox({"制作": "A社、B社、C社、D社"})
        assert dict(rows)["动画制作"] == studio == "A社、B社、C社 等 4 人"


class TestCastFromCharacters:
    """「cast_from_characters」 决定「主要声优」栏，原始数据里坑最多。"""

    @staticmethod
    def _entry(name: str, relation: str, voice: str) -> dict[str, object]:
        return {"name": name, "relation": relation, "actors": [{"name": voice}]}

    def test_guest_relation_never_listed(self) -> None:
        """客串角色跟本作阵容无关：柯南就出现在别番的客串位，必须排除。"""

        rows, _ = cast_from_characters(
            [
                self._entry("江戸川コナン", "客串", "高山みなみ"),
                self._entry("主角甲", "主角", "声优甲"),
            ]
        )
        assert rows == (("主角甲", "声优甲"),)

    def test_leads_sort_before_supporting(self) -> None:
        """接口返回顺序里配角可能排前面，直接截断会把主角截掉。"""

        rows, _ = cast_from_characters(
            [
                self._entry("配角甲", "配角", "声优乙"),
                self._entry("主角甲", "主角", "声优甲"),
            ]
        )
        assert [name for name, _ in rows] == ["主角甲", "配角甲"]

    def test_voice_actor_deduplicated(self) -> None:
        """一位声优兼多角时整块看起来像复读，只留第一次出现。"""

        rows, hint = cast_from_characters(
            [
                self._entry("角色甲", "主角", "声优甲"),
                self._entry("角色乙", "配角", "声优甲"),
            ]
        )
        assert rows == (("角色甲", "声优甲"),)
        assert hint == "2 位"

    def test_entries_without_actor_are_dropped(self) -> None:
        """没登记声优的角色放上卡片只是半行空白，直接跳过。"""

        rows, hint = cast_from_characters([{"name": "角色甲", "relation": "主角"}])
        assert rows == ()
        assert hint == ""

    def test_limit_caps_rows(self) -> None:
        """长番角色上百，卡片只放得下前几条。"""

        raw = [self._entry(f"角色{i}", "配角", f"声优{i}") for i in range(20)]
        rows, hint = cast_from_characters(raw, limit=3)
        assert len(rows) == 3
        assert hint == "20 位"

    def test_garbage_input_is_tolerated(self) -> None:
        """上游偶发返回 None 或字符串数组，不能让整张卡片渲染失败。"""

        assert cast_from_characters(None) == ((), "")
        assert cast_from_characters(["坏数据"]) == ((), "")


class TestMergeStaff:
    """「_merge_staff」 把人工整理的阵容与 Bangumi 兜底拼在一起。"""

    def test_primary_wins_per_label(self) -> None:
        """人工整理过的 「長門番堂」 数据更干净，同岗位不被兜底覆盖。"""

        merged = _merge_staff([("导演", "人工值")], [("导演", "兜底值")])
        assert merged == [("导演", "人工值")]

    def test_fallback_fills_missing_labels(self) -> None:
        """兜底只补人工数据没有的岗位，这样两栏合起来才完整。"""

        merged = _merge_staff([("导演", "人工值")], [("音乐", "兜底值")])
        assert merged == [("导演", "人工值"), ("音乐", "兜底值")]

    def test_limit_is_never_zero(self) -> None:
        """limit 传 0 时仍保留一行，避免调用方手滑就渲染出空栏。"""

        assert len(_merge_staff([("导演", "甲"), ("音乐", "乙")], [], limit=0)) == 1

    def test_empty_values_are_dropped(self) -> None:
        """空串来源于「字段存在但没填」，不应该占一行。"""

        assert _merge_staff([("导演", "")], [("音乐", "乙")]) == [("音乐", "乙")]


class TestDedupeFacts:
    """「_dedupe_facts」 是「条目信息」栏的去重器。"""

    def test_first_value_per_key_wins(self) -> None:
        """同一个键会被多个数据源各填一次，先到先得保证来源优先级生效。"""

        assert _dedupe_facts([("官网", "a"), ("官网", "b")]) == [("官网", "a")]

    def test_long_value_is_clipped(self) -> None:
        """超长值（比如整段官网说明）会把 kv 栏挤变形。

        「clip」 按视觉宽度而非字符数截断（CJK 记 2 宽），所以这里只锁
        「明显变短且带省略号」，不锁精确长度，免得以后调宽度就全红。
        """
        key, value = _dedupe_facts([("简介", "x" * 200)])[0]
        assert key == "简介"
        assert value.endswith("…")
        assert len(value) < 100


class TestWatchCaption:
    """「_watch_caption」 补一段图外可点的在线观看链接。"""

    def test_renders_header_and_rows(self) -> None:
        """卡片是图片、图里链接点不动，所以图外必须给纯文本。"""

        caption = _watch_caption([("Anime1", "https://anime1.me/x")])
        assert caption == "▶ 在线观看\nAnime1 https://anime1.me/x"

    def test_caps_at_five_rows(self) -> None:
        """聚合源多时链接能刷十几条，超过五条就是刷屏了。"""

        links = [(f"源{i}", f"https://e.test/{i}") for i in range(9)]
        assert len(_watch_caption(links).splitlines()) == 6

    def test_incomplete_rows_are_dropped(self) -> None:
        """缺名字或缺 URL 的半条记录点不动，留着只是噪音。"""

        assert _watch_caption([("", "https://e.test"), ("名字", "")]) == ""

    def test_no_links_means_no_caption(self) -> None:
        """没链接时不能留一个孤零零的标题行。"""

        assert _watch_caption([]) == ""
