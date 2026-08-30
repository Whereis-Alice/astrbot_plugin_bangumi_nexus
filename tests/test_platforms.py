"""平台实例解析的回归锁。

这些用例锁的是一个真实事故：用户把 「default_platform_id」 填成适配器类型
「aiocqhttp」，而 AstrBot 的 「send_message」 按实例 id（「default」）匹配，
导致每日播报 100% 失败且只报「没有匹配的平台适配器」。
"""

from __future__ import annotations

from nexus.platforms import (
    describe,
    is_live_platform,
    live_platforms,
    pick_platform_id,
    remap_umo,
)

INSTANCES = (("default", "aiocqhttp"), ("alice", "astrbook"))


class _Meta:
    def __init__(self, ident: str, name: str) -> None:
        self.id = ident
        self.name = name


class _Platform:
    def __init__(self, ident: str, name: str) -> None:
        self._meta = _Meta(ident, name)

    def meta(self) -> _Meta:
        return self._meta


class _Boom:
    def meta(self) -> _Meta:
        raise RuntimeError("meta 炸了")


class _Manager:
    def __init__(self, insts: list[object]) -> None:
        self.platform_insts = insts


class _Context:
    def __init__(self, insts: list[object]) -> None:
        self.platform_manager = _Manager(insts)


def test_实例_id_优先于类型名() -> None:
    """填对实例 id 时必须原样返回，不能被类型名匹配抢走。"""
    both = (("aiocqhttp", "aiocqhttp"), ("default", "aiocqhttp"))
    assert pick_platform_id(both, "default") == "default"


def test_类型名换算成实例_id() -> None:
    """这是事故本身：填类型名要能换算到实例 id。"""
    assert pick_platform_id(INSTANCES, "aiocqhttp") == "default"
    assert pick_platform_id(INSTANCES, "AIOCQHTTP") == "default"


def test_对不上就用第一个启用实例() -> None:
    """宁可发到唯一在线的适配器，也不要静默失败。"""
    assert pick_platform_id(INSTANCES, "telegram") == "default"
    assert pick_platform_id(INSTANCES, "") == "default"


def test_实例表为空时保持原值() -> None:
    """启动早期适配器还没注册，此时不该把平台段抹成空。"""
    assert pick_platform_id((), "aiocqhttp") == "aiocqhttp"
    assert remap_umo("aiocqhttp:GroupMessage:1", (), "") == "aiocqhttp:GroupMessage:1"


def test_umo_平台段纠正() -> None:
    """库里存着的历史错误写法要在投递前纠正。"""
    assert (
        remap_umo("aiocqhttp:GroupMessage:1091576468", INSTANCES)
        == "default:GroupMessage:1091576468"
    )


def test_umo_已正确则不动() -> None:
    """正确的 umo 一个字符都不许改，否则会发错群。"""
    umo = "default:GroupMessage:1091576468"
    assert remap_umo(umo, INSTANCES) == umo
    assert remap_umo("alice:FriendMessage:42", INSTANCES) == "alice:FriendMessage:42"


def test_非三段式原样返回() -> None:
    """畸形标识交给上游报错，这里不做猜测式改写。"""
    assert remap_umo("1091576468", INSTANCES) == "1091576468"
    assert remap_umo("", INSTANCES) == ""


def test_is_live_platform() -> None:
    assert is_live_platform(INSTANCES, "DEFAULT") is True
    assert is_live_platform(INSTANCES, "aiocqhttp") is False
    assert is_live_platform(INSTANCES, "") is False


def test_live_platforms_跳过_meta_异常的适配器() -> None:
    """单个适配器 meta 抛错不能带走整张实例表。"""
    ctx = _Context([_Platform("default", "aiocqhttp"), _Boom(), _Platform("alice", "astrbook")])
    assert live_platforms(ctx) == INSTANCES


def test_live_platforms_容忍缺少_platform_manager() -> None:
    assert live_platforms(object()) == ()


def test_describe_可读() -> None:
    assert describe(INSTANCES) == "default(aiocqhttp)、alice(astrbook)"
    assert describe(()) == "（空）"
