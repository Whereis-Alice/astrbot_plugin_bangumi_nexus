"""平台实例解析：把配置里写的适配器「类型名」纠正为运行时的「实例 id」。

AstrBot 的 「Context.send_message」 是按 「platform.meta().id」（实例唯一标识）匹配的，
而不是按 「meta().name」（适配器类型，如 「aiocqhttp」）。用户在配置里几乎一定会写类型名
—— 因为面板上显眼的就是它 —— 于是每一条推送都会撞上「没有匹配的平台适配器」。

这个模块只做纯函数式的名字换算，不碰 SDK 对象，方便在 pytest 里裸测；
唯一一个接触 「context」 的函数 「live_platforms」 把异常吞掉后退化成空元组，
因为「读不到适配器列表」不该让推送直接崩掉，后续逻辑会原样使用配置值。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

Instances = tuple[tuple[str, str], ...]
"""((实例 id, 适配器类型名), ...)。"""


def live_platforms(context: object) -> Instances:
    """从运行中的 AstrBot 取当前启用的平台实例。

    只取 「enable」 为真的实例：面板上停用的适配器仍然可能残留在列表里，
    真投递过去必然失败，拿来当兜底目标只会掩盖问题。
    """
    manager = getattr(context, "platform_manager", None)
    raw = getattr(manager, "platform_insts", None) if manager is not None else None
    if not raw:
        return ()
    found: list[tuple[str, str]] = []
    for platform in raw:
        try:
            meta = platform.meta()
        except Exception:  # noqa: BLE001 - 某个适配器的 meta 抛错不该带走整张表
            continue
        ident = str(getattr(meta, "id", "") or "").strip()
        kind = str(getattr(meta, "name", "") or "").strip()
        if not ident and not kind:
            continue
        found.append((ident or kind, kind or ident))
    return tuple(dict.fromkeys(found))


def pick_platform_id(instances: Sequence[tuple[str, str]], preferred: str = "") -> str:
    """在实例表里挑一个平台实例 id。

    三级回退，越靠前越尊重用户意图：
    1. 「preferred」 本来就是某个实例 id —— 直接用；
    2. 「preferred」 是适配器类型名（常见误填）—— 换成该类型第一个实例的 id；
    3. 什么都对不上 —— 用第一个启用中的实例，并由调用方打日志说明。

    实例表为空时返回 「preferred」 原值，让调用方保持旧行为而不是拼出空平台段。
    """
    want = (preferred or "").strip()
    if not instances:
        return want
    lowered = want.lower()
    if lowered:
        for ident, _kind in instances:
            if ident.lower() == lowered:
                return ident
        for ident, kind in instances:
            if kind.lower() == lowered:
                return ident
    return instances[0][0]


def is_live_platform(instances: Sequence[tuple[str, str]], platform: str) -> bool:
    """「platform」 是否正好是某个启用中的实例 id。"""
    lowered = (platform or "").strip().lower()
    if not lowered:
        return False
    return any(ident.lower() == lowered for ident, _kind in instances)


def remap_umo(
    umo: str,
    instances: Sequence[tuple[str, str]],
    preferred: str = "",
) -> str:
    """把会话标识里的平台段换成真实存在的实例 id。

    只在「平台段匹配不上任何启用实例」时才动手：DB 里存的历史 umo 可能是
    正确的实例 id，也可能是早期版本拼错的类型名，逐条修库不现实，
    投递前统一过一遍成本最低。三段式以外的写法原样返回，交给上游报错。
    """
    token = (umo or "").strip()
    if not token or not instances:
        return token
    platform, sep, rest = token.partition(":")
    if not sep or not rest:
        return token
    if is_live_platform(instances, platform):
        return token
    resolved = pick_platform_id(instances, platform or preferred)
    if not resolved or resolved == platform:
        return token
    return f"{resolved}:{rest}"


def describe(instances: Iterable[tuple[str, str]]) -> str:
    """给日志用的一行描述，形如 「default(aiocqhttp), 爱丽丝(astrbook)」。"""
    parts = [f"{ident}({kind})" if kind and kind != ident else ident for ident, kind in instances]
    return "、".join(parts) if parts else "（空）"


__all__ = [
    "Instances",
    "describe",
    "is_live_platform",
    "live_platforms",
    "pick_platform_id",
    "remap_umo",
]
