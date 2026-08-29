"""SQLite 持久层。

为什么是 stdlib 「sqlite3」 而不是 ORM / apscheduler jobstore / rdflib：追番表和订阅
表的数据量顶多几千行，引入重依赖只会让安装更容易失败。这里用单连接 + 线程锁，
所有调用都过 「asyncio.to_thread」，因此不会阻塞事件循环。

表结构靠 「PRAGMA user_version」 做版本迁移，升级插件不会丢数据。

Copyright (C) 2026 Whereis-Alice and AstrBot Plugin Authors.
Licensed under the GNU Affero General Public License v3.0 or later.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, TypeVar

from .constants import MAX_SUBSCRIPTIONS_PER_SESSION, MAX_WATCHLIST_PER_SESSION
from .models import Subscription, WatchItem

T = TypeVar("T")

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS watchlist (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    umo         TEXT    NOT NULL,
    subject_id  INTEGER NOT NULL DEFAULT 0,
    title       TEXT    NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'watching',
    progress    INTEGER NOT NULL DEFAULT 0,
    total       INTEGER NOT NULL DEFAULT 0,
    score       REAL    NOT NULL DEFAULT 0,
    cover       TEXT    NOT NULL DEFAULT '',
    weekday     INTEGER NOT NULL DEFAULT 0,
    note        TEXT    NOT NULL DEFAULT '',
    updated_at  REAL    NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_watch_unique ON watchlist(umo, title);
CREATE INDEX IF NOT EXISTS idx_watch_umo ON watchlist(umo);

CREATE TABLE IF NOT EXISTS subscriptions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    umo           TEXT    NOT NULL,
    name          TEXT    NOT NULL,
    url           TEXT    NOT NULL,
    enabled       INTEGER NOT NULL DEFAULT 1,
    subject_id    INTEGER NOT NULL DEFAULT 0,
    keywords      TEXT    NOT NULL DEFAULT '',
    excludes      TEXT    NOT NULL DEFAULT '',
    last_checked  REAL    NOT NULL DEFAULT 0,
    last_item     TEXT    NOT NULL DEFAULT '',
    error         TEXT    NOT NULL DEFAULT '',
    created_at    REAL    NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sub_unique ON subscriptions(umo, name);
CREATE INDEX IF NOT EXISTS idx_sub_enabled ON subscriptions(enabled);

CREATE TABLE IF NOT EXISTS push_history (
    uid      TEXT    NOT NULL,
    umo      TEXT    NOT NULL DEFAULT '',
    at       REAL    NOT NULL DEFAULT 0,
    PRIMARY KEY (uid, umo)
);
CREATE INDEX IF NOT EXISTS idx_history_at ON push_history(at);

CREATE TABLE IF NOT EXISTS session_prefs (
    umo    TEXT NOT NULL,
    key    TEXT NOT NULL,
    value  TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (umo, key)
);

CREATE TABLE IF NOT EXISTS kv (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL DEFAULT '',
    at     REAL NOT NULL DEFAULT 0
);
"""


def _split(text: str) -> tuple[str, ...]:
    return tuple(part for part in (text or "").split("\u0000") if part)


def _join(values: Iterable[str]) -> str:
    return "\u0000".join(str(value).strip() for value in values if str(value).strip())


