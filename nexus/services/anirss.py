"""ani-rss 同步：把本地下载器里的订阅拉进插件的追番表 / RSS 订阅。

用户的真实工作流是「在电脑上的 ani-rss 里点订阅」，而不是在聊天窗口里一条条
「/追番」。这个服务把两边对齐，于是 ani-rss 是唯一的录入口，插件负责播报与查询。

三条设计约束：

* **单向**：只读 ani-rss，绝不回写。下载器的配置是用户的资产，插件没有资格改。
* **不删**：ani-rss 里删掉的条目，本地只报告成「已失联」，不自动删。
  自动删是不可逆操作，而误配一次地址就可能让整张追番表被清空。
* **进度只往前**：ani-rss 的 「currentEpisodeNumber」 是「下载到第几集」，
  用户实际看到哪一集只有插件这边知道，所以取两者较大值。

同步结果走**独立推送链**：只发到 「anirss_sync_targets」 指定的会话，
绝不混进 RSS 更新那条链 —— 那条链是给「有新集了」用的，同步账目刷进去会吵。

Copyright (C) 2026 Whereis-Alice and AstrBot Plugin Authors.
Licensed under the GNU Affero General Public License v3.0 or later.
"""

from __future__ import annotations

import json
import time
from typing import Any

from ..models import Notification, Subscription, WatchItem
from ..render import build_anirss_card
from ..sources.anirss import (
    AniEntry,
    AniRssError,
    AniRssSnapshot,
    AniRssSource,
    parse_snapshot,
    unwrap_payload,
)
from .base import Deps, Reply, make_card, style_for

#: 卡片和通知里最多列几条，超出只报总数。ani-rss 里挂三五十条订阅是常态。
LIST_LIMIT = 14
#: 上次同步时间戳存在 KV 里，重启不丢 —— 否则每次重启都会立刻触发一次全量同步。
KV_LAST_SYNC = "anirss:last_sync"
KV_LAST_RESULT = "anirss:last_result"
#: 鉴权方式的中文说法。放在服务层而不是模板层，是因为指令回执也要用同一套字眼。
AUTH_LABEL = {"api_key": "API Key", "password": "账号密码", "none": "未设置"}


