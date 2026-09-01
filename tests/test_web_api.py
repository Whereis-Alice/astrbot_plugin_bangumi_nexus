"""WebUI 配置写入的类型收敛单测。

WebUI 表单永远送 string，落库前必须按「_conf_schema.json」的 type 收敛，
否则「card_width」会存成「"860"」，后续算版式时直接崩。
"""

from __future__ import annotations

import pytest

from nexus.config import SECRET_KEYS as CONFIG_SECRET_KEYS
from nexus.config import NexusConfig
from nexus.web.api import (
    CONF_GROUPS,
    CONF_SCHEMA,
    SECRET_KEYS,
    NexusWebError,
    coerce_config_value,
)


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


class TestConfGroups:
    """管理页的配置分组必须覆盖全部键，漏一个用户就只能去 Dashboard 找。"""

    def test_每个键都归进了一个分组(self) -> None:
        grouped = [key for _, _, keys in CONF_GROUPS for key in keys]
        missing = [key for key in CONF_SCHEMA if key not in set(grouped)]
        assert missing == [], f"这些配置项在管理页里没有归组：{missing}"

    def test_分组里没有重复或幽灵键(self) -> None:
        grouped = [key for _, _, keys in CONF_GROUPS for key in keys]
        assert len(grouped) == len(set(grouped)), "同一个键被归进了两个分组"
        ghosts = [key for key in grouped if key not in CONF_SCHEMA]
        assert ghosts == [], f"分组里写了 schema 里没有的键：{ghosts}"

    def test_分组标题唯一(self) -> None:
        ids = [gid for gid, _, _ in CONF_GROUPS]
        names = [name for _, name, _ in CONF_GROUPS]
        assert len(ids) == len(set(ids))
        assert len(names) == len(set(names))


class TestSecretKeys:
    """脱敏名单只能有一份，否则 WebUI 会把「true」当成真密钥写回去。"""

    def test_web_层直接复用配置层的名单(self) -> None:
        assert SECRET_KEYS is CONFIG_SECRET_KEYS

    def test_名单里的键都真实存在(self) -> None:
        conf = NexusConfig()
        for key in SECRET_KEYS:
            assert hasattr(conf, key), key
            assert key in CONF_SCHEMA, key

    def test_payload_把敏感项脱成布尔(self) -> None:
        conf = NexusConfig(**dict.fromkeys(SECRET_KEYS, "s3cret"))
        data = conf.payload()
        for key in SECRET_KEYS:
            assert data[key] is True, key
        empty = NexusConfig().payload()
        for key in SECRET_KEYS:
            assert empty[key] is False, key