class Store:
    """所有持久化状态的唯一归口。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    # -- 连接与迁移 ---------------------------------------------------------

    def _connection(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, check_same_thread=False, timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        self._conn = conn
        return conn

    def _migrate(self) -> None:
        conn = self._connection()
        conn.executescript(_SCHEMA)
        current = int(conn.execute("PRAGMA user_version").fetchone()[0] or 0)
        # 迁移脚本按版本追加即可；空 dict 表示当前版本无需额外 DDL。
        migrations: dict[int, tuple[str, ...]] = {}
        for version in range(current + 1, SCHEMA_VERSION + 1):
            for statement in migrations.get(version, ()):
                conn.execute(statement)
        if current != SCHEMA_VERSION:
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.commit()

    async def initialize(self) -> None:
        await self._run(self._migrate)

    async def close(self) -> None:
        def _close() -> None:
            with self._lock:
                if self._conn is not None:
                    try:
                        self._conn.commit()
                        self._conn.close()
                    except Exception:  # noqa: BLE001 # pragma: no cover - 关库失败无所谓
                        pass
                    self._conn = None

        await self._run(_close)

    async def _run(self, func: Callable[[], T]) -> T:
        def _guarded() -> T:
            with self._lock:
                return func()

        return await asyncio.to_thread(_guarded)

    # -- 追番清单 -----------------------------------------------------------

    async def upsert_watch(self, item: WatchItem) -> WatchItem:
        def _work() -> WatchItem:
            conn = self._connection()
            now = time.time()
            row = conn.execute(
                "SELECT id FROM watchlist WHERE umo=? AND title=?", (item.umo, item.title)
            ).fetchone()
            if row is None:
                count = conn.execute(
                    "SELECT COUNT(*) FROM watchlist WHERE umo=?", (item.umo,)
                ).fetchone()[0]
                if count >= MAX_WATCHLIST_PER_SESSION:
                    raise ValueError(
                        f"这个会话的追番清单已经有 {count} 部，超出上限 "
                        f"{MAX_WATCHLIST_PER_SESSION} 部，先清理一些再加吧"
                    )
                cursor = conn.execute(
                    "INSERT INTO watchlist"
                    "(umo, subject_id, title, status, progress, total, score, cover,"
                    " weekday, note, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        item.umo,
                        item.subject_id,
                        item.title,
                        item.status,
                        item.progress,
                        item.total,
                        item.score,
                        item.cover,
                        item.weekday,
                        item.note,
                        now,
                    ),
                )
                new_id = int(cursor.lastrowid or 0)
            else:
                new_id = int(row["id"])
                conn.execute(
                    "UPDATE watchlist SET subject_id=?, status=?, progress=?, total=?,"
                    " score=?, cover=?, weekday=?, note=?, updated_at=? WHERE id=?",
                    (
                        item.subject_id,
                        item.status,
                        item.progress,
                        item.total,
                        item.score,
                        item.cover,
                        item.weekday,
                        item.note,
                        now,
                        new_id,
                    ),
                )
            conn.commit()
            item.id = new_id
            item.updated_at = now
            return item

        return await self._run(_work)

    async def list_watch(self, umo: str = "", *, status: str = "") -> list[WatchItem]:
        def _work() -> list[WatchItem]:
            conn = self._connection()
            clauses, params = [], []
            if umo:
                clauses.append("umo=?")
                params.append(umo)
            if status:
                clauses.append("status=?")
                params.append(status)
            where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
            rows = conn.execute(
                f"SELECT * FROM watchlist{where} ORDER BY status, weekday, updated_at DESC",
                params,
            ).fetchall()
            return [_watch_from_row(row) for row in rows]

        return await self._run(_work)

    async def find_watch(self, umo: str, title: str) -> WatchItem | None:
        def _work() -> WatchItem | None:
            conn = self._connection()
            row = conn.execute(
                "SELECT * FROM watchlist WHERE umo=? AND title=? LIMIT 1", (umo, title)
            ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT * FROM watchlist WHERE umo=? AND title LIKE ? LIMIT 1",
                    (umo, f"%{title}%"),
                ).fetchone()
            return _watch_from_row(row) if row else None

        return await self._run(_work)

    async def update_watch(self, watch_id: int, **fields: Any) -> bool:
        allowed = {
            "status",
            "progress",
            "total",
            "score",
            "cover",
            "weekday",
            "note",
            "subject_id",
            "title",
        }
        payload = {key: value for key, value in fields.items() if key in allowed}
        if not payload:
            return False

        def _work() -> bool:
            conn = self._connection()
            assignments = ", ".join(f"{key}=?" for key in payload)
            values = [*payload.values(), time.time(), watch_id]
            cursor = conn.execute(
                f"UPDATE watchlist SET {assignments}, updated_at=? WHERE id=?", values
            )
            conn.commit()
            return cursor.rowcount > 0

        return await self._run(_work)

    async def delete_watch(self, watch_id: int) -> bool:
        def _work() -> bool:
            conn = self._connection()
            cursor = conn.execute("DELETE FROM watchlist WHERE id=?", (watch_id,))
            conn.commit()
            return cursor.rowcount > 0

        return await self._run(_work)

    # -- RSS 订阅 -----------------------------------------------------------

    async def add_subscription(self, sub: Subscription) -> Subscription:
        def _work() -> Subscription:
            conn = self._connection()
            count = conn.execute(
                "SELECT COUNT(*) FROM subscriptions WHERE umo=?", (sub.umo,)
            ).fetchone()[0]
            existing = conn.execute(
                "SELECT id FROM subscriptions WHERE umo=? AND name=?", (sub.umo, sub.name)
            ).fetchone()
            if existing is None and count >= MAX_SUBSCRIPTIONS_PER_SESSION:
                raise ValueError(
                    f"这个会话已经有 {count} 条订阅，超出上限 {MAX_SUBSCRIPTIONS_PER_SESSION} 条"
                )
            now = time.time()
            if existing is None:
                cursor = conn.execute(
                    "INSERT INTO subscriptions"
                    "(umo, name, url, enabled, subject_id, keywords, excludes,"
                    " last_checked, last_item, error, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        sub.umo,
                        sub.name,
                        sub.url,
                        int(sub.enabled),
                        sub.subject_id,
                        _join(sub.keywords),
                        _join(sub.excludes),
                        sub.last_checked,
                        sub.last_item,
                        sub.error,
                        now,
                    ),
                )
                sub.id = int(cursor.lastrowid or 0)
            else:
                sub.id = int(existing["id"])
                conn.execute(
                    "UPDATE subscriptions SET url=?, enabled=?, subject_id=?, keywords=?,"
                    " excludes=?, error='' WHERE id=?",
                    (
                        sub.url,
                        int(sub.enabled),
                        sub.subject_id,
                        _join(sub.keywords),
                        _join(sub.excludes),
                        sub.id,
                    ),
                )
            conn.commit()
            sub.created_at = now
            return sub

        return await self._run(_work)

    async def list_subscriptions(
        self, umo: str = "", *, enabled_only: bool = False
    ) -> list[Subscription]:
        def _work() -> list[Subscription]:
            conn = self._connection()
            clauses, params = [], []
            if umo:
                clauses.append("umo=?")
                params.append(umo)
            if enabled_only:
                clauses.append("enabled=1")
            where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
            rows = conn.execute(
                f"SELECT * FROM subscriptions{where} ORDER BY umo, name", params
            ).fetchall()
            return [_sub_from_row(row) for row in rows]

        return await self._run(_work)

    async def find_subscription(self, umo: str, name: str) -> Subscription | None:
        def _work() -> Subscription | None:
            conn = self._connection()
            row = conn.execute(
                "SELECT * FROM subscriptions WHERE umo=? AND name=? LIMIT 1", (umo, name)
            ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT * FROM subscriptions WHERE umo=? AND (name LIKE ? OR url=?) LIMIT 1",
                    (umo, f"%{name}%", name),
                ).fetchone()
            return _sub_from_row(row) if row else None

        return await self._run(_work)

    async def set_subscription_state(
        self,
        sub_id: int,
        *,
        last_checked: float | None = None,
        last_item: str | None = None,
        error: str | None = None,
        enabled: bool | None = None,
    ) -> None:
        payload: dict[str, Any] = {}
        if last_checked is not None:
            payload["last_checked"] = last_checked
        if last_item is not None:
            payload["last_item"] = last_item
        if error is not None:
            payload["error"] = error[:300]
        if enabled is not None:
            payload["enabled"] = int(enabled)
        if not payload:
            return

        def _work() -> None:
            conn = self._connection()
            assignments = ", ".join(f"{key}=?" for key in payload)
            conn.execute(
                f"UPDATE subscriptions SET {assignments} WHERE id=?",
                [*payload.values(), sub_id],
            )
            conn.commit()

        await self._run(_work)

    async def set_subscriptions_enabled(self, umo: str, enabled: bool) -> int:
        def _work() -> int:
            conn = self._connection()
            cursor = conn.execute(
                "UPDATE subscriptions SET enabled=? WHERE umo=?", (int(enabled), umo)
            )
            conn.commit()
            return cursor.rowcount

        return await self._run(_work)

    async def delete_subscription(self, sub_id: int) -> bool:
        def _work() -> bool:
            conn = self._connection()
            cursor = conn.execute("DELETE FROM subscriptions WHERE id=?", (sub_id,))
            conn.commit()
            return cursor.rowcount > 0

        return await self._run(_work)

    async def delete_subscriptions(self, umo: str) -> int:
        def _work() -> int:
            conn = self._connection()
            cursor = conn.execute("DELETE FROM subscriptions WHERE umo=?", (umo,))
            conn.commit()
            return cursor.rowcount

        return await self._run(_work)

    # -- 推送去重 -----------------------------------------------------------

    async def seen(self, uid: str, umo: str = "") -> bool:
        def _work() -> bool:
            conn = self._connection()
            row = conn.execute(
                "SELECT 1 FROM push_history WHERE uid=? AND umo=? LIMIT 1", (uid, umo)
            ).fetchone()
            return row is not None

        return await self._run(_work)

    async def mark_seen(self, uids: Iterable[str], umo: str = "") -> int:
        rows = [(uid, umo, time.time()) for uid in dict.fromkeys(uids) if uid]
        if not rows:
            return 0

        def _work() -> int:
            conn = self._connection()
            conn.executemany(
                "INSERT OR REPLACE INTO push_history(uid, umo, at) VALUES (?,?,?)", rows
            )
            conn.commit()
            return len(rows)

        return await self._run(_work)

    async def filter_unseen(self, uids: Iterable[str], umo: str = "") -> list[str]:
        """一次查询筛出没推过的 uid —— 逐条查会把 IO 次数放大 N 倍。"""

        wanted = [uid for uid in dict.fromkeys(uids) if uid]
        if not wanted:
            return []

        def _work() -> list[str]:
            conn = self._connection()
            placeholders = ",".join("?" * len(wanted))
            rows = conn.execute(
                f"SELECT uid FROM push_history WHERE umo=? AND uid IN ({placeholders})",
                [umo, *wanted],
            ).fetchall()
            known = {row["uid"] for row in rows}
            return [uid for uid in wanted if uid not in known]

        return await self._run(_work)

    async def prune_history(self, days: int) -> int:
        cutoff = time.time() - max(1, days) * 86400

        def _work() -> int:
            conn = self._connection()
            cursor = conn.execute("DELETE FROM push_history WHERE at < ?", (cutoff,))
            conn.commit()
            return cursor.rowcount

        return await self._run(_work)

    # -- 会话偏好 -----------------------------------------------------------

    async def get_pref(self, umo: str, key: str, default: str = "") -> str:
        def _work() -> str:
            conn = self._connection()
            row = conn.execute(
                "SELECT value FROM session_prefs WHERE umo=? AND key=?", (umo, key)
            ).fetchone()
            return row["value"] if row else default

        return await self._run(_work)

    async def set_pref(self, umo: str, key: str, value: str) -> None:
        def _work() -> None:
            conn = self._connection()
            conn.execute(
                "INSERT OR REPLACE INTO session_prefs(umo, key, value) VALUES (?,?,?)",
                (umo, key, value),
            )
            conn.commit()

        await self._run(_work)

    async def sessions_with_pref(self, key: str, value: str) -> list[str]:
        def _work() -> list[str]:
            conn = self._connection()
            rows = conn.execute(
                "SELECT umo FROM session_prefs WHERE key=? AND value=?", (key, value)
            ).fetchall()
            return [row["umo"] for row in rows]

        return await self._run(_work)

    async def all_prefs(self) -> list[dict[str, str]]:
        def _work() -> list[dict[str, str]]:
            conn = self._connection()
            rows = conn.execute("SELECT umo, key, value FROM session_prefs").fetchall()
            return [dict(row) for row in rows]

        return await self._run(_work)

    # -- KV（缓存快照、上次运行时间等） --------------------------------------

    async def kv_get(self, key: str, default: Any = None) -> Any:
        def _work() -> Any:
            conn = self._connection()
            row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
            if row is None:
                return default
            try:
                return json.loads(row["value"])
            except (ValueError, TypeError):
                return default

        return await self._run(_work)

    async def kv_set(self, key: str, value: Any) -> None:
        blob = json.dumps(value, ensure_ascii=False)

        def _work() -> None:
            conn = self._connection()
            conn.execute(
                "INSERT OR REPLACE INTO kv(key, value, at) VALUES (?,?,?)",
                (key, blob, time.time()),
            )
            conn.commit()

        await self._run(_work)

    # -- 导入导出 -----------------------------------------------------------

    async def export_all(self, umo: str = "") -> dict[str, Any]:
        watch = await self.list_watch(umo)
        subs = await self.list_subscriptions(umo)
        prefs = await self.all_prefs()
        return {
            "version": SCHEMA_VERSION,
            "exported_at": time.time(),
            "scope": umo or "all",
            "watchlist": [
                {
                    "umo": item.umo,
                    "subject_id": item.subject_id,
                    "title": item.title,
                    "status": item.status,
                    "progress": item.progress,
                    "total": item.total,
                    "score": item.score,
                    "weekday": item.weekday,
                    "note": item.note,
                }
                for item in watch
            ],
            "subscriptions": [
                {
                    "umo": sub.umo,
                    "name": sub.name,
                    "url": sub.url,
                    "enabled": sub.enabled,
                    "subject_id": sub.subject_id,
                    "keywords": list(sub.keywords),
                    "excludes": list(sub.excludes),
                }
                for sub in subs
            ],
            "prefs": [pref for pref in prefs if not umo or pref["umo"] == umo],
        }

    async def import_all(self, payload: dict[str, Any], *, umo: str = "") -> dict[str, int]:
        """导入导出的备份。「umo」 非空时强制落到该会话，用于跨群搬家。"""

        counters = {"watchlist": 0, "subscriptions": 0, "prefs": 0}
        for raw in payload.get("watchlist") or []:
            target = umo or str(raw.get("umo") or "")
            title = str(raw.get("title") or "").strip()
            if not target or not title:
                continue
            try:
                await self.upsert_watch(
                    WatchItem(
                        id=0,
                        umo=target,
                        subject_id=int(raw.get("subject_id") or 0),
                        title=title,
                        status=str(raw.get("status") or "watching"),
                        progress=int(raw.get("progress") or 0),
                        total=int(raw.get("total") or 0),
                        score=float(raw.get("score") or 0),
                        weekday=int(raw.get("weekday") or 0),
                        note=str(raw.get("note") or ""),
                    )
                )
                counters["watchlist"] += 1
            except (ValueError, TypeError):
                continue
        for raw in payload.get("subscriptions") or []:
            target = umo or str(raw.get("umo") or "")
            name = str(raw.get("name") or "").strip()
            url = str(raw.get("url") or "").strip()
            if not target or not name or not url:
                continue
            try:
                await self.add_subscription(
                    Subscription(
                        id=0,
                        umo=target,
                        name=name,
                        url=url,
                        enabled=bool(raw.get("enabled", True)),
                        subject_id=int(raw.get("subject_id") or 0),
                        keywords=tuple(raw.get("keywords") or ()),
                        excludes=tuple(raw.get("excludes") or ()),
                    )
                )
                counters["subscriptions"] += 1
            except (ValueError, TypeError):
                continue
        for raw in payload.get("prefs") or []:
            target = umo or str(raw.get("umo") or "")
            key = str(raw.get("key") or "")
            if target and key:
                await self.set_pref(target, key, str(raw.get("value") or ""))
                counters["prefs"] += 1
        return counters

    # -- 统计 ---------------------------------------------------------------

    async def stats(self) -> dict[str, Any]:
        def _work() -> dict[str, Any]:
            conn = self._connection()

            def count(sql: str) -> int:
                return int(conn.execute(sql).fetchone()[0] or 0)

            size = self.path.stat().st_size if self.path.exists() else 0
            return {
                "watchlist": count("SELECT COUNT(*) FROM watchlist"),
                "watching": count("SELECT COUNT(*) FROM watchlist WHERE status='watching'"),
                "subscriptions": count("SELECT COUNT(*) FROM subscriptions"),
                "subscriptions_enabled": count(
                    "SELECT COUNT(*) FROM subscriptions WHERE enabled=1"
                ),
                "sessions": count("SELECT COUNT(DISTINCT umo) FROM subscriptions"),
                "history": count("SELECT COUNT(*) FROM push_history"),
                "db_bytes": size,
            }

        return await self._run(_work)


def _watch_from_row(row: sqlite3.Row) -> WatchItem:
    return WatchItem(
        id=int(row["id"]),
        umo=row["umo"],
        subject_id=int(row["subject_id"]),
        title=row["title"],
        status=row["status"],
        progress=int(row["progress"]),
        total=int(row["total"]),
        score=float(row["score"]),
        cover=row["cover"],
        weekday=int(row["weekday"]),
        note=row["note"],
        updated_at=float(row["updated_at"]),
    )


def _sub_from_row(row: sqlite3.Row) -> Subscription:
    return Subscription(
        id=int(row["id"]),
        umo=row["umo"],
        name=row["name"],
        url=row["url"],
        enabled=bool(row["enabled"]),
        subject_id=int(row["subject_id"]),
        keywords=_split(row["keywords"]),
        excludes=_split(row["excludes"]),
        last_checked=float(row["last_checked"]),
        last_item=row["last_item"],
        error=row["error"],
        created_at=float(row["created_at"]),
    )
