"""SQLite 持久层往返单测。

追番进度、订阅、去重历史全靠这一层，
用「tmp_path」跑真实 SQLite 文件，比 mock 更能抓出建表 / 迁移问题。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nexus.models import Subscription, WatchItem
from nexus.store import Store

UMO = "aiocqhttp:GroupMessage:12345"
OTHER = "aiocqhttp:GroupMessage:67890"


@pytest.fixture
async def store(tmp_path: Path):
    """每个用例独享一个库文件，避免相互污染。"""

    st = Store(tmp_path / "nexus.db")
    await st.initialize()
    try:
        yield st
    finally:
        await st.close()


def make_watch(**kw) -> WatchItem:
    base = {
        "id": 0,
        "umo": UMO,
        "subject_id": 400602,
        "title": "葬送的芙莉莲",
        "progress": 3,
        "total": 28,
    }
    base.update(kw)
    return WatchItem(**base)


def make_sub(**kw) -> Subscription:
    base = {
        "id": 0,
        "umo": UMO,
        "name": "葬送的芙莉莲",
        "url": "https://mikanani.me/RSS/Bangumi?bangumiId=3141",
    }
    base.update(kw)
    return Subscription(**base)


@pytest.mark.asyncio
class TestWatchlist:
    async def test_upsert_assigns_id(self, store: Store) -> None:
        item = await store.upsert_watch(make_watch())
        assert item.id > 0

    async def test_upsert_is_idempotent_per_session(self, store: Store) -> None:
        """同会话同番重复添加应更新而不是产生第二条。"""

        first = await store.upsert_watch(make_watch())
        second = await store.upsert_watch(make_watch(progress=5))
        assert second.id == first.id
        assert len(await store.list_watch(UMO)) == 1

    async def test_find_and_update(self, store: Store) -> None:
        item = await store.upsert_watch(make_watch())
        assert await store.update_watch(item.id, progress=9) is True
        found = await store.find_watch(UMO, "葬送的芙莉莲")
        assert found is not None
        assert found.progress == 9

    async def test_sessions_are_isolated(self, store: Store) -> None:
        await store.upsert_watch(make_watch())
        await store.upsert_watch(make_watch(umo=OTHER, title="迷宫饭"))
        assert len(await store.list_watch(UMO)) == 1
        assert len(await store.list_watch()) == 2

    async def test_filter_by_status(self, store: Store) -> None:
        await store.upsert_watch(make_watch())
        await store.upsert_watch(make_watch(title="迷宫饭", status="finished"))
        assert len(await store.list_watch(UMO, status="finished")) == 1

    async def test_delete(self, store: Store) -> None:
        item = await store.upsert_watch(make_watch())
        assert await store.delete_watch(item.id) is True
        assert await store.delete_watch(item.id) is False
        assert await store.list_watch(UMO) == []


@pytest.mark.asyncio
class TestSubscriptions:
    async def test_add_and_list(self, store: Store) -> None:
        sub = await store.add_subscription(make_sub())
        assert sub.id > 0
        assert len(await store.list_subscriptions(UMO)) == 1

    async def test_enabled_only(self, store: Store) -> None:
        sub = await store.add_subscription(make_sub())
        await store.set_subscription_state(sub.id, enabled=False)
        assert await store.list_subscriptions(UMO, enabled_only=True) == []

    async def test_bulk_toggle(self, store: Store) -> None:
        await store.add_subscription(make_sub())
        await store.add_subscription(make_sub(name="迷宫饭", url="mikan:1"))
        assert await store.set_subscriptions_enabled(UMO, False) == 2
        assert await store.list_subscriptions(UMO, enabled_only=True) == []

    async def test_state_records_error(self, store: Store) -> None:
        sub = await store.add_subscription(make_sub())
        await store.set_subscription_state(sub.id, error="超时", last_checked=1.0)
        found = await store.find_subscription(UMO, "葬送的芙莉莲")
        assert found is not None
        assert found.error == "超时"

    async def test_delete_all_for_session(self, store: Store) -> None:
        await store.add_subscription(make_sub())
        await store.add_subscription(make_sub(name="迷宫饭", url="mikan:1"))
        assert await store.delete_subscriptions(UMO) == 2


@pytest.mark.asyncio
class TestHistory:
    async def test_dedupe_roundtrip(self, store: Store) -> None:
        assert await store.seen("uid-1", UMO) is False
        assert await store.mark_seen(["uid-1", "uid-2"], UMO) == 2
        assert await store.seen("uid-1", UMO) is True

    async def test_filter_unseen_preserves_order(self, store: Store) -> None:
        await store.mark_seen(["b"], UMO)
        assert await store.filter_unseen(["a", "b", "c"], UMO) == ["a", "c"]

    async def test_history_is_per_session(self, store: Store) -> None:
        await store.mark_seen(["uid-1"], UMO)
        assert await store.seen("uid-1", OTHER) is False


@pytest.mark.asyncio
class TestPrefsAndKv:
    async def test_pref_roundtrip(self, store: Store) -> None:
        assert await store.get_pref(UMO, "theme", "midnight") == "midnight"
        await store.set_pref(UMO, "theme", "sakura")
        assert await store.get_pref(UMO, "theme") == "sakura"

    async def test_sessions_with_pref(self, store: Store) -> None:
        await store.set_pref(UMO, "daily", "1")
        await store.set_pref(OTHER, "daily", "0")
        assert await store.sessions_with_pref("daily", "1") == [UMO]

    async def test_kv_json_roundtrip(self, store: Store) -> None:
        await store.kv_set("webui_state", {"theme": "aurora", "dense": True})
        assert await store.kv_get("webui_state") == {"theme": "aurora", "dense": True}

    async def test_kv_default(self, store: Store) -> None:
        assert await store.kv_get("missing", "fallback") == "fallback"


@pytest.mark.asyncio
class TestExportImport:
    async def test_roundtrip_into_fresh_store(self, store: Store, tmp_path: Path) -> None:
        await store.upsert_watch(make_watch())
        await store.add_subscription(make_sub())
        payload = await store.export_all(UMO)

        target = Store(tmp_path / "copy.db")
        await target.initialize()
        try:
            counts = await target.import_all(payload, umo=UMO)
            assert sum(counts.values()) >= 2
            assert len(await target.list_watch(UMO)) == 1
            assert len(await target.list_subscriptions(UMO)) == 1
        finally:
            await target.close()

    async def test_import_garbage_is_safe(self, store: Store) -> None:
        assert isinstance(await store.import_all({}, umo=UMO), dict)


@pytest.mark.asyncio
async def test_stats_shape(store: Store) -> None:
    stats = await store.stats()
    for key in ("watchlist", "subscriptions", "sessions", "history"):
        assert key in stats
