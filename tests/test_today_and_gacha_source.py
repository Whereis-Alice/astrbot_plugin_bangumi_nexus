"""「/今日新番」 的口径统一，以及抽番池子的默认数据源。

为什么单独锁这一组：1.1.3 之前 「/今日新番」 是一条独立指令，走 「today(compact=True)」——
卡片尺寸一模一样，却不取封面、也不查长期连载。用户看到的是「全是首字占位块、
还缺了在播年番（《名探偵プリキュア！》就是这么消失的）」的残卡，而且完全无从得知
自己踩的是精简模式。1.1.4 把整条分支删掉，「今日新番」 降级成 「today」 的别名。

同一轮还把抽番池子从「长门番堂优先」改成「Bangumi 优先」：长门番堂需要放宽 TLS
才连得上，把它放在第一位等于让每次 「/抽番」 都先赌一次网络。

下面这几条把「只有一种今日放送口径」和「默认走 Bangumi」钉死，避免以后又被绕回去。
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from nexus import catalog
from nexus.config import NexusConfig
from nexus.models import CalendarDay, Subject
from nexus.render.template import build_today_card
from nexus.services.gacha import GachaService
from nexus.services.search import TODAY_LIMIT, SearchService

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
MAIN_SOURCE = (PLUGIN_ROOT / "main.py").read_text(encoding="utf-8")


class Test今日放送只有一种口径:
    """精简分支必须彻底消失，而不是留着不被调用。"""

    def test_today_不再接受_compact(self) -> None:
        """留着 「compact」 形参就等于留着复活的路，签名层面直接封掉。"""

        params = inspect.signature(SearchService.today).parameters
        assert "compact" not in params
        assert set(params) == {"self", "umo", "weekday"}

    def test_搜索服务源码里没有残留的_compact_分支(self) -> None:
        source = inspect.getsource(SearchService.today)
        assert "compact=True" not in source
        assert "if compact" not in source

    def test_今日新番是别名而不是独立指令(self) -> None:
        """独立指令会绕过 「today」 的完整实现，用户又会拿到残卡。"""

        names = {cmd.name for cmd in catalog.all_commands()}
        assert "今日新番" not in names
        registered = set(re.findall(r'filter\.command\(\s*"([^"]+)"', MAIN_SOURCE))
        assert "今日新番" not in registered

    def test_today_的别名同时覆盖两种叫法(self) -> None:
        entry = next(cmd for cmd in catalog.all_commands() if cmd.name == "today")
        assert set(entry.aliases) == {"今日放送", "今日新番"}


class Test主栏截断要说人话:
    """卡片上「共 N 部」和实际列出的条数不一致时，必须写清楚。"""

    @staticmethod
    def _day(count: int) -> CalendarDay:
        items = tuple(
            Subject(id=index, name=f"番 {index}", name_cn=f"番 {index}", score=9 - index * 0.01)
            for index in range(1, count + 1)
        )
        return CalendarDay(weekday=3, label="周三", items=items)

    def test_超出上限时副标题写明只展示了前几部(self) -> None:
        html = build_today_card("sakura", self._day(20), limit=12)
        assert "今天共 20 部，这里展示前 12 部" in html

    def test_没超上限时不加多余提示(self) -> None:
        """没截断还写「展示前 N 部」是自找的误解。"""

        html = build_today_card("sakura", self._day(5), limit=12)
        assert "这里展示前" not in html

    def test_服务层用的上限就是模板默认值(self) -> None:
        """两边各写一个数字，迟早会漂成「提示 12 实际 8」这种谎话。"""

        default = inspect.signature(build_today_card).parameters["limit"].default
        assert default == TODAY_LIMIT


class _FakeActivity:
    """只记下告警文本，方便断言降级路径真的被走到。"""

    def __init__(self) -> None:
        self.warnings: list[str] = []

    def warn(self, scope: str, text: str) -> None:
        self.warnings.append(f"{scope}:{text}")

    def info(self, scope: str, text: str) -> None:
        pass


class _FakeBangumi:
    def __init__(self, days: list[CalendarDay] | None, *, boom: bool = False) -> None:
        self._days = days or []
        self._boom = boom
        self.calls = 0

    async def calendar(self) -> list[CalendarDay]:
        self.calls += 1
        if self._boom:
            raise RuntimeError("网络炸了")
        return self._days


class _FakeYuc:
    def __init__(self, table: object = None, *, boom: bool = False) -> None:
        self._table = table
        self._boom = boom
        self.calls = 0

    async def season(self, code: str = "") -> object:
        self.calls += 1
        if self._boom:
            raise RuntimeError("TLS 握手失败")
        return self._table


class _FakeHub:
    def __init__(self, bangumi: _FakeBangumi, yuc: _FakeYuc) -> None:
        self.bangumi = bangumi
        self.yuc = yuc


class _FakeDeps:
    """抽番只用到 「conf」「hub」「activity」 三样，没必要拉起真依赖。"""

    def __init__(self, hub: _FakeHub, *, source: str = "auto") -> None:
        self.conf = NexusConfig(gacha_source=source)
        self.hub = hub
        self.activity = _FakeActivity()


def _calendar() -> list[CalendarDay]:
    """两天，且第二天重复了第一天的条目，用来验证去重。"""

    alpha = Subject(id=1, name="甲番", name_cn="甲番", score=8.0)
    beta = Subject(id=2, name="乙番", name_cn="乙番", score=7.0)
    return [
        CalendarDay(weekday=1, label="周一", items=(alpha, beta)),
        CalendarDay(weekday=2, label="周二", items=(alpha,)),
    ]


class Test抽番池子默认走Bangumi:
    """默认配置下不该先去赌一次长门番堂的连通性。"""

    def test_配置默认值仍是auto(self) -> None:
        assert NexusConfig().gacha_source == "auto"

    @pytest.mark.parametrize("source", ["auto", "bangumi"])
    async def test_auto_与_bangumi_都先问_bangumi(self, source: str) -> None:
        hub = _FakeHub(_FakeBangumi(_calendar()), _FakeYuc())
        service = GachaService(_FakeDeps(hub, source=source))
        entries, subjects, label = await service._pool()
        assert entries == []
        assert [item.id for item in subjects] == [1, 2]
        assert "Bangumi" in label
        assert hub.yuc.calls == 0, "Bangumi 拿到了还去碰长门番堂，等于白搭一次超时"

    async def test_bangumi_空手时_auto_回落长门番堂(self) -> None:
        table = type("T", (), {"total": 1, "entries": ()})()
        hub = _FakeHub(_FakeBangumi([], boom=True), _FakeYuc(table))
        service = GachaService(_FakeDeps(hub, source="auto"))
        _, subjects, label = await service._pool()
        assert subjects == []
        assert "长门番堂" in label
        assert hub.yuc.calls == 1

    async def test_显式选_bangumi_时不回落(self) -> None:
        """显式指定数据源就该独占，否则用户根本没法把长门番堂关掉。"""

        hub = _FakeHub(_FakeBangumi([]), _FakeYuc())
        service = GachaService(_FakeDeps(hub, source="bangumi"))
        entries, subjects, _ = await service._pool()
        assert (entries, subjects) == ([], [])
        assert hub.yuc.calls == 0

    async def test_显式选_yuc_时不碰_bangumi(self) -> None:
        table = type("T", (), {"total": 2, "entries": ()})()
        hub = _FakeHub(_FakeBangumi(_calendar()), _FakeYuc(table))
        service = GachaService(_FakeDeps(hub, source="yuc"))
        _, subjects, label = await service._pool()
        assert subjects == []
        assert "长门番堂" in label
        assert hub.bangumi.calls == 0

    async def test_两边都挂时只是空池子而不是抛异常(self) -> None:
        """「/抽番」 要能回一句人话，不能把栈打到用户脸上。"""

        hub = _FakeHub(_FakeBangumi([], boom=True), _FakeYuc(boom=True))
        deps = _FakeDeps(hub, source="auto")
        entries, subjects, _ = await GachaService(deps)._pool()
        assert (entries, subjects) == ([], [])
        assert len(deps.activity.warnings) == 2


class Test题材提示不留空:
    """Bangumi 池子没有 「genres」 字段，题材提示必须从标签里凑。"""

    async def test_bangumi_池子下题材提示改用条目标签(self) -> None:
        from nexus.services.gacha import _tags

        alpha = Subject(id=1, name="甲番", name_cn="甲番", tags=("奇幻", "日常"))
        beta = Subject(id=2, name="乙番", name_cn="乙番", tags=("奇幻",))
        assert _tags([alpha, beta])[0] == "奇幻"

    async def test_没匹配到题材时给的是可选项而不是空句子(self) -> None:
        hub = _FakeHub(
            _FakeBangumi(
                [
                    CalendarDay(
                        weekday=1,
                        label="周一",
                        items=(Subject(id=1, name="甲番", name_cn="甲番", tags=("奇幻",)),),
                    )
                ]
            ),
            _FakeYuc(),
        )
        reply = await GachaService(_FakeDeps(hub)).draw("umo:test", "根本没有这个题材")
        assert "可选题材：奇幻" in reply.text