class AniRssSyncService:
    """ani-rss → 番剧中枢 的单向同步。"""

    def __init__(self, deps: Deps, *, notifier: Any = None) -> None:
        self._deps = deps
        self._notifier = notifier
        self._last_error = ""

    # ------------------------------------------------------------------
    # 客户端
    # ------------------------------------------------------------------
    def _source(self) -> AniRssSource:
        """每次现场构造客户端。

        地址和密钥都在 AstrBot 配置里，用户可能刚在 WebUI 改完就点同步；
        缓存一个实例会拿着旧凭据一直失败，得不偿失 —— 建对象本身不花钱。
        """

        conf = self._deps.conf
        return AniRssSource(
            self._deps.http,
            base=conf.anirss_base,
            api_key=conf.anirss_api_key,
            username=conf.anirss_username,
            password=conf.anirss_password,
            verify_tls=conf.anirss_verify_tls,
        )

    # ------------------------------------------------------------------
    # 同步主流程
    # ------------------------------------------------------------------
    async def sync(self, *, targets: tuple[str, ...] = (), force: bool = False) -> dict[str, Any]:
        """跑一次同步，返回给 WebUI / 指令用的结果字典。

        「force」 会先让 ani-rss 重扫一遍 RSS 再拉列表 —— 手动点同步的人通常
        就是刚在下载器里加完订阅，等自动轮询才刷新会让人以为同步坏了。
        定时同步不 force，避免每小时给下载器一次全量重扫。
        """

        deps = self._deps
        source = self._source()
        if not source.configured:
            self._last_error = "还没配置 ani-rss 地址或密钥"
            return {"ok": False, "error": self._last_error, **source.describe()}
        if force:
            try:
                await source.refresh_all()
            except AniRssError as error:
                # 重扫失败不该阻断同步：拿到的只是稍旧一点的进度，比整轮失败好。
                deps.activity.warn("anirss", f"请求 ani-rss 重扫失败，继续用现有数据：{error}")
        try:
            snapshot = await source.list_ani()
        except AniRssError as error:
            self._last_error = error.message
            deps.activity.warn("anirss", f"读取 ani-rss 失败：{error.message}")
            return {"ok": False, "error": error.message, **source.describe()}
        self._last_error = ""

        return await self._commit(snapshot, targets, extra=source.describe(), origin="同步")

    async def import_snapshot(self, raw: Any, *, targets: tuple[str, ...] = ()) -> dict[str, Any]:
        """离线导入：吃一份手动导出的 「listAni」 响应，全程不碰网络。

        为什么必须留这条路：ani-rss 基本都跑在自己电脑上，AstrBot 多半在公网服务器上。
        服务器要主动连回家里的 7789 端口，就得内网穿透或在路由器上开端口 —— 不是谁都愿意
        为了同步一张追番表把下载器暴露到公网。把 JSON 搬过来能得到完全一样的结果，
        而且一行网络配置都不用改，也不需要填任何凭据。

        「raw」 既可以是 JSON 文本，也可以是已经解析好的对象；包封和 「data」 两层都认。
        """

        deps = self._deps
        payload: Any = raw
        if isinstance(raw, (str, bytes, bytearray)):
            text = raw if isinstance(raw, str) else bytes(raw).decode("utf-8", "replace")
            if not text.strip():
                return {"ok": False, "error": "导入内容是空的"}
            try:
                payload = json.loads(text)
            except ValueError as error:
                return {"ok": False, "error": f"不是合法 JSON：{error}"}
        try:
            snapshot = parse_snapshot(unwrap_payload(payload))
        except AniRssError as error:
            return {"ok": False, "error": error.message}
        if not snapshot.entries:
            return {
                "ok": False,
                "error": "这份数据里没有订阅条目，确认导出的是 「POST /api/listAni」 的响应",
            }
        self._last_error = ""
        deps.activity.info("anirss", f"离线导入 {len(snapshot.entries)} 条订阅")
        return await self._commit(
            snapshot, targets, extra=self._source().describe(), origin="离线导入"
        )

    async def _commit(
        self,
        snapshot: AniRssSnapshot,
        targets: tuple[str, ...],
        *,
        extra: dict[str, Any],
        origin: str,
    ) -> dict[str, Any]:
        """把一份快照落库并记账。在线同步与离线导入共用这一段。

        抽出来是因为两条入口只在「快照从哪来」上不同，落库、写 KV、发同步账目卡
        这三件事必须一模一样 —— 复制一份出去迟早会有一边悄悄走偏。
        """

        deps = self._deps
        conf = deps.conf
        sessions = tuple(dict.fromkeys(t for t in (targets or conf.anirss_sync_targets) if t))
        if not sessions:
            note = "同步目标为空：先在配置里填 「anirss_sync_targets」，否则不知道往哪个会话的追番表里写"
            deps.activity.warn("anirss", note)
            return {"ok": False, "error": note, "total": snapshot.total, **extra}

        report = _Report()
        for session in sessions:
            await self._apply(session, snapshot, report)
        await deps.store.kv_set(KV_LAST_SYNC, time.time())
        result = {
            "ok": True,
            "origin": origin,
            "total": snapshot.total,
            "entries": len(snapshot.entries),
            "active": len(snapshot.active),
            "sessions": list(sessions),
            **report.payload(),
            **extra,
        }
        await deps.store.kv_set(KV_LAST_RESULT, {k: v for k, v in result.items() if k != "ok"})
        deps.activity.info("anirss", f"{origin}：{report.summary(len(snapshot.active))}")
        if conf.anirss_notify_on_change and report.changed:
            await self._announce(snapshot, report, origin=origin)
        return result

    async def _apply(self, umo: str, snapshot: AniRssSnapshot, report: _Report) -> None:
        """把快照落到一个会话上。"""

        deps = self._deps
        conf = deps.conf
        links = {row["ani_id"]: row for row in await deps.store.list_anirss_links(umo)}
        existing = {item.id: item for item in await deps.store.list_watch(umo)}
        seen: set[str] = set()
        for entry in snapshot.entries:
            if entry.ova and not entry.enabled:
                # 停用的 OVA 条目在 ani-rss 里多半是「加错了」的残留，不往本地搬。
                continue
            seen.add(entry.ani_id)
            watch_id = 0
            sub_id = 0
            if conf.anirss_sync_watchlist:
                watch_id = await self._sync_watch(umo, entry, links, existing, report)
            if conf.anirss_sync_subscriptions and entry.url:
                sub_id = await self._sync_subscription(umo, entry, report)
            if watch_id or sub_id:
                await deps.store.remember_anirss_link(
                    umo,
                    entry.ani_id,
                    watch_id=watch_id,
                    sub_id=sub_id,
                    title=entry.display_title,
                )
        report.orphans.extend(
            str(row.get("title") or ani_id) for ani_id, row in links.items() if ani_id not in seen
        )

    async def _sync_watch(
        self,
        umo: str,
        entry: AniEntry,
        links: dict[str, dict[str, Any]],
        existing: dict[int, WatchItem],
        report: _Report,
    ) -> int:
        """把一条 ani-rss 订阅同步成追番表里的一行，返回本地行 id。"""

        deps = self._deps
        linked = links.get(entry.ani_id) or {}
        current = existing.get(int(linked.get("watch_id") or 0))
        if current is None:
            current = await deps.store.find_watch(umo, entry.display_title)
        progress = entry.progress
        status = "finished" if entry.completed else "watching"
        if current is not None:
            # 已看进度只增不减；用户手动标过「已弃坑」也尊重，不被同步覆盖回「在追」。
            progress = max(progress, current.progress)
            if current.status == "dropped":
                status = current.status
        item = WatchItem(
            id=current.id if current else 0,
            umo=umo,
            subject_id=entry.subject_id or (current.subject_id if current else 0),
            title=entry.display_title,
            status=status,
            progress=progress,
            total=entry.total or (current.total if current else 0),
            score=entry.score or (current.score if current else 0.0),
            cover=entry.cover or (current.cover if current else ""),
            weekday=entry.weekday or (current.weekday if current else 0),
            note=current.note if current else "由 ani-rss 同步",
        )
        try:
            saved = await deps.store.upsert_watch(item)
        except ValueError as error:
            # 超出会话上限是可预期的业务错误，逐条记账后继续，不要中断整轮。
            report.failures.append(f"{entry.display_title}：{error}")
            return 0
        except Exception as error:  # noqa: BLE001 - 单条写失败不该拖垮整轮同步
            report.failures.append(f"{entry.display_title}：{error}")
            return 0
        if current is None:
            report.added.append(entry.display_title)
        elif (current.progress, current.total, current.status) != (
            saved.progress,
            saved.total,
            saved.status,
        ):
            report.updated.append(f"{entry.display_title} → {saved.progress_label}")
        return saved.id

    async def _sync_subscription(self, umo: str, entry: AniEntry, report: _Report) -> int:
        """把 ani-rss 的 RSS 地址也搬成插件订阅（默认关）。

        默认关是有意的：ani-rss 已经在下载了，插件再订同一条源等于同一集播两遍。
        只有「想让群里也看到更新播报、但下载器不在群成员手上」时才该开。
        """

        deps = self._deps
        try:
            before = await deps.store.find_subscription(umo, entry.display_title)
            saved = await deps.store.add_subscription(
                Subscription(
                    id=0,
                    umo=umo,
                    name=entry.display_title,
                    url=entry.url,
                    enabled=entry.enabled,
                    subject_id=entry.subject_id,
                    keywords=entry.match,
                    excludes=entry.exclude,
                )
            )
        except ValueError as error:
            report.failures.append(f"{entry.display_title}（订阅）：{error}")
            return 0
        except Exception as error:  # noqa: BLE001 - 同上，逐条记账
            report.failures.append(f"{entry.display_title}（订阅）：{error}")
            return 0
        if before is None:
            report.subscribed.append(entry.display_title)
        return saved.id

    # ------------------------------------------------------------------
    # 同步结果通知（独立链）
    # ------------------------------------------------------------------
    async def _announce(
        self, snapshot: AniRssSnapshot, report: _Report, *, origin: str = "同步"
    ) -> None:
        deps = self._deps
        if self._notifier is None:
            return
        targets = self._notifier.resolve_targets(deps.conf.anirss_sync_targets)
        if not targets:
            return
        notification = Notification(
            kind="anirss_sync",
            title=f"ani-rss {origin}完成",
            subtitle=report.summary(len(snapshot.active)),
            lines=tuple(report.lines()),
        )
        await self._notifier.dispatch(notification, targets)

    # ------------------------------------------------------------------
    # 状态 / 自检 / 卡片
    # ------------------------------------------------------------------
    async def status(self) -> dict[str, Any]:
        """给 WebUI 和 「/anirss」 用的状态字典。不触发写库。"""

        deps = self._deps
        conf = deps.conf
        source = self._source()
        payload: dict[str, Any] = {
            "ok": False,
            "enabled": conf.anirss_enabled,
            "interval": conf.anirss_sync_interval_minutes,
            "targets": list(conf.anirss_sync_targets),
            "sync_watchlist": conf.anirss_sync_watchlist,
            "sync_subscriptions": conf.anirss_sync_subscriptions,
            "notify_on_change": conf.anirss_notify_on_change,
            "error": self._last_error,
            **source.describe(),
        }
        payload["last_at"] = float(await deps.store.kv_get(KV_LAST_SYNC, 0.0) or 0.0)
        payload["last_result"] = await deps.store.kv_get(KV_LAST_RESULT, {}) or {}
        payload["synced"] = len(await deps.store.list_anirss_links())
        if not source.configured:
            payload["error"] = payload["error"] or "还没配置 ani-rss 地址或密钥"
            return payload
        try:
            snapshot = await source.list_ani()
        except AniRssError as error:
            payload["error"] = error.message
            return payload
        payload["ok"] = True
        payload["error"] = ""
        payload["total"] = snapshot.total or len(snapshot.entries)
        payload["active"] = len(snapshot.active)
        payload["items"] = [
            {
                "ani_id": entry.ani_id,
                "title": entry.display_title,
                "summary": entry.summary(),
                "progress": entry.progress,
                "total": entry.total,
                "weekday": entry.weekday,
                "subject_id": entry.subject_id,
                "enabled": entry.enabled,
                "completed": entry.completed,
                "subgroup": entry.subgroup,
                "url": entry.url,
                "cover": entry.cover,
            }
            for entry in snapshot.entries
        ]
        return payload

    async def test(self) -> dict[str, Any]:
        """连通性自检，给 WebUI 的「测试连接」按钮。"""

        source = self._source()
        if not source.configured:
            return {"ok": False, "error": "还没配置 ani-rss 地址或密钥", **source.describe()}
        try:
            return await source.ping()
        except AniRssError as error:
            return {
                "ok": False,
                "error": error.message,
                "status": error.status,
                **source.describe(),
            }

    async def card(self, umo: str) -> Reply:
        """「/anirss」 的状态卡。"""

        deps = self._deps
        conf = deps.conf
        payload = await self.status()
        theme, _ = await style_for(deps, umo)
        items = payload.get("items") or []
        entries = [
            (str(row["title"]), str(row["summary"]), _tail(row)) for row in items[:LIST_LIMIT]
        ]
        status = dict(payload)
        status["last_at_label"] = _stamp(float(payload.get("last_at") or 0.0))
        status["direction"] = _direction(conf)
        status["targets_label"] = "、".join(conf.anirss_sync_targets) or "未指定"
        status["note"] = payload.get("error") or (
            f"另有 {len(items) - LIST_LIMIT} 条没展示" if len(items) > LIST_LIMIT else ""
        )
        html = build_anirss_card(theme, status=status, entries=entries, width=conf.card_width)
        plain = _plain(status, entries, len(items))
        return Reply(
            text=plain,
            card=make_card(
                html,
                plain=plain,
                title="ani-rss 同步",
                eyebrow="ANI-RSS",
                subtitle=str(status.get("base") or ""),
                theme=theme,
                width=conf.card_width,
            ),
        )


