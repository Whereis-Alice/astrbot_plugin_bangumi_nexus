"""选源流程：Mikan 番组页解析、待选会话、全局排除项。

为什么单独锁一组测试：这条流程是本插件与所有上游插件最大的行为差异 ——
上游一律用 Mikan 的「关键词搜索源」，把所有字幕组、所有语言、所有画质一起收下，
一集番能推七八条。这里改成「先列字幕组、用户回序号、只订一个组」，
代价是引入了三处很容易悄悄退化的脏活：

1. 番组页 HTML 解析（页面结构一改就静默返回空，功能会无声降级）；
2. 「等一个数字回复」的有状态会话（判定放宽一点就会吞掉群里正常聊天）；
3. 排除项预设展开（同一件事字幕组能写出四种写法，只存字面量等于没过滤）。
"""

from __future__ import annotations

import pytest
from astrbot_plugin_bangumi_nexus.nexus.constants import PICK_SESSION_SECONDS
from astrbot_plugin_bangumi_nexus.nexus.models import MikanGroup
from astrbot_plugin_bangumi_nexus.nexus.services.base import expand_excludes
from astrbot_plugin_bangumi_nexus.nexus.services.picker import PickOption, PickRegistry
from astrbot_plugin_bangumi_nexus.nexus.services.subscriptions import (
    _group_detail,
    _split_action,
)
from astrbot_plugin_bangumi_nexus.nexus.sources.mikan import (
    parse_groups,
    parse_search,
    release_tags,
)


@pytest.fixture(scope="module")
def bangumi_html(read_fixture) -> str:
    """真实番组页的精简样本，保留了两个已知坑（见 fixture 内注释）。"""

    return read_fixture("mikan_bangumi.html")


@pytest.fixture(scope="module")
def groups(bangumi_html: str) -> tuple[MikanGroup, ...]:
    return parse_groups(bangumi_html)


class TestParseGroups:
    """「parse_groups」 决定选源列表上到底有哪些组、按什么顺序排。"""

    def test_order_follows_leftbar(self, groups: tuple[MikanGroup, ...]) -> None:
        """必须按左栏顺序（Mikan 自己按最近更新排），而不是按正文块顺序。

        正文块顺序在页面上并不稳定，照它排会让「最近更新的组」跑到列表末尾。
        """
        assert [group.id for group in groups] == [6, 615, 202, 370]

    def test_names_come_from_anchor_text(self, groups: tuple[MikanGroup, ...]) -> None:
        """组名取左栏链接文字，这是唯一带完整组名的地方。"""

        assert [group.name for group in groups] == [
            "雪飘工作室",
            "Kirara Fantasia",
            "生肉·不明字幕",
            "LoliHouse",
        ]

    def test_publish_group_id_is_ignored(self, groups: tuple[MikanGroup, ...]) -> None:
        """正文里 Kirara Fantasia 的发布者链接是 「PublishGroup/392」，不是 615。

        用它拼 RSS 会拿到空源，所以组 id 只认左栏的 「subgroup-{id}」。
        """
        assert 392 not in {group.id for group in groups}

    def test_updated_date_is_optional(self, groups: tuple[MikanGroup, ...]) -> None:
        """左栏日期偶尔缺失（LoliHouse 这条就没有），缺了留空而不是报错。"""

        by_id = {group.id: group for group in groups}
        assert by_id[6].updated == "2026/08/29"
        assert by_id[370].updated == ""

    def test_samples_are_matched_by_block_id(self, groups: tuple[MikanGroup, ...]) -> None:
        """样例标题必须落到对应的组，串台会让用户按错误信息选源。"""

        by_id = {group.id: group for group in groups}
        assert by_id[6].samples[0].startswith("[雪飘工作室]")
        assert len(by_id[6].samples) == 2
        assert by_id[202].samples == ()

    def test_tags_are_sniffed_from_samples(self, groups: tuple[MikanGroup, ...]) -> None:
        """标记要能看出这个组给的是繁体 + Baha 片源 + MP4 封装。"""

        by_id = {group.id: group for group in groups}
        assert by_id[615].tags == ("繁体", "1080p", "Baha", "MP4")

    def test_missing_dependency_or_bad_html_is_empty(self) -> None:
        """页面改版 / 拿到错误页时返回空元组，让上层降级回关键词搜索源。"""

        assert parse_groups("") == ()
        assert parse_groups("<html><body>404</body></html>") == ()


