"""人格转述的重试与兜底：卡片上的「播报」位不许开天窗。

LLM 网关会间歇抽风 —— 实测 new-api 返回 400 「invalid_grant」，三分钟后同一
条链路又完全正常。这种瞬时故障要是直接放弃，同一批通知里就会有的带播报、有的
不带，版式忽有忽无，用户只会以为插件坏了。所以策略是两级的：先退避重试一次，
仍失败就用一句确定性文案顶上。

这份文件锁住三件事：重试几次、什么时候不该重试、兜底文案长什么样。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from nexus.config import NexusConfig
from nexus.models import Notification
from nexus.services import notifier as notifier_module
from nexus.services.notifier import PERSONA_RETRY_DELAY, Notifier, _fallback_line

UMO = "aiocqhttp:GroupMessage:1078946249"

#: 真实事件形态：ani-rss 推来的「下载完成」，标题已经带上季度。
NOTICE = Notification(
    kind="download_complete",
    title="超超超超超喜欢你的100个女朋友 第三季",
    subtitle="第 09 集下好了",
    lines=("字幕组：Kirara Fantasia", "进度：第 3 季第 09 集 · 共 12 集"),
)

#: 兜底文案的预期形态，多处复用。
FALLBACK = "《超超超超超喜欢你的100个女朋友 第三季》第 09 集下好了。"

#: 网关抽风时的真实报错（new-api channel #20）。
GATEWAY_ERROR = "channel error (channel #20, status code: 400): invalid_grant"


class _Activity:
    """记下活动流条目，用来断言「改用兜底文案」那条 warn 有没有落下。"""

    def __init__(self) -> None:
        self.notes: list[str] = []

    def info(self, scope: str, text: str) -> None:
        self.notes.append(f"i:{scope}:{text}")

    def warn(self, scope: str, text: str) -> None:
        self.notes.append(f"w:{scope}:{text}")

    def error(self, scope: str, text: str) -> None:
        self.notes.append(f"e:{scope}:{text}")

    def matched(self, keyword: str) -> list[str]:
        return [note for note in self.notes if keyword in note]


class _Gateway:
    """按脚本逐次应答的假 LLM 入口。

    脚本里的 「None」 表示这一次抛异常（复现网关 400），空串表示模型返回了空
    回复 —— 两种都是「这次失败了」，都该触发重试。脚本用尽后重复最后一项。
    """

    def __init__(self, *script: str | None) -> None:
        self._script: tuple[str | None, ...] = script or ("",)
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        reply = self._script[min(len(self.calls), len(self._script)) - 1]
        if reply is None:
            raise RuntimeError(GATEWAY_ERROR)
        return SimpleNamespace(completion_text=reply)


class _Persona:
    """只提供默认人格的假 persona_manager，顺手数一下被问了几次。"""

    PROMPT = "你是爱丽丝，说话软软的。"

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def get_default_persona_v3(self, umo: str) -> SimpleNamespace:
        self.calls.append(umo)
        return SimpleNamespace(system_prompt=self.PROMPT)


def _notifier(
    gateway: _Gateway | None,
    *,
    fallback_line: bool = True,
    provider_id: str = "",
    persona: _Persona | None = None,
) -> tuple[Notifier, _Activity]:
    """搭一个只够跑人格链路的 Notifier。

    「gateway=None」 表示运行时压根没挂 LLM 入口 —— 这是 「llm_available」 为假的
    唯一形态，也是「不该重试」的那一档。
    """

    activity = _Activity()
    context = SimpleNamespace()
    if gateway is not None:
        context.llm_generate = gateway
    if persona is not None:
        context.persona_manager = persona
    deps = cast(
        Any,
        SimpleNamespace(
            conf=NexusConfig(
                persona_fallback_line=fallback_line,
                persona_provider_id=provider_id,
            ),
            activity=activity,
            context=context,
        ),
    )
    return Notifier(deps), activity


@pytest.fixture
def slept(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """拦下退避里的真睡眠：1.5 秒 × 每个用例会让整套测试慢一个数量级。"""

    recorded: list[float] = []

    async def _sleep(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(notifier_module.asyncio, "sleep", _sleep)
    return recorded


class Test人格转述重试:
    """瞬时故障值得再试一次，永久缺席不值得。"""

    async def test_空回复会重试并采用第二次的结果(self, slept: list[float]) -> None:
        gateway = _Gateway("", "第 9 集下好啦，快去看吧")
        notifier, _ = _notifier(gateway)

        spoken = await notifier._persona_line(NOTICE, UMO)

        assert spoken == "第 9 集下好啦，快去看吧"
        assert len(gateway.calls) == 2
        assert slept == [PERSONA_RETRY_DELAY]

    async def test_网关抛异常也算失败(self, slept: list[float]) -> None:
        """new-api 的 400 「invalid_grant」 走的是抛异常这条路，不是返回空串。"""

        gateway = _Gateway(None, "顶上来了")
        notifier, activity = _notifier(gateway)

        spoken = await notifier._persona_line(NOTICE, UMO)

        assert spoken == "顶上来了"
        assert len(gateway.calls) == 2
        assert activity.matched("invalid_grant")

    async def test_一次就成功时既不睡也不兜底(self, slept: list[float]) -> None:
        gateway = _Gateway("这集有点好看")
        notifier, activity = _notifier(gateway)

        spoken = await notifier._persona_line(NOTICE, UMO)

        assert spoken == "这集有点好看"
        assert len(gateway.calls) == 1
        assert slept == []
        assert activity.matched("兜底") == []

    async def test_重试用的是同一份提示词(self, slept: list[float]) -> None:
        """重试只该重发同一份请求；顺手重建提示词会多问人格一次，白花一次往返。"""

        gateway = _Gateway("", "好了")
        notifier, _ = _notifier(gateway, persona=_Persona())

        await notifier._persona_line(NOTICE, UMO)

        first, second = gateway.calls
        assert first["prompt"] == second["prompt"]
        assert first["system_prompt"] == second["system_prompt"] == _Persona.PROMPT
        assert NOTICE.title in first["prompt"]

    async def test_人格提示词只取一次(self, slept: list[float]) -> None:
        """两次尝试共用一份人格提示词，别把 persona_manager 问两遗。"""

        persona = _Persona()
        gateway = _Gateway("", "好了")
        notifier, _ = _notifier(gateway, persona=persona)

        await notifier._persona_line(NOTICE, UMO)

        assert persona.calls == [UMO]

    async def test_换行被压成空格(self, slept: list[float]) -> None:
        """卡片的「播报」位是单段文本，模型冒出多行会把版式撑歪。"""

        gateway = _Gateway("第一行\n第二行")
        notifier, _ = _notifier(gateway)

        assert await notifier._persona_line(NOTICE, UMO) == "第一行 第二行"

    async def test_指定提供商时按配置走(self, slept: list[float]) -> None:
        gateway = _Gateway("好")
        notifier, _ = _notifier(gateway, provider_id="gemini-flash")

        await notifier._persona_line(NOTICE, UMO)

        assert gateway.calls[0]["chat_provider_id"] == "gemini-flash"


class Test不该重试的情形:
    """重试要花掉一次退避，只在真的可能自愈时才值得。"""

    async def test_没挂模型就只试一次(self, slept: list[float]) -> None:
        """没配提供商的用户每条通知都白等 1.5 秒，纯亏。"""

        notifier, _ = _notifier(None)

        assert await notifier._persona_line(NOTICE, UMO) == FALLBACK
        assert slept == []

    async def test_事实为空时直接给兜底(self, slept: list[float]) -> None:
        """空事实喂给模型只会得到一段幻觉，不如不问。"""

        gateway = _Gateway("不该被调用")
        notifier, _ = _notifier(gateway)

        assert await notifier.speak("   ", UMO, fallback="占个位") == "占个位"
        assert gateway.calls == []


class Test兜底文案:
    """卡片版式里「播报」是固定的一段，空着比文案生硬更糟。"""

    async def test_两次都失败就顶上兜底(self, slept: list[float]) -> None:
        gateway = _Gateway("", "")
        notifier, activity = _notifier(gateway)

        spoken = await notifier._persona_line(NOTICE, UMO)

        assert spoken == FALLBACK
        assert len(gateway.calls) == 2
        assert slept == [PERSONA_RETRY_DELAY]
        assert activity.matched("兜底文案")

    async def test_关掉开关就宁可空着(self, slept: list[float]) -> None:
        """有人就是不想让机器人说半句假人格的话，那就一个字都不填。"""

        gateway = _Gateway("", "")
        notifier, activity = _notifier(gateway, fallback_line=False)

        assert await notifier._persona_line(NOTICE, UMO) == ""
        assert activity.matched("兜底") == []

    async def test_指令回复不走兜底(self, slept: list[float]) -> None:
        """指令回复里没有「播报」这一格，硬塞一句反而像答非所问。"""

        gateway = _Gateway("", "")
        notifier, _ = _notifier(gateway)

        assert await notifier.speak("随便一段事实", UMO) == ""

    def test_有副标题就用副标题(self) -> None:
        assert _fallback_line(NOTICE) == FALLBACK

    def test_没副标题给一句中性的(self) -> None:
        notice = Notification(kind="rss_update", title="药屋少女的呢喃")
        assert _fallback_line(notice) == "《药屋少女的呢喃》有新动态。"

    def test_连标题都没有也不留空(self) -> None:
        """标题空着的通知现实里出现过（上游模板没填），卡片不能因此缺一段。"""

        assert _fallback_line(Notification(kind="test", title="")) == "《番剧》有新动态。"
