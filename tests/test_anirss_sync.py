"""ani-rss 同步服务的行为锁。

同步是「读远端、写本地」的操作，写错了会静默毁掉用户手工维护的追番表，
所以这里把三条不可退让的不变量钉死：**进度只往前**、**弃坑不被覆盖**、**永不删除**。
另外锁住「目标会话为空时拒绝执行」—— 追番表按会话分表，猜一个会话写进去
比什么都不做更糟。

Copyright (C) 2026 Whereis-Alice and AstrBot Plugin Authors.
Licensed under the GNU Affero General Public License v3.0 or later.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from nexus.models import WatchItem
from nexus.services.anirss import KV_LAST_SYNC, AniRssSyncService
from nexus.sources.anirss import AniEntry, AniRssSnapshot
from nexus.store import Store

UMO = "aiocqhttp:GroupMessage:1"


def _entry(**kwargs: Any) -> AniEntry:
    """一条典型的 ani-rss 订阅，测试里只改关心的字段。"""

    base: dict[str, Any] = {
        "ani_id": "a1",
        "title": "名侦探光之美少女",
        "url": "https://mikanani.me/RSS/Bangumi?bangumiId=3345",
        "bgm_url": "https://bgm.tv/subject/523174",
        "cover": "https://lain.bgm.tv/pic/cover/l/aa.jpg",
        "total": 50,
        "progress": 22,
        "week_label": "1",
        "subgroup": "桜都字幕组",
    }
    base.update(kwargs)
    return AniEntry(**base)


class FakeSource:
    """假 ani-rss 客户端：不碰网络，只回给定快照。"""

    def __init__(self, snapshot: AniRssSnapshot, *, configured: bool = True) -> None:
        self.snapshot = snapshot
        self._configured = configured
        self.refreshed = 0
        self.listed = 0

    @property
    def configured(self) -> bool:
        return self._configured

    def describe(self) -> dict[str, Any]:
        return {"base": "http://127.0.0.1:7789", "configured": self._configured, "auth": "api_key"}

    async def list_ani(self) -> AniRssSnapshot:
        self.listed += 1
        return self.snapshot

    async def refresh_all(self) -> bool:
        self.refreshed += 1
        return True


class FakeNotifier:
    """记录 dispatch 的假通知器，用来验证同步走的是独立链。"""

    def __init__(self) -> None:
        self.sent: list[tuple[Any, tuple[str, ...]]] = []

    def resolve_targets(self, raw: Any) -> tuple[str, ...]:
        return tuple(str(item) for item in (raw or ()) if str(item).strip())

    async def dispatch(self, notification: Any, targets: tuple[str, ...], **kwargs: Any) -> int:
        self.sent.append((notification, targets))
        return len(targets)


def _conf(**kwargs: Any) -> SimpleNamespace:
    options: dict[str, Any] = {
        "anirss_enabled": True,
        "anirss_base": "http://127.0.0.1:7789",
        "anirss_api_key": "k",
        "anirss_username": "",
        "anirss_password": "",
        "anirss_verify_tls": True,
        "anirss_sync_interval_minutes": 60,
        "anirss_sync_targets": (UMO,),
        "anirss_sync_watchlist": True,
        "anirss_sync_subscriptions": False,
        "anirss_notify_on_change": True,
        "card_width": 880,
        "card_theme": "midnight",
        "card_renderer": "text",
    }
    options.update(kwargs)
    return SimpleNamespace(**options)


def _service(
    store: Store,
    snapshot: AniRssSnapshot,
    *,
    notifier: Any = None,
    configured: bool = True,
    **conf_kwargs: Any,
) -> tuple[AniRssSyncService, FakeSource]:
    """拼一个只依赖 「conf / store / activity」 的服务实例。

    真 「Deps」 要 「HttpClient」 与 「SourceHub」，而同步路径一行都用不到，
    掺进来只会让失败原因变糊。
    """

    activity = SimpleNamespace(
        info=lambda *a, **k: None, warn=lambda *a, **k: None, error=lambda *a, **k: None
    )
    deps = SimpleNamespace(conf=_conf(**conf_kwargs), store=store, activity=activity, http=None)
    service = AniRssSyncService(cast(Any, deps), notifier=notifier)
    source = FakeSource(snapshot, configured=configured)
    service._source = lambda: cast(Any, source)  # type: ignore[method-assign]
    return service, source


@pytest.fixture
async def store(tmp_path: Path):
    """每个用例一份独立库，避免用例之间通过追番表互相污染。"""

    inst = Store(tmp_path / "nexus.db")
    await inst.initialize()
    yield inst
    await inst.close()


class Test首次同步:
    async def test_把订阅写进追番表(self, store: Store) -> None:
        service, _ = _service(store, AniRssSnapshot(entries=(_entry(),), total=1))
        result = await service.sync()
        assert result["ok"] is True
        assert result["added"] == ["名侦探光之美少女"]
        rows = await store.list_watch(UMO)
        assert len(rows) == 1
        assert rows[0].progress == 22
        assert rows[0].total == 50
        assert rows[0].subject_id == 523174
        assert rows[0].weekday == 7

    async def test_记下认领关系(self, store: Store) -> None:
        """存 「ani_id → 本地行」 的映射，否则用户改了标题就会同步出第二条。"""

        service, _ = _service(store, AniRssSnapshot(entries=(_entry(),)))
        await service.sync()
        links = await store.list_anirss_links(UMO)
        assert [row["ani_id"] for row in links] == ["a1"]
        assert links[0]["watch_id"] > 0

    async def test_改了标题也不会同步出第二条(self, store: Store) -> None:
        service, _ = _service(store, AniRssSnapshot(entries=(_entry(),)))
        await service.sync()
        rows = await store.list_watch(UMO)
        await store.update_watch(rows[0].id, title="キミとアイドルプリキュア")
        await service.sync()
        assert len(await store.list_watch(UMO)) == 1

    async def test_完结的直接标成看完(self, store: Store) -> None:
        service, _ = _service(store, AniRssSnapshot(entries=(_entry(completed=True),)))
        await service.sync()
        rows = await store.list_watch(UMO)
        assert rows[0].status == "finished"

    async def test_停用的_ova_不搬过来(self, store: Store) -> None:
        """ani-rss 里停用的 OVA 基本都是加错留下的残渣。"""

        snapshot = AniRssSnapshot(entries=(_entry(ani_id="x", ova=True, enabled=False),))
        service, _ = _service(store, snapshot)
        await service.sync()
        assert await store.list_watch(UMO) == []

    async def test_手动同步会先请远端重扫(self, store: Store) -> None:
        service, source = _service(store, AniRssSnapshot(entries=(_entry(),)))
        await service.sync(force=True)
        assert source.refreshed == 1

    async def test_定时同步不请求重扫(self, store: Store) -> None:
        """每小时让下载器全量重扫一遍 RSS 太重，定时那条链只读现成数据。"""

        service, source = _service(store, AniRssSnapshot(entries=(_entry(),)))
        await service.sync()
        assert source.refreshed == 0


class Test进度只往前:
    async def test_远端更新时推进进度(self, store: Store) -> None:
        service, source = _service(store, AniRssSnapshot(entries=(_entry(progress=5),)))
        await service.sync()
        source.snapshot = AniRssSnapshot(entries=(_entry(progress=9),))
        result = await service.sync()
        rows = await store.list_watch(UMO)
        assert rows[0].progress == 9
        assert result["updated"] and "名侦探光之美少女" in result["updated"][0]

    async def test_本地看得更快时不倒退(self, store: Store) -> None:
        """ani-rss 的进度是「下载到第几集」，用户跑在下载前面是常态。"""

        service, _ = _service(store, AniRssSnapshot(entries=(_entry(progress=5),)))
        await service.sync()
        rows = await store.list_watch(UMO)
        await store.update_watch(rows[0].id, progress=12)
        await service.sync()
        assert (await store.list_watch(UMO))[0].progress == 12

    async def test_没变化时不记成更新(self, store: Store) -> None:
        service, _ = _service(store, AniRssSnapshot(entries=(_entry(),)))
        await service.sync()
        result = await service.sync()
        assert result["added"] == []
        assert result["updated"] == []

    async def test_弃坑不会被同步拉回在追(self, store: Store) -> None:
        """用户明确弃坑过，下载器还在下不代表他想继续看。"""

        service, _ = _service(store, AniRssSnapshot(entries=(_entry(),)))
        await service.sync()
        rows = await store.list_watch(UMO)
        await store.update_watch(rows[0].id, status="dropped")
        await service.sync()
        assert (await store.list_watch(UMO))[0].status == "dropped"


class Test永不删除:
    async def test_远端消失只报失联(self, store: Store) -> None:
        service, source = _service(
            store, AniRssSnapshot(entries=(_entry(), _entry(ani_id="a2", title="蔚蓝档案")))
        )
        await service.sync()
        source.snapshot = AniRssSnapshot(entries=(_entry(),))
        result = await service.sync()
        assert result["orphans"] == ["蔚蓝档案"]
        assert len(await store.list_watch(UMO)) == 2


class Test拒绝执行的场景:
    async def test_没配好就不动库(self, store: Store) -> None:
        service, _ = _service(store, AniRssSnapshot(), configured=False)
        result = await service.sync()
        assert result["ok"] is False
        assert await store.list_watch(UMO) == []

    async def test_目标会话为空时拒绝(self, store: Store) -> None:
        """追番表按会话分表，猜一个会话写进去比什么都不做更糟。"""

        service, _ = _service(store, AniRssSnapshot(entries=(_entry(),)), anirss_sync_targets=())
        result = await service.sync()
        assert result["ok"] is False
        assert "anirss_sync_targets" in result["error"]
        assert await store.list_watch(UMO) == []

    async def test_指定会话能覆盖配置(self, store: Store) -> None:
        service, _ = _service(store, AniRssSnapshot(entries=(_entry(),)), anirss_sync_targets=())
        result = await service.sync(targets=("aiocqhttp:FriendMessage:9",))
        assert result["ok"] is True
        assert await store.list_watch("aiocqhttp:FriendMessage:9")

    async def test_两个开关都关时只读不写(self, store: Store) -> None:
        service, _ = _service(
            store,
            AniRssSnapshot(entries=(_entry(),)),
            anirss_sync_watchlist=False,
            anirss_sync_subscriptions=False,
        )
        result = await service.sync()
        assert result["ok"] is True
        assert await store.list_watch(UMO) == []
        assert await store.list_anirss_links(UMO) == []


class Test订阅同步:
    async def test_开了才建订阅(self, store: Store) -> None:
        service, _ = _service(
            store, AniRssSnapshot(entries=(_entry(),)), anirss_sync_subscriptions=True
        )
        result = await service.sync()
        assert result["subscribed"] == ["名侦探光之美少女"]
        subs = await store.list_subscriptions(UMO)
        assert [sub.url for sub in subs] == ["https://mikanani.me/RSS/Bangumi?bangumiId=3345"]

    async def test_默认不建订阅(self, store: Store) -> None:
        """ani-rss 已经在轮询同一条源，插件再订一遍等于同一集通知两遍。"""

        service, _ = _service(store, AniRssSnapshot(entries=(_entry(),)))
        await service.sync()
        assert await store.list_subscriptions(UMO) == []

    async def test_重复同步不会堆出多条订阅(self, store: Store) -> None:
        service, _ = _service(
            store, AniRssSnapshot(entries=(_entry(),)), anirss_sync_subscriptions=True
        )
        await service.sync()
        result = await service.sync()
        assert result["subscribed"] == []
        assert len(await store.list_subscriptions(UMO)) == 1


class Test单条失败不拖垮整轮:
    async def test_超出上限逐条记账(self, store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
        entries = (_entry(), _entry(ani_id="a2", title="蔚蓝档案"))
        service, _ = _service(store, AniRssSnapshot(entries=entries))
        real = store.upsert_watch
        calls = {"n": 0}

        async def flaky(item: WatchItem) -> WatchItem:
            calls["n"] += 1
            if calls["n"] == 1:
                raise ValueError("这个会话的追番表已经有 200 部，先清理一些再加吧")
            return await real(item)

        monkeypatch.setattr(store, "upsert_watch", flaky)
        result = await service.sync()
        assert result["ok"] is True
        assert len(result["failures"]) == 1
        assert result["added"] == ["蔚蓝档案"]


class Test同步播报:
    async def test_有变化才发且只发指定会话(self, store: Store) -> None:
        notifier = FakeNotifier()
        service, _ = _service(store, AniRssSnapshot(entries=(_entry(),)), notifier=notifier)
        await service.sync()
        assert len(notifier.sent) == 1
        notification, targets = notifier.sent[0]
        assert targets == (UMO,)
        assert notification.kind == "anirss_sync"

    async def test_没变化时不吵(self, store: Store) -> None:
        notifier = FakeNotifier()
        service, _ = _service(store, AniRssSnapshot(entries=(_entry(),)), notifier=notifier)
        await service.sync()
        await service.sync()
        assert len(notifier.sent) == 1

    async def test_关了开关就不发(self, store: Store) -> None:
        notifier = FakeNotifier()
        service, _ = _service(
            store,
            AniRssSnapshot(entries=(_entry(),)),
            notifier=notifier,
            anirss_notify_on_change=False,
        )
        await service.sync()
        assert notifier.sent == []


class Test状态与卡片:
    async def test_同步后记下时间戳(self, store: Store) -> None:
        """时间戳落库，重启后不会立刻又触发一次全量同步。"""

        service, _ = _service(store, AniRssSnapshot(entries=(_entry(),)))
        await service.sync()
        assert float(await store.kv_get(KV_LAST_SYNC, 0.0)) > 0

    async def test_状态里带条目清单(self, store: Store) -> None:
        service, _ = _service(store, AniRssSnapshot(entries=(_entry(),), total=1))
        payload = await service.status()
        assert payload["ok"] is True
        assert payload["items"][0]["title"] == "名侦探光之美少女"
        assert payload["total"] == 1

    async def test_没配好时状态给出可读原因(self, store: Store) -> None:
        service, _ = _service(store, AniRssSnapshot(), configured=False)
        payload = await service.status()
        assert payload["ok"] is False
        assert payload["error"]

    async def test_卡片有文本兜底(self, store: Store) -> None:
        service, _ = _service(store, AniRssSnapshot(entries=(_entry(),), total=1))
        reply = await service.card(UMO)
        assert "名侦探光之美少女" in reply.text
        assert reply.card is not None


RAW_ANI = {
    "id": "a1",
    "title": "名侦探光之美少女",
    "url": "https://mikanani.me/RSS/Bangumi?bangumiId=3345",
    "bgmUrl": "https://bgm.tv/subject/523174",
    "image": "https://lain.bgm.tv/pic/cover/l/aa.jpg",
    "totalEpisodeNumber": 50,
    "currentEpisodeNumber": 22,
    "weekLabel": "1",
    "subgroup": "桜都字幕组",
    "enable": True,
}

#: 用户在自己电脑上 「curl POST /api/listAni」 存下来的原样响应。
RAW_EXPORT: dict[str, Any] = {
    "code": 200,
    "message": "",
    "data": {"weekList": [{"weekLabel": "1", "items": [RAW_ANI]}], "total": 1},
}


class Test离线导入:
    """ani-rss 基本都在自己电脑上，公网服务器连不回去，所以必须留一条搬 JSON 的路。

    这一组要证明的核心是：**离线导入和在线同步落库结果完全一致**，
    区别只在「快照从哪来」—— 否则用户会怀疑这条路是个缩水版。
    """

    async def test_粘一份_json_文本就能落库(self, store: Store) -> None:
        service, source = _service(store, AniRssSnapshot())
        result = await service.import_snapshot(json.dumps(RAW_EXPORT, ensure_ascii=False))
        assert result["ok"] is True
        assert result["added"] == ["名侦探光之美少女"]
        rows = await store.list_watch(UMO)
        assert (rows[0].progress, rows[0].total, rows[0].subject_id) == (22, 50, 523174)
        # 全程不碰网络，这是这条路存在的唯一理由。
        assert source.listed == 0

    async def test_没配地址也能导入(self, store: Store) -> None:
        """在线同步会因「没配好」直接拒绝；离线导入不需要任何凭据，必须放行。"""

        service, _ = _service(store, AniRssSnapshot(), configured=False)
        result = await service.import_snapshot(RAW_EXPORT)
        assert result["ok"] is True
        assert await store.list_watch(UMO)

    async def test_已解析好的对象也认(self, store: Store) -> None:
        service, _ = _service(store, AniRssSnapshot())
        assert (await service.import_snapshot(RAW_EXPORT))["ok"] is True

    async def test_只给里层的_data_也认(self, store: Store) -> None:
        service, _ = _service(store, AniRssSnapshot())
        result = await service.import_snapshot(RAW_EXPORT["data"])
        assert result["ok"] is True
        assert result["added"] == ["名侦探光之美少女"]

    async def test_bytes_也认(self, store: Store) -> None:
        """有人会用 「-o ani.json」 存文件再整份读上来，编码路径得走通。"""

        service, _ = _service(store, AniRssSnapshot())
        payload = json.dumps(RAW_EXPORT, ensure_ascii=False).encode("utf-8")
        assert (await service.import_snapshot(payload))["ok"] is True

    async def test_和在线同步落库结果一致(self, store: Store, tmp_path: Path) -> None:
        online = Store(tmp_path / "online.db")
        await online.initialize()
        try:
            snapshot = AniRssSnapshot(entries=(_entry(),), total=1)
            live, _ = _service(online, snapshot)
            await live.sync()
            offline, _ = _service(store, AniRssSnapshot())
            await offline.import_snapshot(RAW_EXPORT)
            fields = ("title", "progress", "total", "subject_id", "weekday", "status")
            picked = [
                [getattr(row, name) for name in fields] for row in await store.list_watch(UMO)
            ]
            expected = [
                [getattr(row, name) for name in fields] for row in await online.list_watch(UMO)
            ]
            assert picked == expected
        finally:
            await online.close()

    async def test_重复导入同一份不会堆出第二条(self, store: Store) -> None:
        service, _ = _service(store, AniRssSnapshot())
        await service.import_snapshot(RAW_EXPORT)
        result = await service.import_snapshot(RAW_EXPORT)
        assert result["added"] == []
        assert len(await store.list_watch(UMO)) == 1

    async def test_导入过的还能被在线同步接着认领(self, store: Store) -> None:
        """认领关系记在 「anirss_links」 上，两条入口共用，所以不会互相打出重复行。"""

        offline, _ = _service(store, AniRssSnapshot())
        await offline.import_snapshot(RAW_EXPORT)
        live, _ = _service(store, AniRssSnapshot(entries=(_entry(progress=30),), total=1))
        result = await live.sync()
        assert result["updated"] == ["名侦探光之美少女 → 30/50"]
        assert len(await store.list_watch(UMO)) == 1


class Test离线导入的错法:
    """导入失败时最没用的提示是「没反应」，所以每种错法都得有自己的话。"""

    async def test_空内容(self, store: Store) -> None:
        service, _ = _service(store, AniRssSnapshot())
        result = await service.import_snapshot("   \n  ")
        assert result["ok"] is False
        assert "空" in result["error"]

    async def test_不是合法_json(self, store: Store) -> None:
        service, _ = _service(store, AniRssSnapshot())
        result = await service.import_snapshot("{看起来像但不是}")
        assert result["ok"] is False
        assert "JSON" in result["error"]
        assert await store.list_watch(UMO) == []

    async def test_存下来的是失败响应(self, store: Store) -> None:
        service, _ = _service(store, AniRssSnapshot())
        result = await service.import_snapshot(
            {"code": 401, "message": "api-key 不正确", "data": None}
        )
        assert result["ok"] is False
        assert "api-key 不正确" in result["error"]

    async def test_里面没有条目(self, store: Store) -> None:
        service, _ = _service(store, AniRssSnapshot())
        result = await service.import_snapshot({"code": 200, "data": {"weekList": [], "total": 0}})
        assert result["ok"] is False
        assert "listAni" in result["error"]

    async def test_目标会话为空时同样拒绝(self, store: Store) -> None:
        service, _ = _service(store, AniRssSnapshot(), anirss_sync_targets=())
        result = await service.import_snapshot(RAW_EXPORT)
        assert result["ok"] is False
        assert "anirss_sync_targets" in result["error"]

    async def test_可以当场指定会话(self, store: Store) -> None:
        service, _ = _service(store, AniRssSnapshot(), anirss_sync_targets=())
        result = await service.import_snapshot(RAW_EXPORT, targets=("aiocqhttp:FriendMessage:9",))
        assert result["ok"] is True
        assert await store.list_watch("aiocqhttp:FriendMessage:9")


class Test来路会被标出来:
    """账目卡和结果字典都要写清是同步还是导入，否则用户看不出这份数字从哪来。"""

    async def test_结果里带来路(self, store: Store) -> None:
        service, _ = _service(store, AniRssSnapshot(entries=(_entry(),), total=1))
        assert (await service.sync())["origin"] == "同步"

    async def test_导入的来路是离线导入(self, store: Store) -> None:
        service, _ = _service(store, AniRssSnapshot())
        assert (await service.import_snapshot(RAW_EXPORT))["origin"] == "离线导入"

    async def test_播报标题跟着来路变(self, store: Store) -> None:
        notifier = FakeNotifier()
        service, _ = _service(store, AniRssSnapshot(), notifier=notifier)
        await service.import_snapshot(RAW_EXPORT)
        notification, targets = notifier.sent[0]
        assert "离线导入" in notification.title
        assert targets == (UMO,)
        assert notification.kind == "anirss_sync"
