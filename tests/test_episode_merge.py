"""跨轮次同集归并的回归锁。

为什么需要单独一套：真实场景里同一个字幕组对同一集从四个片源各压一版，
**发布日期是跨天的**（实测 CR 8/31、Baha 与 B-Global 9/1、ABEMA 再晚五天）。
只在单次轮询的批次内归并时，先到的那一版当轮推走，第二天后到的又是全新条目，
一集照样刷两三条 —— 归并对这类组基本失效。这里锁住的就是「跨轮次也只推一条」，
同时锁住两条不能被顺手优化掉的例外：修订版仍要推、窗口过期后要放行。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from nexus.models import FeedItem, Subscription
from nexus.services.subscriptions import SubscriptionService
from nexus.store import Store

# 截图里的真实标题：同一个组、同一集、四个片源，发布日期跨天。
CR = "[黒ネズミたち] GRAND BLUE 碧蓝之海 3 - 09 [CR][WEB-DL][1080p][AVC AAC][CHT][MKV]"
BAHA = "[黒ネズミたち] GRAND BLUE 碧蓝之海 3 - 09 [Baha][WEB-DL][1080p][AVC AAC][CHT][MP4]"
BGLOBAL = "[黒ネズミたち] GRAND BLUE 碧蓝之海 3 - 09 [B-Global][WEB-DL][1080p][HEVC AAC][CHT][MKV]"
NEXT_EP = "[黒ネズミたち] GRAND BLUE 碧蓝之海 3 - 10 [Baha][WEB-DL][1080p][AVC AAC][CHT][MP4]"
BAHA_V2 = "[黒ネズミたち] GRAND BLUE 碧蓝之海 3 - 09v2 [Baha][WEB-DL][1080p][AVC AAC][CHT][MP4]"
MOVIE = "[黒ネズミたち] GRAND BLUE 剧场版 [Baha][WEB-DL][1080p][AVC AAC][CHT][MP4]"


def _item(title: str) -> FeedItem:
    """只填 「_merge_across_polls」 真正读到的字段，其余留空。

    刻意不用真 RSS 解析：这条路径只看标题，掺进解析细节会让失败原因变糊。
    """

    return FeedItem(uid=title, title=title, link="https://example.com/" + title[:8])


def _sub() -> Subscription:
    return Subscription(id=7, umo="umo-a", name="碧蓝之海 3", url="https://example.com/rss")


def _service(store: Store, *, window: int = 48) -> SubscriptionService:
    """拼一个只够跑归并路径的服务实例。

    真 「Deps」 要 「HttpClient」/「SourceHub」，而这条路径只读 「conf」「store」「activity」。
    """

    activity = SimpleNamespace(
        info=lambda *a, **k: None, warn=lambda *a, **k: None, error=lambda *a, **k: None
    )
    deps = SimpleNamespace(
        conf=SimpleNamespace(rss_episode_dedup=True, rss_episode_dedup_window_hours=window),
        store=store,
        activity=activity,
    )
    return SubscriptionService(cast(Any, deps))


async def _age(store: Store, seconds: float) -> None:
    """把同集记账的时间戳往前挪，等效于「窗口已经过去这么久」。

    比 「sleep」 可靠也快得多，而且能模拟出「30 天前」这种没法真等的场景。
    """

    def _work() -> None:
        conn = store._connection()
        conn.execute("UPDATE episode_history SET at = at - ?", (float(seconds),))
        conn.commit()

    await store._run(_work)


@pytest.fixture
async def store(tmp_path: Path):
    """每个用例一份独立的库文件，避免用例之间通过历史互相污染。"""

    inst = Store(tmp_path / "nexus.db")
    await inst.initialize()
    yield inst
    await inst.close()


class Test跨轮次只推一条:
    async def test_第二轮的同集其它片源被跳过(self, store: Store) -> None:
        """CR 先到（8/31）推走，第二天 Baha 到（9/1）不该再推第二条。"""

        service = _service(store)
        first, skipped = await service._merge_across_polls(_sub(), [_item(CR)])
        assert [item.title for item in first] == [CR]
        assert skipped == 0

        second, skipped = await service._merge_across_polls(_sub(), [_item(BAHA)])
        assert second == []
        assert skipped == 1

    async def test_同轮多个残留版本一起跳过(self, store: Store) -> None:
        """批内归并万一漏了（作品键靠字符串启发），这一层也要能收住。"""

        service = _service(store)
        await service._merge_across_polls(_sub(), [_item(CR)])
        kept, skipped = await service._merge_across_polls(_sub(), [_item(BAHA), _item(BGLOBAL)])
        assert kept == []
        assert skipped == 2

    async def test_下一集照常放行(self, store: Store) -> None:
        """窗口是按「集」记账的，不能把整条订阅静默两天。"""

        service = _service(store)
        await service._merge_across_polls(_sub(), [_item(CR)])
        kept, skipped = await service._merge_across_polls(_sub(), [_item(NEXT_EP)])
        assert [item.title for item in kept] == [NEXT_EP]
        assert skipped == 0

    async def test_修订版仍然推(self, store: Store) -> None:
        """「09v2」 是字幕组在修错，压过初版才合理 —— 这条不能被归并吃掉。"""

        service = _service(store)
        await service._merge_across_polls(_sub(), [_item(BAHA)])
        kept, skipped = await service._merge_across_polls(_sub(), [_item(BAHA_V2)])
        assert [item.title for item in kept] == [BAHA_V2]
        assert skipped == 0

    async def test_修订版之后初版不再回头推(self, store: Store) -> None:
        """推过 v2 之后又抓到 v1（RSS 顺序抖动是常事），不该倒退再推一条。"""

        service = _service(store)
        await service._merge_across_polls(_sub(), [_item(BAHA_V2)])
        kept, skipped = await service._merge_across_polls(_sub(), [_item(CR)])
        assert kept == []
        assert skipped == 1

    async def test_认不出集数的原样放行(self, store: Store) -> None:
        """剧场版、合集、字幕组公告都没有集数，归并绝不能碰它们。"""

        service = _service(store)
        kept, skipped = await service._merge_across_polls(_sub(), [_item(MOVIE)])
        assert [item.title for item in kept] == [MOVIE]
        assert skipped == 0

    async def test_窗口关闭时不记账也不跳过(self, store: Store) -> None:
        """窗口填 0 是明确的「退回旧行为」，此时连库都不该写。"""

        service = _service(store, window=0)
        await service._merge_across_polls(_sub(), [_item(CR)])
        kept, skipped = await service._merge_across_polls(_sub(), [_item(BAHA)])
        assert [item.title for item in kept] == [BAHA]
        assert skipped == 0

    async def test_不同订阅互不干扰(self, store: Store) -> None:
        """两个群订同一部番时，A 群推过不能让 B 群收不到。"""

        service = _service(store)
        await service._merge_across_polls(_sub(), [_item(CR)])
        other = Subscription(id=9, umo="umo-b", name="碧蓝之海 3", url="https://example.com/rss")
        kept, skipped = await service._merge_across_polls(other, [_item(BAHA)])
        assert [item.title for item in kept] == [BAHA]
        assert skipped == 0


class Test窗口过期:
    async def test_过期后放行(self, store: Store) -> None:
        """窗口的意义是「刚推过」，过了就该当成新一轮 —— 否则重播、补种永远收不到。"""

        service = _service(store, window=1)
        await service._merge_across_polls(_sub(), [_item(CR)])
        await _age(store, 7200)
        kept, skipped = await service._merge_across_polls(_sub(), [_item(BAHA)])
        assert [item.title for item in kept] == [BAHA]
        assert skipped == 0


class Test记账清理:
    async def test_删订阅时同集记账一起清(self, store: Store) -> None:
        """「id」 是自增的，复用到同一个号时旧记录会让新订阅第一轮莫名少推几条。"""

        service = _service(store)
        await service._merge_across_polls(_sub(), [_item(CR)])
        await store.delete_subscription(7)
        kept, skipped = await service._merge_across_polls(_sub(), [_item(BAHA)])
        assert [item.title for item in kept] == [BAHA]
        assert skipped == 0

    async def test_prune_覆盖同集记账(self, store: Store) -> None:
        """两张表共用一条清理线，漏掉哪张都会无限膨胀。"""

        service = _service(store)
        await service._merge_across_polls(_sub(), [_item(CR)])
        await _age(store, 86400 * 30)
        assert await store.prune_history(14) >= 1
        kept, _ = await service._merge_across_polls(_sub(), [_item(BAHA)])
        assert [item.title for item in kept] == [BAHA]
