"""Webhook 请求体容错解析的单测。

**为什么值得单开一个文件**：这一层是给 ani-rss 那类「字符串模板拼 JSON」的推送端兜底的，
尺度两头都得卡死 —— 救得太狠会端出一份面目全非的 payload，救得太怂用户就只能对着几十条
400 发懵。所以正反两面一起钉：该救的一处不漏，不敢确定的一律照旧拒绝，而且救回来的值
必须和转义前一模一样（多转一层，卡片里就会冒出字面的反斜杠 n）。

Copyright (C) 2026 Whereis-Alice and AstrBot Plugin Authors.
Licensed under the GNU Affero General Public License v3.0 or later.
"""

from __future__ import annotations

import json

import pytest

from nexus.web import jsonbody
from nexus.web.jsonbody import parse_body, preview

# ani-rss 3.2.22 的默认模板漏掉 「${message}」 那一对引号之后，真正打过来的 body。
# v1.2.7 之前它每一条都会被回 400，而用户那头只看到一句「通知失败」。
_ANIRSS_BROKEN = (
    '{"event":"下载完成","title":"药屋少女的呢喃","season":"2","episode":"3",'
    '"poster_url":"https://example.com/p.jpg","url":"https://bgm.tv/subject/1",'
    '"subgroup":"桜都字幕组","score":"8.1",'
    '"message":下载完成 药屋少女的呢喃 第 3 集}'
)


class TestStrictParse:
    """合法 JSON 必须走原路：内容一个字都不许动，「repairs」 也得是空的。"""

    def test_plain_object(self) -> None:
        parsed = parse_body('{"event":"download_complete","episode":"3"}')
        assert parsed.payload == {"event": "download_complete", "episode": "3"}
        assert parsed.repairs == ()

    def test_escaped_message_is_untouched(self) -> None:
        """模板填对时的样子：转义序列还原成真字符，且不留容错痕迹。"""

        parsed = parse_body(r'{"message":"新集更新\n第 3 集 \"OP\""}')
        assert parsed.payload["message"] == '新集更新\n第 3 集 "OP"'
        assert parsed.repairs == ()

    def test_empty_body_is_object(self) -> None:
        """空 body 是探针的常见形状，不该报错。"""

        assert parse_body("").payload == {}
        assert parse_body("   \n  ").payload == {}

    def test_parsed_body_defaults_to_no_repairs(self) -> None:
        assert jsonbody.ParsedBody({"a": 1}).repairs == ()


class TestControlChars:
    """手抄模板最常翻的一种车：字符串里直接躺着一个真换行。"""

    def test_raw_newline_is_rescued(self) -> None:
        parsed = parse_body('{"message":"第一行\n第二行"}')
        assert parsed.payload["message"] == "第一行\n第二行"
        assert parsed.repairs == (jsonbody.REPAIR_CONTROL_CHARS,)

    def test_raw_tab_is_rescued(self) -> None:
        parsed = parse_body('{"message":"标题\t正文"}')
        assert parsed.payload["message"] == "标题\t正文"
        assert parsed.repairs == (jsonbody.REPAIR_CONTROL_CHARS,)