class TestReleaseTags:
    """「release_tags」 是选源列表上那排小标记，用来横向对比两个组的差异。"""

    def test_order_is_fixed_by_rules(self) -> None:
        """顺序由规则表固定，否则两个组的标记没法对比。"""

        titles = ("[X][1080p][简日内嵌]", "[X][720p]")
        assert release_tags(titles) == ("简体", "1080p", "720p", "内嵌")

    def test_dual_language_hits_both(self) -> None:
        """「简繁内封」 表示两种字幕都给，两个标记都要出。"""

        assert release_tags(("[X] - 01 [简繁内封字幕]",))[:2] == ("简体", "繁体")

    def test_ordinary_words_do_not_match(self) -> None:
        """裸的两字母关键词会在普通单词里误命中，这条锁住不许回退。"""

        assert release_tags(("Discovery Switch class Bass",)) == ()

    def test_empty_titles_give_no_tags(self) -> None:
        """一条发布都没抓到时不能凭空造标记。"""

        assert release_tags(()) == ()

    def test_limit_caps_the_list(self) -> None:
        """标记太多会把一行撑破，限额至少保留一个。"""

        titles = ("[1080p][720p][2160p][简体][繁体][MKV]",)
        assert len(release_tags(titles, limit=3)) == 3
        assert len(release_tags(titles, limit=0)) == 1


class TestParseSearch:
    """「parse_search」 用在 bangumi-data 没登记 mikan_id 时兜底。"""

    def test_takes_first_bangumi_id(self) -> None:
        html = '<a href="/Home/Bangumi/3883#615">x</a><a href="/Home/Bangumi/4001">y</a>'
        assert parse_search(html) == 3883

    def test_no_result_returns_zero(self) -> None:
        """搜不到时返回 0，让调用方明确知道「没有 id」而不是拿到脏值。"""

        assert parse_search("<html>没有搜索结果</html>") == 0
        assert parse_search("") == 0


def _options(count: int = 3) -> tuple[PickOption, ...]:
    return tuple(
        PickOption(index=index, label=f"组{index}", url=f"https://example.com/{index}")
        for index in range(1, count + 1)
    )


class TestPickRegistry:
    """待选会话：判定必须收紧，否则 「ALL」 消息钩子会吞掉群里所有聊天。"""

    def test_plain_number_resolves(self) -> None:
        registry = PickRegistry()
        registry.open("g1", kind="sub", name="某番", options=_options())
        hit = registry.resolve("g1", "2")
        assert hit is not None
        assert hit[1].label == "组2"

    def test_trailing_punctuation_is_tolerated(self) -> None:
        """手机输入法很容易带出句号，这种明显是选序号的写法要认。"""

        registry = PickRegistry()
        registry.open("g1", kind="sub", name="某番", options=_options())
        assert registry.resolve("g1", " 1。") is not None

    def test_sentence_containing_number_is_not_consumed(self) -> None:
        """「第 3 集好看」 必须放行 —— 这是同类插件最常见的翻车方式。"""

        registry = PickRegistry()
        registry.open("g1", kind="sub", name="某番", options=_options())
        assert registry.resolve("g1", "第 3 集好看") is None
        assert registry.resolve("g1", "3 号选手") is None

    def test_out_of_range_number_is_not_consumed(self) -> None:
        """群里聊到 「99」 时不该被当成选源，也不该报错。"""

        registry = PickRegistry()
        registry.open("g1", kind="sub", name="某番", options=_options())
        assert registry.resolve("g1", "99") is None
        assert registry.resolve("g1", "0") is None

    def test_no_session_means_nothing_is_consumed(self) -> None:
        """没发起过选源时，任何数字都只是普通聊天。"""

        assert PickRegistry().resolve("g1", "1") is None

    def test_expired_session_is_dropped(self) -> None:
        """过期会话必须失效，否则十分钟后随口一个数字会订下一个源。"""

        registry = PickRegistry()
        session = registry.open("g1", kind="sub", name="某番", options=_options())
        session.created_at -= PICK_SESSION_SECONDS + 1
        assert registry.resolve("g1", "1") is None
        assert registry.get("g1") is None

    def test_reopen_replaces_previous(self) -> None:
        """同一会话重新发起选源应顶掉上一次，避免序号对应到旧列表。"""

        registry = PickRegistry()
        registry.open("g1", kind="sub", name="旧番", options=_options())
        registry.open("g1", kind="sub", name="新番", options=_options(1))
        hit = registry.resolve("g1", "1")
        assert hit is not None
        assert hit[0].name == "新番"
        assert registry.resolve("g1", "2") is None

    def test_sessions_are_isolated_per_conversation(self) -> None:
        """A 群的待选列表不能被 B 群的数字消掉。"""

        registry = PickRegistry()
        registry.open("g1", kind="sub", name="某番", options=_options())
        assert registry.resolve("g2", "1") is None

    def test_message_ids_survive_drop(self) -> None:
        """「drop」 之后调用方仍要能拿到消息 id 去撤回列表。"""

        registry = PickRegistry()
        registry.open("g1", kind="sub", name="某番", options=_options())
        registry.note_message("g1", "12345")
        registry.note_message("g1", "")
        session = registry.drop("g1")
        assert session is not None
        assert session.message_ids == ["12345"]
        assert registry.get("g1") is None

    def test_stats_sweeps_expired(self) -> None:
        """长期运行时过期会话必须被清掉，字典不能只增不减。"""

        registry = PickRegistry()
        session = registry.open("g1", kind="sub", name="某番", options=_options())
        assert registry.stats() == {"pending": 1}
        session.created_at -= PICK_SESSION_SECONDS + 1
        assert registry.stats() == {"pending": 0}