class _Report:
    """一轮同步的账目。分类记账是为了让通知能说清「到底动了什么」。"""

    def __init__(self) -> None:
        self.added: list[str] = []
        self.updated: list[str] = []
        self.subscribed: list[str] = []
        self.failures: list[str] = []
        self.orphans: list[str] = []

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated or self.subscribed)

    def payload(self) -> dict[str, Any]:
        return {
            "added": list(self.added),
            "updated": list(self.updated),
            "subscribed": list(self.subscribed),
            "failures": list(self.failures),
            "orphans": list(dict.fromkeys(self.orphans)),
        }

    def summary(self, active: int) -> str:
        bits = [f"读到 {active} 条启用中的订阅"]
        if self.added:
            bits.append(f"新增 {len(self.added)}")
        if self.updated:
            bits.append(f"更新 {len(self.updated)}")
        if self.subscribed:
            bits.append(f"建订阅 {len(self.subscribed)}")
        if self.orphans:
            bits.append(f"失联 {len(dict.fromkeys(self.orphans))}")
        if self.failures:
            bits.append(f"失败 {len(self.failures)}")
        if len(bits) == 1:
            bits.append("没有变化")
        return " · ".join(bits)

    def lines(self) -> list[str]:
        out: list[str] = []
        for label, rows in (
            ("新增追番", self.added),
            ("进度更新", self.updated),
            ("新建订阅", self.subscribed),
        ):
            if rows:
                shown = "、".join(rows[:6])
                more = f" 等 {len(rows)} 条" if len(rows) > 6 else ""
                out.append(f"{label}：{shown}{more}")
        orphans = list(dict.fromkeys(self.orphans))
        if orphans:
            out.append(f"ani-rss 里已不存在（本地保留未删）：{'、'.join(orphans[:6])}")
        if self.failures:
            out.append(f"失败 {len(self.failures)} 条：{self.failures[0]}")
        return out


