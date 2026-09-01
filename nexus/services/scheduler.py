"""定时调度：每日播报、RSS 轮询、anime1 刷新、历史清理。

上游 「astrbot_plugin_bangumi_calendar」 用的是裸 「asyncio.sleep(interval)」，
累积漂移会让 08:30 的播报慢慢变成 08:34；「astrbot_plugin_anime1_list」 又为了
一个 cron 引入 apscheduler 依赖。这里两头都不取：自己写一个「对齐整分钟」的
循环，每分钟醒一次、比对上一次醒来的时刻，于是

* 触发时刻精确到分钟，长期不漂移；
* 机器休眠 / 事件循环卡住导致错过的时刻，在宽限期内会补跑一次（misfire 容忍）；
* 零额外依赖，插件卸载时一个 「task.cancel()」 就干净收工。

Copyright (C) 2026 Whereis-Alice and AstrBot Plugin Authors.
Licensed under the GNU Affero General Public License v3.0 or later.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta

from astrbot.api import logger

from ..models import Notification
from .anirss import AniRssSyncService
from .base import PREF_DAILY, Deps
from .notifier import Notifier
from .search import SearchService
from .subscriptions import SubscriptionService
from .watchlist import WatchlistService, backfill_progress

# 错过的触发时刻，落后不超过这个秒数就补跑；再久就当这一轮作废，
# 免得笔记本合盖一整天、开盖瞬间被十几条播报糊脸。
MISFIRE_GRACE_SECONDS = 30 * 60
# 去重历史表的清理频率：跟着轮询走没必要，一天两次足够。
PRUNE_INTERVAL_SECONDS = 12 * 3600
# 单次 tick 的最大执行时间，超时就放弃本轮，绝不阻塞下一分钟。
TICK_TIMEOUT_SECONDS = 300


class Scheduler:
    """插件里唯一的后台循环。

    刻意只开一个 task：三件定时活儿（播报 / 轮询 / anime1 刷新）都是轻量 IO，
    串在同一分钟里跑完比开三个循环更好观测，也不会互相抢 HTTP 连接池。
    """

    def __init__(
        self,
        deps: Deps,
        *,
        search: SearchService,
        subscriptions: SubscriptionService,
        notifier: Notifier,
        watchlist: WatchlistService | None = None,
        anirss: AniRssSyncService | None = None,
    ) -> None:
        self._deps = deps
        self._search = search
        self._subs = subscriptions
        self._notifier = notifier
        self._watchlist = watchlist
        self._anirss = anirss
        self._task: asyncio.Task[None] | None = None
        self._last_tick: datetime | None = None
        self._next_rss: datetime | None = None
        self._next_anirss: datetime | None = None
        self._last_prune = 0.0
        self._polls = 0
        self._pushes = 0
        self._errors = 0
        self._started_at = 0.0

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> None:
        """拉起后台循环。重复调用是安全的。"""
        if self._task is not None and not self._task.done():
            return
        self._started_at = time.time()
        self._task = asyncio.create_task(self._loop(), name="bangumi-nexus-scheduler")
        self._deps.activity.info("scheduler", "调度器已启动")

    async def stop(self) -> None:
        """停止循环并等它真正退出，避免卸载后还有请求在飞。"""
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        self._deps.activity.info("scheduler", "调度器已停止")

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    async def _loop(self) -> None:
        # 启动时不立刻跑一轮：插件刚加载时 HTTP 客户端、数据源缓存都还是冷的，
        # 等到下一个整分钟再开始，日志也更干净。
        while True:
            await self._sleep_to_next_minute()
            now = datetime.now()
            previous = self._last_tick
            self._last_tick = now
            try:
                await asyncio.wait_for(self._tick(now, previous), TICK_TIMEOUT_SECONDS)
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                self._errors += 1
                self._deps.activity.warn("scheduler", "本轮任务超时，已跳过")
            except Exception as error:  # noqa: BLE001 - 循环必须活着
                self._errors += 1
                self._deps.activity.error("scheduler", f"调度异常：{error}")
                logger.warning(f"番剧中枢调度异常：{error}")

    @staticmethod
    async def _sleep_to_next_minute() -> None:
        """睡到下一个整分钟再多 0.5 秒。

        多睡半秒是为了确保醒来时 「datetime.now().second」 已经跨过 0，
        否则边界抖动会让同一分钟被处理两次。
        """
        now = time.time()
        await asyncio.sleep(60.0 - (now % 60.0) + 0.5)

    async def _tick(self, now: datetime, previous: datetime | None) -> None:
        conf = self._deps.conf
        if conf.push_enabled and self._due_slots(now, previous, conf.push_times):
            await self.run_daily()
        if conf.rss_enabled:
            if self._next_rss is None or now >= self._next_rss:
                self._next_rss = now + timedelta(minutes=max(1, conf.rss_interval_minutes))
                await self.run_rss()
        elif self._next_rss is not None:
            self._next_rss = None
        if conf.anime1_enabled:
            hours = tuple(f"{hour:02d}:00" for hour in conf.anime1_refresh_hours)
            if self._due_slots(now, previous, hours):
                await self.refresh_anime1()
        # ani-rss 同步走间隔而不是固定时刻：它是「跟本地下载器对齐」，
        # 越接近实时越好，没有「早上八点半播报」那种仪式感需求。
        if conf.anirss_enabled and conf.anirss_sync_interval_minutes > 0:
            if self._next_anirss is None or now >= self._next_anirss:
                self._next_anirss = now + timedelta(
                    minutes=max(1, conf.anirss_sync_interval_minutes)
                )
                await self.run_anirss()
        elif self._next_anirss is not None:
            self._next_anirss = None
        await self._maybe_prune()

    @staticmethod
    def _due_slots(
        now: datetime, previous: datetime | None, slots: tuple[str, ...]
    ) -> tuple[str, ...]:
        """挑出「上次醒来之后、现在之前」应当触发的时刻。

        用区间判断而不是 「now.strftime() in slots」：后者一旦某一分钟被跳过
        （GC 停顿、系统休眠）就永久丢掉这一轮，前者能在宽限期内补上。
        """
        if not slots:
            return ()
        floor = previous or (now - timedelta(minutes=1))
        earliest = now - timedelta(seconds=MISFIRE_GRACE_SECONDS)
        if floor < earliest:
            floor = earliest
        due: list[str] = []
        for slot in slots:
            try:
                hour, _, minute = slot.partition(":")
                moment = now.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
            except ValueError:
                continue
            if floor < moment <= now:
                due.append(slot)
        return tuple(due)

    # ------------------------------------------------------------------
    # 每日播报
    # ------------------------------------------------------------------
    async def run_daily(self, *, targets: tuple[str, ...] = (), weekday: int = 0) -> int:
        """把今天的放送表推给所有目标会话，返回成功条数。

        目标有两个来源：插件配置里的 「push_targets」（管理员统一配），
        以及各会话自己用 「/日历订阅 开」 登记的偏好（群自助）。
        两者合并去重，所以群里开过之后管理员不必再抄一遍群号。
        """
        deps = self._deps
        sessions = tuple(targets) or await self.push_targets()
        if not sessions:
            deps.activity.warn("push", "每日播报没有目标会话，已跳过")
            return 0
        day = weekday or datetime.now().isoweekday()
        sent = 0
        # 卡片按会话主题渲染，所以逐个会话生成；同一主题的结果会命中渲染缓存。
        for session in sessions:
            try:
                reply = await self._search.digest(session, weekday=day)
            except Exception as error:  # noqa: BLE001
                deps.activity.error("push", f"生成播报失败：{error}")
                continue
            if reply.empty:
                deps.activity.info("push", "今日没有符合过滤条件的番，跳过播报")
                continue
            facts = _facts_of(reply.text)
            if await self._notifier.send_reply(reply, session, facts=facts):
                sent += 1
        self._pushes += sent
        deps.activity.info("push", f"每日播报完成，成功 {sent}/{len(sessions)}")
        return sent

    async def push_targets(self) -> tuple[str, ...]:
        """配置目标 + 会话自助订阅，合并去重。"""
        deps = self._deps
        resolved = list(self._notifier.resolve_targets(deps.conf.push_targets))
        try:
            resolved.extend(await deps.store.sessions_with_pref(PREF_DAILY, "1"))
        except Exception as error:  # noqa: BLE001
            deps.activity.warn("push", f"读取会话订阅失败：{error}")
        return tuple(dict.fromkeys(item for item in resolved if item))

    # ------------------------------------------------------------------
    # RSS 轮询
    # ------------------------------------------------------------------
    async def run_rss(self, *, umo: str = "", force: bool = False) -> int:
        """轮询一轮订阅源并投递更新，返回发出的通知条数。"""
        deps = self._deps
        self._polls += 1
        try:
            results = await self._subs.poll(umo=umo, force=force)
        except Exception as error:  # noqa: BLE001
            self._errors += 1
            deps.activity.error("rss", f"轮询失败：{error}")
            return 0
        if not results:
            return 0
        sent = 0
        for session, notification in results:
            if await self._notifier.send(notification, session):
                sent += 1
            # 回填放在投递之后：字幕组发了新集，说明这一集确实存在，进度理应跟上。
            # 只在投递成功后做，是为了让「进度动了」和「用户收到通知」保持一致 ——
            # 否则进度悄悄往前跳，用户会以为自己漏看了一条播报。
            await self._backfill(session, notification)
        deps.activity.info("rss", f"轮询产出 {len(results)} 条更新，投递成功 {sent} 条")
        return sent

    async def _backfill(self, session: str, notification: Notification) -> None:
        """RSS 更新顺手把追番进度推到这一集。

        「targets」 只给这一条通知实际投递到的会话：订阅是按会话建的，
        A 群订的番不该把 B 群的进度也一起推动。
        """

        deps = self._deps
        if not deps.conf.rss_auto_progress or self._watchlist is None:
            return
        try:
            episode = int(notification.payload.get("episode") or 0)
        except (TypeError, ValueError):
            return
        if episode <= 0:
            return
        await backfill_progress(
            deps,
            self._watchlist,
            title=notification.title,
            episode=episode,
            targets=(session,),
            channel="rss",
        )

    # ------------------------------------------------------------------
    # ani-rss 同步
    # ------------------------------------------------------------------
    async def run_anirss(self, *, force: bool = False) -> dict[str, object]:
        """跑一轮 ani-rss 同步。异常一律吞掉：本地下载器不在线是常态。"""

        deps = self._deps
        if self._anirss is None:
            return {"ok": False, "error": "同步服务未装配"}
        try:
            return await self._anirss.sync(force=force)
        except Exception as error:  # noqa: BLE001 - 循环必须活着
            self._errors += 1
            deps.activity.error("anirss", f"同步失败：{error}")
            return {"ok": False, "error": str(error)}

    # ------------------------------------------------------------------
    # anime1 缓存刷新与历史清理
    # ------------------------------------------------------------------
    async def refresh_anime1(self, *, force: bool = True) -> int:
        """刷新 anime1 番剧表缓存，返回条目数。"""
        deps = self._deps
        try:
            entries = await deps.hub.anime1.refresh(force=force)
        except Exception as error:  # noqa: BLE001
            self._errors += 1
            deps.activity.error("anime1", f"刷新失败：{error}")
            return 0
        count = len(entries or ())
        deps.activity.info("anime1", f"番剧表已刷新，共 {count} 条")
        return count

    async def _maybe_prune(self) -> None:
        """定期清掉过期的去重记录，别让 SQLite 无限长大。"""
        now = time.time()
        if now - self._last_prune < PRUNE_INTERVAL_SECONDS:
            return
        self._last_prune = now
        try:
            removed = await self._deps.store.prune_history(self._deps.conf.rss_history_days)
        except Exception as error:  # noqa: BLE001
            self._deps.activity.warn("store", f"清理历史失败：{error}")
            return
        if removed:
            self._deps.activity.info("store", f"清理了 {removed} 条过期去重记录")

    # ------------------------------------------------------------------
    # 观测
    # ------------------------------------------------------------------
    def snapshot(self) -> dict[str, object]:
        """给 「/sub_status」 与 WebUI 用的运行快照。"""
        conf = self._deps.conf
        return {
            "running": "运行中" if self.running else "未启动",
            "next_rss": self._next_rss.strftime("%H:%M") if self._next_rss else "",
            "next_anirss": self._next_anirss.strftime("%H:%M") if self._next_anirss else "",
            "next_push": self._next_push(),
            "polls": self._polls,
            "pushes": self._pushes,
            "errors": self._errors,
            "uptime": int(time.time() - self._started_at) if self._started_at else 0,
            "push_enabled": conf.push_enabled,
            "rss_enabled": conf.rss_enabled,
            "last_tick": self._last_tick.strftime("%H:%M") if self._last_tick else "",
        }

    def _next_push(self) -> str:
        """算出下一次播报时刻（可能落在明天）。"""
        conf = self._deps.conf
        if not conf.push_enabled or not conf.push_times:
            return ""
        now = datetime.now()
        best: datetime | None = None
        for slot in conf.push_times:
            hour, _, minute = slot.partition(":")
            try:
                moment = now.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
            except ValueError:
                continue
            if moment <= now:
                moment += timedelta(days=1)
            if best is None or moment < best:
                best = moment
        if best is None:
            return ""
        same_day = best.date() == now.date()
        return best.strftime("%H:%M") if same_day else best.strftime("明天 %H:%M")


def _facts_of(text: str, *, limit: int = 260) -> str:
    """从播报纯文本里截一段当人格口播的素材。

    整张放送表几十行喂给模型既贵又容易让它开始逐条念，
    截前几行足够它说出「今天有哪几部」。
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    picked: list[str] = []
    used = 0
    for line in lines[:8]:
        used += len(line)
        picked.append(line)
        if used >= limit:
            break
    return "\n".join(picked)


__all__ = ["MISFIRE_GRACE_SECONDS", "Scheduler"]