class TestExpandExcludes:
    """排除项展开：只存字面量等于没过滤，这里锁住预设与去重行为。"""

    def test_preset_expands_to_synonyms(self) -> None:
        """用户勾的是 「繁体」，字幕组能写成 「繁日」「CHT」「BIG5」。"""

        assert expand_excludes(["繁体"]) == ("繁体", "繁日", "繁中", "CHT", "BIG5", "[TC]", "TC]")

    def test_custom_word_passes_through(self) -> None:
        """不在预设里的自定义词按原样保留。"""

        assert expand_excludes(["某某组"]) == ("某某组",)

    def test_order_is_preserved_and_deduped(self) -> None:
        """去重必须大小写不敏感，且保持勾选顺序，方便 WebUI 回显。"""

        assert expand_excludes(["MP4", "mp4", "合集"]) == ("MP4", "合集", "Batch", "BDRip")

    def test_blanks_are_dropped(self) -> None:
        """空串会匹配所有标题，一旦落库这条订阅就再也推不出东西。"""

        assert expand_excludes(["", "  ", None]) == ()  # type: ignore[list-item]


class TestSubscriptionHelpers:
    """选源列表那行小字与 「/sub_exclude」 的参数拆分。"""

    def test_group_detail_shows_date_and_sample(self) -> None:
        """给一条真实标题是刻意的：组名看不出简繁和画质，标题能。"""

        group = MikanGroup(id=6, name="雪飘", updated="2026/08/29", samples=("[雪飘][01][1080p]",))
        assert _group_detail(group) == "更新 2026/08/29 · [雪飘][01][1080p]"

    def test_group_detail_tolerates_missing_parts(self) -> None:
        """日期或样例缺失时不要留下孤零零的分隔符。"""

        assert _group_detail(MikanGroup(id=6, name="雪飘")) == ""
        assert (
            _group_detail(MikanGroup(id=6, name="雪飘", updated="2026/08/29")) == "更新 2026/08/29"
        )

    def test_label_appends_tags(self) -> None:
        """「MikanGroup.label」 是纯文本兜底时展示的一行。"""

        group = MikanGroup(id=6, name="雪飘", tags=("简体", "1080p"))
        assert group.label == "雪飘（简体 / 1080p）"

    def test_split_action_is_case_insensitive(self) -> None:
        """指令参数大小写不该影响动作识别。"""

        assert _split_action("ADD 繁体 720p") == ("add", "繁体 720p")
        assert _split_action("") == ("", "")
        assert _split_action("  list  ") == ("list", "")
