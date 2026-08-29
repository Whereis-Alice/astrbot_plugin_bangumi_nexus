"""WebUI 配置写入的类型收敛单测。

WebUI 表单永远送 string，落库前必须按「_conf_schema.json」的 type 收敛，
否则「card_width」会存成「"860"」，后续算版式时直接崩。
"""

from __future__ import annotations

import pytest

from nexus.web.api import CONF_SCHEMA, NexusWebError, coerce_config_value


class TestUnknownKey:
    def test_rejects_unknown_key(self) -> None:
        """未知键一律拒绝，避免前端拼错字段名后写进一堆垃圾配置。"""

        with pytest.raises(NexusWebError):
            coerce_config_value("not_a_real_key", "x")


class TestBool:
    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", "开", True])
    def test_truthy(self, value: object) -> None:
        assert coerce_config_value("push_enabled", value) is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", "关", "", False])
    def test_falsy(self, value: object) -> None:
        assert coerce_config_value("push_enabled", value) is False


class TestInt:
    def test_plain_int(self) -> None:
        assert coerce_config_value("card_width", "1200") == 1200

    def test_float_string_truncates(self) -> None:
        assert coerce_config_value("card_width", "1200.9") == 1200

    def test_real_int(self) -> None:
        assert coerce_config_value("card_width", 900) == 900

    def test_invalid_raises(self) -> None:
        with pytest.raises(NexusWebError):
            coerce_config_value("card_width", "很宽")


class TestList:
    def test_newline_split(self) -> None:
        assert coerce_config_value("push_targets", "a\nb") == ["a", "b"]

    def test_comma_split(self) -> None:
        assert coerce_config_value("push_targets", "a, b") == ["a", "b"]

    def test_drops_blank_entries(self) -> None:
        assert coerce_config_value("push_targets", "a,,  ,b") == ["a", "b"]

    def test_sequence_input_is_stringified(self) -> None:
        assert coerce_config_value("push_targets", [" a ", 12]) == ["a", "12"]

    def test_empty(self) -> None:
        assert coerce_config_value("push_targets", "") == []


class TestString:
    def test_passthrough(self) -> None:
        assert coerce_config_value("card_theme", " sakura ") == " sakura "

    def test_none_becomes_empty(self) -> None:
        assert coerce_config_value("card_theme", None) == ""


# AstrBot 配置面板只认这几种 type，写错会导致整块配置在 WebUI 里渲染不出来。
ALLOWED_TYPES = frozenset(
    {
        "string",
        "text",
        "int",
        "float",
        "bool",
        "object",
        "list",
        "template_list",
        "file",
    }
)


class TestSchemaIntegrity:
    """schema 自身的健康度：类型必须是 AstrBot 面板认得的枚举。"""

    def test_types_are_supported(self) -> None:
        for key, spec in CONF_SCHEMA.items():
            assert spec.get("type") in ALLOWED_TYPES, key

    def test_every_key_has_description(self) -> None:
        for key, spec in CONF_SCHEMA.items():
            assert spec.get("description"), key

    def test_every_key_is_coercible(self) -> None:
        """遍历全部键跑一次收敛，确保没有 schema 类型漏处理。"""

        for key, spec in CONF_SCHEMA.items():
            sample = {"int": "1", "float": "1.5", "bool": "1", "object": {}}.get(
                spec.get("type", "string"), ""
            )
            coerce_config_value(key, sample)
