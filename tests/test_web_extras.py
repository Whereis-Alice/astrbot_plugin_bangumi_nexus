"""WebUI 新增接口的行为锁定单测（选源 / 全局排除项）。

这三个接口是「聊天侧的选源与排除」在面板上的镜像，为什么要单独锁：

* 面板与指令必须看到**同一份**候选清单，否则用户在面板点的字幕组
  和聊天里回的序号会指向不同源；
* 「排除项」写库存的是**预设名**而不是展开词，回显要能重新勾上复选框；
* 「回写到已有订阅」是批量覆盖，默认必须关，只有显式 「apply」 才动老订阅；
* 全局层（插件配置里设的）必须一起回给前端 —— 面板改不了它，但过滤结果是
  两层叠加的，不显示出来用户会把「我没勾却被过滤了」当成 bug。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from nexus.constants import EPISODE_PREFER_DEFAULT
from nexus.services.base import PREF_EXCLUDES
from nexus.services.picker import PickOption
from nexus.web.api import NexusService, NexusWebError


class _FakeStore:
    """只实现被测路径用到的三个方法的假 store。

    不用真 「NexusStore」：这几个接口的行为跟 SQLite 无关，
    真库会把测试变成慢的集成测试，也掩盖掉「到底读写了哪个偏好键」。
    """

    def __init__(self) -> None:
        self.prefs: dict[tuple[str, str], str] = {}
        self.applied: list[tuple[str, tuple[str, ...]]] = []

    async def get_pref(self, umo: str, key: str) -> str:
        return self.prefs.get((umo, key), "")

    async def set_pref(self, umo: str, key: str, value: str) -> None:
        self.prefs[(umo, key)] = value

    async def apply_excludes(self, umo: str, words: Any) -> int:
        self.applied.append((umo, tuple(words)))
        return 2


class _FakeSubs:
    """假订阅服务，只暴露 「pick_options」。"""

    def __init__(self, options: tuple[PickOption, ...]) -> None:
        self.options = options
        self.calls: list[str] = []

    async def pick_options(self, name: str) -> tuple[PickOption, ...]:
        self.calls.append(name)
        return self.options


def _service(
    options: tuple[PickOption, ...] = (),
    *,
    global_excludes: tuple[str, ...] = (),
) -> tuple[NexusService, _FakeStore, _FakeSubs]:
    """拼一个只够跑这三个接口的 「NexusService」。

    「Deps」 / 「Wiring」 都是字段很多的 dataclass，这里用 「SimpleNamespace」
    顶上 —— 「NexusService」 只按名字取属性，不做类型校验，
    这样新增无关字段时本测试不会跟着塌。
    """
    store = _FakeStore()
    subs = _FakeSubs(options)
    conf = SimpleNamespace(
        global_excludes=global_excludes,
        rss_episode_dedup=True,
        rss_episode_prefer=EPISODE_PREFER_DEFAULT,
        rss_episode_dedup_window_hours=48,
    )
    deps = SimpleNamespace(
        store=store,
        conf=conf,
        activity=SimpleNamespace(info=lambda *a, **k: None),
    )
    wiring = SimpleNamespace(subs=subs)
    return NexusService(cast(Any, deps), cast(Any, wiring)), store, subs


_OPTIONS = (
    PickOption(
        index=1,
        label="Mikan 单番",
        url="https://mikanani.me/RSS/Bangumi?bangumiId=3883",
        detail="整部番所有字幕组",
        group_id=0,
        tags=("全部",),
    ),
    PickOption(
        index=2,
        label="雪飘工作室",
        url="https://mikanani.me/RSS/Bangumi?bangumiId=3883&subgroupid=6",
        detail="最后更新 2026/08/29",
        group_id=6,
        tags=("字幕组",),
    ),
)


class TestSubSources:
    async def test_列出候选并保留序号与分组信息(self) -> None:
        """面板要靠 「index」 对齐聊天侧序号，靠 「url」 直接下单，两者都不能丢。"""

        service, _store, subs = _service(_OPTIONS)
        payload = await service.sub_sources(" 名侦探光之美少女 ")

        assert subs.calls == ["名侦探光之美少女"]
        assert payload["name"] == "名侦探光之美少女"
        assert payload["total"] == 2
        assert [item["index"] for item in payload["items"]] == [1, 2]
        assert payload["items"][1]["group_id"] == 6
        assert payload["items"][1]["url"].endswith("subgroupid=6")

    async def test_番名为空直接报错而不去打Mikan(self) -> None:
        """空关键词打过去只会拿到一堆无关搜索结果，属于必须拦在前端之前的输入错误。"""

        service, _store, subs = _service(_OPTIONS)
        with pytest.raises(NexusWebError):
            await service.sub_sources("   ")
        assert subs.calls == []


class TestExcludes:
    async def test_预设清单始终返回且未选会话时不读偏好(self) -> None:
        """面板一进来还没选会话，也得能把复选框先渲染出来。"""

        service, _store, _subs = _service()
        payload = await service.excludes("")

        assert payload["chosen"] == []
        assert payload["expanded"] == []
        assert payload["global"] == []
        names = [preset["name"] for preset in payload["presets"]]
        assert "繁体" in names
        assert all(preset["words"] for preset in payload["presets"])

    async def test_回显勾选原始名并附带展开结果(self) -> None:
        """存原始名是为了能重新勾上复选框，展开结果只用于让用户看清实际过滤词。"""

        service, store, _subs = _service()
        store.prefs[("umo-a", PREF_EXCLUDES)] = "繁体|我方自定义"
        payload = await service.excludes("umo-a")

        assert payload["chosen"] == ["繁体", "我方自定义"]
        assert "CHT" in payload["expanded"]
        assert "我方自定义" in payload["expanded"]


class TestSaveExcludes:
    async def test_默认不回写已有订阅(self) -> None:
        """改全局清单不等于要覆盖老订阅的过滤词，批量覆盖必须是显式一次点击。"""

        service, store, _subs = _service()
        payload = await service.save_excludes({"umo": "umo-a", "values": ["繁体", "  ", "繁体"]})

        assert payload["ok"] is True
        assert payload["chosen"] == ["繁体"]
        assert payload["applied"] == 0
        assert store.applied == []
        assert store.prefs[("umo-a", PREF_EXCLUDES)] == "繁体"

    async def test_显式apply时用展开后的词回写(self) -> None:
        """回写进订阅的必须是展开词，存预设名等于没过滤。"""

        service, store, _subs = _service()
        payload = await service.save_excludes(
            {"umo": "umo-a", "values": ["720p"], "apply": True},
        )

        assert payload["applied"] == 2
        assert store.applied and store.applied[0][0] == "umo-a"
        assert "1280x720" in store.applied[0][1]

    async def test_缺会话或值不是数组都要拒绝(self) -> None:
        """字符串会被逐字符拆成排除项，是最容易踩的一种前端 bug。"""

        service, _store, _subs = _service()
        with pytest.raises(NexusWebError):
            await service.save_excludes({"values": []})
        with pytest.raises(NexusWebError):
            await service.save_excludes({"umo": "umo-a", "values": "繁体"})


class Test全局层与同集归并回显:
    """面板要能说清「这条为什么被过滤」和「同一集为什么只来了一条」。"""

    async def test_全局层原样回显且并进展开结果(self) -> None:
        service, store, _subs = _service(global_excludes=("合集",))
        store.prefs[("umo-a", PREF_EXCLUDES)] = "繁体"
        payload = await service.excludes("umo-a")

        assert payload["global"] == ["合集"]
        assert payload["chosen"] == ["繁体"]
        # 展开结果是两层的并集：只展开会话层，用户就看不出全局层在起作用。
        assert "CHT" in payload["expanded"]
        assert "BATCH" in [word.upper() for word in payload["expanded"]]

    async def test_带上同集归并的当前设置(self) -> None:
        """省一次单独的配置请求，也让「开没开」在排除项面板上一眼可见。"""

        service, _store, _subs = _service()
        payload = await service.excludes("")

        assert payload["episode_dedup"] is True
        assert payload["episode_prefer"] == list(EPISODE_PREFER_DEFAULT)

    async def test_回写不带全局层(self) -> None:
        """全局层每轮轮询现取，写进订阅记录只会留下改配置也刷不掉的残留。"""

        service, store, _subs = _service(global_excludes=("合集",))
        await service.save_excludes({"umo": "umo-a", "values": ["720p"], "apply": True})

        applied = store.applied[0][1]
        assert "1280x720" in applied
        assert not [word for word in applied if "batch" in word.lower()]


class _FakeAnirss:
    """记录 「import_snapshot」 收到什么的假同步服务。"""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, tuple[str, ...]]] = []

    async def import_snapshot(self, raw: Any, *, targets: tuple[str, ...] = ()) -> dict[str, Any]:
        self.calls.append((raw, targets))
        return {"ok": True, "origin": "离线导入", "entries": 1}


def _anirss_service(anirss: Any) -> NexusService:
    """只够跑离线导入这一个接口的 「NexusService」。"""

    deps = SimpleNamespace(
        store=_FakeStore(),
        conf=SimpleNamespace(),
        activity=SimpleNamespace(info=lambda *a, **k: None),
    )
    return NexusService(cast(Any, deps), cast(Any, SimpleNamespace(anirss=anirss)))


class Test离线导入接口:
    """WebUI 粘一份 JSON 进来时，这一层只负责转交与清洗会话列表。"""

    async def test_原样把文本交给同步服务(self) -> None:
        """故意不在这里 「json.loads」：错法的分类和文案都归服务层，两处各判一次必然走偏。"""

        fake = _FakeAnirss()
        result = await _anirss_service(fake).anirss_import('{"code":200}')
        assert result["ok"] is True
        assert fake.calls == [('{"code":200}', ())]

    async def test_会话列表会去掉空白项(self) -> None:
        fake = _FakeAnirss()
        await _anirss_service(fake).anirss_import({"code": 200}, ["  a  ", "", "   ", "b"])
        assert fake.calls[0][1] == ("a", "b")

    async def test_没装配同步服务时报可读错误(self) -> None:
        with pytest.raises(NexusWebError):
            await _anirss_service(None).anirss_import("{}")