class TestBareValues:
    """漏掉的那一对引号 —— 也就是这次真出过事的那个形状。"""

    def test_bare_value_at_the_end(self) -> None:
        parsed = parse_body('{"episode":"3","message":番剧更新啦}')
        assert parsed.payload == {"episode": "3", "message": "番剧更新啦"}
        assert parsed.repairs == (jsonbody.REPAIR_BARE_VALUE,)

    def test_bare_value_in_the_middle(self) -> None:
        parsed = parse_body('{"message":番剧更新啦,"score":"8.1"}')
        assert parsed.payload == {"message": "番剧更新啦", "score": "8.1"}
        assert parsed.repairs == (jsonbody.REPAIR_BARE_VALUE,)

    def test_two_bare_values_in_one_body(self) -> None:
        """「顺手把 ${subgroup} 的引号也漏了」这种连环错也要救。"""

        parsed = parse_body('{"subgroup":桜都字幕组,"message":第 3 集来了}')
        assert parsed.payload == {"subgroup": "桜都字幕组", "message": "第 3 集来了"}

    def test_empty_bare_value(self) -> None:
        """「"message":」 后面空着 —— 补成空串也比整条丢掉好。"""

        parsed = parse_body('{"title":"药屋","message":}')
        assert parsed.payload == {"title": "药屋", "message": ""}

    def test_trailing_whitespace_stays_outside_quotes(self) -> None:
        parsed = parse_body('{"message":番剧更新啦   }')
        assert parsed.payload["message"] == "番剧更新啦"

    def test_escapes_are_not_doubled(self) -> None:
        """推送端已经转义过一遍，容错只补外层引号，绝不能再转一次。"""

        parsed = parse_body(r'{"message":新集更新\n药屋 第 3 集 \"OP\"}')
        assert parsed.payload["message"] == '新集更新\n药屋 第 3 集 "OP"'
        assert parsed.repairs == (jsonbody.REPAIR_BARE_VALUE,)

    def test_ani_rss_broken_template(self) -> None:
        parsed = parse_body(_ANIRSS_BROKEN)
        assert parsed.payload["event"] == "下载完成"
        assert parsed.payload["title"] == "药屋少女的呢喃"
        assert parsed.payload["subgroup"] == "桜都字幕组"
        assert parsed.payload["message"] == "下载完成 药屋少女的呢喃 第 3 集"
        assert parsed.repairs == (jsonbody.REPAIR_BARE_VALUE,)

    def test_both_problems_are_reported(self) -> None:
        """两处毛病一次说清，省得用户改完一处又撞另一处。"""

        parsed = parse_body('{"title":"药屋\n少女","message":第 3 集}')
        assert parsed.payload["title"] == "药屋\n少女"
        assert parsed.repairs == (
            jsonbody.REPAIR_BARE_VALUE,
            jsonbody.REPAIR_CONTROL_CHARS,
        )


class TestRejected:
    """猜不动的一律照旧回 400：宁可拒绝，也不端出一份猜歪的 payload。"""

    @pytest.mark.parametrize(
        "text",
        [
            "not-json",
            "[1,2,",
            "<html><body>502 Bad Gateway</body></html>",
            '{"a":{"b":裸值}}',
            '{"a":[裸值]}',
            '{"a":"x",,}',
        ],
    )
    def test_unrepairable_bodies(self, text: str) -> None:
        with pytest.raises(json.JSONDecodeError):
            parse_body(text)

    def test_original_error_is_raised(self) -> None:
        """抛的是最初那个异常：只有原文里的位置才和用户模板里看到的对得上。"""

        with pytest.raises(json.JSONDecodeError) as excinfo:
            parse_body('{"message":没有右括号')
        assert excinfo.value.pos == 11

    def test_too_many_bare_values_is_rejected(self) -> None:
        """连环漏引号超过容错上限就别猜了 —— 那多半不是漏引号，是模板整个坏了。"""

        text = "{" + ",".join(f'"k{index}":裸值{index}' for index in range(8)) + "}"
        with pytest.raises(json.JSONDecodeError):
            parse_body(text)


class TestPreview:
    """日志里贴的那一段原文：压平、截断，长度可控。"""

    def test_short_text_is_verbatim(self) -> None:
        assert preview('{"a":1}') == '{"a":1}'

    def test_whitespace_is_flattened(self) -> None:
        assert preview('{"a":\n\t"b"}') == '{"a": "b"}'

    def test_long_text_is_truncated(self) -> None:
        result = preview("x" * 500)
        assert result.endswith("……")
        assert len(result) == jsonbody.PREVIEW_LIMIT + 2

    def test_limit_can_be_overridden(self) -> None:
        assert preview("abcdefghij", limit=4) == "abcd……"


class TestRepairMessages:
    """文案是用户唯一能看到的线索，必须直接点出该改哪里。"""

    def test_bare_value_message_names_the_placeholder(self) -> None:
        assert "${message}" in jsonbody.REPAIR_BARE_VALUE
        assert "引号" in jsonbody.REPAIR_BARE_VALUE

    def test_control_chars_message_mentions_newline(self) -> None:
        assert "换行" in jsonbody.REPAIR_CONTROL_CHARS

    def test_repair_passes_have_room_for_a_chain(self) -> None:
        """上限太小会把「漏了两三处」也判死，太大又等于纵容乱模板。"""

        assert jsonbody.MAX_REPAIR_PASSES >= 4
        assert jsonbody.PREVIEW_LIMIT >= 80