def _tail(row: dict[str, Any]) -> str:
    if not row.get("enabled"):
        return "已停用"
    if row.get("completed"):
        return "已完结"
    total = int(row.get("total") or 0)
    progress = int(row.get("progress") or 0)
    if total:
        return f"{progress}/{total}"
    return f"至 {progress}" if progress else ""


def _direction(conf: Any) -> str:
    bits = []
    if conf.anirss_sync_watchlist:
        bits.append("追番表")
    if conf.anirss_sync_subscriptions:
        bits.append("RSS 订阅")
    return "ani-rss → " + "、".join(bits) if bits else "两侧都关了，同步不会写任何东西"


def _stamp(moment: float) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(moment)) if moment else ""


def _plain(status: dict[str, Any], entries: list[tuple[str, str, str]], total: int) -> str:
    head = "ani-rss 同步"
    if not status.get("configured"):
        return head + "\n还没配置地址或密钥，去 WebUI 的「ani-rss 同步」板块填一下。"
    if not status.get("ok"):
        return head + f"\n连不上 {status.get('base')}：{status.get('error') or '未知原因'}"
    lines = [
        f"{head}（{status.get('base')}）",
        f"共 {total} 条，上次同步 {status.get('last_at_label') or '还没同步过'}",
    ]
    lines.extend(
        f"{index}. {title} · {note}" + (f" · {tail}" if tail else "")
        for index, (title, note, tail) in enumerate(entries, 1)
    )
    if total > len(entries):
        lines.append(f"另有 {total - len(entries)} 条没展示")
    return "\n".join(lines)


__all__ = ["KV_LAST_RESULT", "KV_LAST_SYNC", "AniRssSyncService"]
