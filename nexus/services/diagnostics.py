"""自检与数据维护。

「/番剧诊断」 是这套插件里最实用的运维指令：番剧数据源全是第三方站点，
改版、限流、被墙、证书过期都会发生，而用户看到的现象只是「指令没反应」。
这里把每个源单独探一次、各自计时，一张卡就能指出到底是哪一环断了。

另外收纳三条数据维护指令（来自 「astrbot_plugin_anime1_list」 与
「astrbot_plugin_anime_gacha」）：刷新 anime1 索引、检查缓存概况、
预热指定季度的数据。

Copyright (C) 2026 Whereis-Alice and AstrBot Plugin Authors.
Licensed under the GNU Affero General Public License v3.0 or later.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence

from ..render import build_diagnose_card
from ..sources.rss import mikan_classic_feed, rsshub_feed
from ..titles import season_code, season_codes_around, season_label
from .base import Deps, Reply, make_card, style_for

# 单个探针的超时：慢不等于坏，但等太久会让诊断本身变成阻塞操作。
PROBE_TIMEOUT = 12.0

Probe = tuple[str, Callable[[], Awaitable[str]]]


class DiagnosticsService:
    """数据源健康检查与缓存维护。"""

    def __init__(self, deps: Deps) -> None:
        self._deps = deps

    # ------------------------------------------------------------------
    # /番剧诊断
    # ------------------------------------------------------------------
    async def diagnose(self, umo: str) -> Reply:
        """逐源探活并出一张诊断卡。"""
        deps = self._deps
        results = await self.run_probes()
        theme, _ = await style_for(deps, umo)
        html = build_diagnose_card(theme, results, width=max(deps.conf.card_width, 880))
        plain = _diagnose_plain(results)
        ok_count = sum(1 for _, ok, _, _ in results if ok)
        return Reply(
            text=plain,
            card=make_card(
                html,
                plain=plain,
                title="番剧中枢自检",
                eyebrow="DIAGNOSE",
                subtitle=f"{ok_count}/{len(results)} 项正常",
                theme=theme,
                width=max(deps.conf.card_width, 880),
            ),
        )

    async def run_probes(self) -> tuple[tuple[str, bool, str, float], ...]:
        """并发跑完所有探针，返回 「(名称, 是否正常, 说明, 耗时秒)」。

        并发是刻意的：串行探八个站点，光超时就能攒到一分多钟。
        """
        probes = self._probes()
        results = await asyncio.gather(*(self._run(name, fn) for name, fn in probes))
        return tuple(results)

    def _probes(self) -> tuple[Probe, ...]:
        deps = self._deps
        conf = deps.conf
        hub = deps.hub

        async def bangumi() -> str:
            days = await hub.bangumi.calendar()
            total = sum(len(day.items) for day in days)
            return f"7 天共 {total} 部" if total else "接口有响应但没有条目"

        async def bangumi_data() -> str:
            items = await hub.bangumi_data.season(season_code())
            return f"本季 {len(items)} 条"

        async def anime1() -> str:
            entries = await hub.anime1.entries()
            return f"索引 {len(entries)} 条"

        async def yuc() -> str:
            table = await hub.yuc.season(season_code())
            if table is None or not table.entries:
                return "抓到页面但没解析出条目（站点可能改版）"
            return f"{season_label(table.code)} 共 {table.total} 部"

        async def age() -> str:
            items = await hub.age.recommend(limit=6)
            return f"推荐位 {len(items)} 条"

        async def moegirl() -> str:
            hits = await hub.moegirl.search("动画", limit=1)
            return f"搜索返回 {len(hits)} 条"

        async def mikan() -> str:
            ok, detail, count = await hub.rss.probe(mikan_classic_feed(conf.mikan_base))
            if not ok:
                raise RuntimeError(detail or "无法解析")
            return f"最新 {count} 条"

        async def rsshub() -> str:
            url = rsshub_feed(conf.rsshub_base, "/bangumi/tv/calendar/today")
            ok, detail, count = await hub.rss.probe(url)
            if not ok:
                raise RuntimeError(detail or "无法解析")
            return f"实例可用，返回 {count} 条"

        async def renderer() -> str:
            card = await deps.engine.render(_probe_card(conf.card_theme), conf)
            if not card.has_image:
                raise RuntimeError(f"只拿到文本回退（{card.backend}）")
            return f"{card.backend} · {card.elapsed:.1f}s"

        async def database() -> str:
            stats = await deps.store.stats()
            return (
                f"追番 {stats.get('watchlist', 0)} · 订阅 {stats.get('subscriptions', 0)}"
                f" · {int(stats.get('db_bytes', 0)) // 1024} KB"
            )

        probes: list[Probe] = [
            ("Bangumi 番组计划", bangumi),
            ("bangumi-data", bangumi_data),
            ("长门番堂 yuc.wiki", yuc),
            ("AGE 动漫", age),
            ("萌娘百科", moegirl),
            ("Mikan Project", mikan),
            ("RSSHub 实例", rsshub),
            ("卡片渲染", renderer),
            ("本地数据库", database),
        ]
        if conf.anime1_enabled:
            probes.insert(3, ("anime1.me", anime1))
        return tuple(probes)

    async def _run(
        self, name: str, probe: Callable[[], Awaitable[str]]
    ) -> tuple[str, bool, str, float]:
        started = time.monotonic()
        try:
            note = await asyncio.wait_for(probe(), PROBE_TIMEOUT)
        except asyncio.TimeoutError:
            return name, False, f"超时（>{PROBE_TIMEOUT:.0f}s）", time.monotonic() - started
        except Exception as error:  # noqa: BLE001 - 探针失败本身就是结论
            return (
                name,
                False,
                _short(str(error) or error.__class__.__name__),
                time.monotonic() - started,
            )
        return name, True, note, time.monotonic() - started

    # ------------------------------------------------------------------
    # /anime_update
    # ------------------------------------------------------------------
    async def refresh_anime1(self, umo: str) -> Reply:
        """强制刷新 anime1 索引。"""
        deps = self._deps
        if not deps.conf.anime1_enabled:
            return Reply.plain("anime1 数据源当前是关闭状态，先在配置里打开 anime1_enabled。")
        started = time.monotonic()
        try:
            entries = await deps.hub.anime1.refresh(force=True)
        except Exception as error:  # noqa: BLE001
            deps.activity.error("anime1", f"刷新失败：{error}")
            return Reply.plain(f"刷新失败：{error}")
        elapsed = time.monotonic() - started
        if not entries:
            return Reply.plain("请求成功但没解析出条目，anime1 可能改版了。")
        latest = await deps.hub.anime1.latest(limit=5)
        lines = [f"anime1 索引已刷新：{len(entries)} 条，耗时 {elapsed:.1f}s"]
        if latest:
            lines.append("最近更新：")
            lines.extend(f"· {entry.title} {entry.status}".rstrip() for entry in latest)
        return Reply.plain("\n".join(lines))

    # ------------------------------------------------------------------
    # /检查番剧数据
    # ------------------------------------------------------------------
    async def check_data(self, umo: str) -> Reply:
        """汇报各源缓存与本地库的概况。"""
        deps = self._deps
        hub = deps.hub
        lines = ["番剧数据概况"]
        try:
            store_stats = await deps.store.stats()
            lines.append(
                f"本地库：追番 {store_stats.get('watchlist', 0)} 部 / "
                f"订阅 {store_stats.get('subscriptions', 0)} 条 / "
                f"去重记录 {store_stats.get('history', 0)} 条 / "
                f"{int(store_stats.get('db_bytes', 0)) // 1024} KB"
            )
        except Exception as error:  # noqa: BLE001
            lines.append(f"本地库：读取失败（{error}）")

        for label, source in (
            ("bangumi-data", hub.bangumi_data),
            ("anime1", hub.anime1),
            ("长门番堂", hub.yuc),
            ("AGE 动漫", hub.age),
            ("萌娘百科", hub.moegirl),
        ):
            getter = getattr(source, "stats", None)
            if getter is None:
                continue
            try:
                payload = getter()
                if asyncio.iscoroutine(payload):
                    payload = await payload
            except Exception as error:  # noqa: BLE001
                lines.append(f"{label}：{error}")
                continue
            lines.append(f"{label}：{_render_stats(payload)}")

        cached = hub.yuc.cached_seasons()
        lines.append("已缓存季度：" + ("、".join(cached) if cached else "无"))
        lines.append(f"HTTP 缓存：{_render_stats(hub.http.stats())}")
        lines.append("")
        lines.append("需要预热某个季度时用 /更新番剧数据 202607。")
        return Reply.plain("\n".join(lines))

    # ------------------------------------------------------------------
    # /更新番剧数据
    # ------------------------------------------------------------------
    async def update_data(self, umo: str, code: str = "") -> Reply:
        """预热指定季度（默认当前季及前后一季）的跨源数据。

        用途是把季度表、bangumi-data 月度文件、anime1 索引一次性拉热，
        之后的 「/抽番」 「/季度新番」 「/查番」 就不必现场等网络。
        """
        deps = self._deps
        wanted = code.strip()
        codes = (wanted,) if wanted else season_codes_around(span=1)
        lines = ["数据预热结果"]
        for target in codes:
            started = time.monotonic()
            detail = []
            try:
                table = await deps.hub.yuc.season(target, force=bool(wanted))
                detail.append(f"季度表 {table.total if table else 0} 部")
            except Exception as error:  # noqa: BLE001
                detail.append(f"季度表失败（{_short(str(error))}）")
            try:
                items = await deps.hub.bangumi_data.season(target)
                detail.append(f"bangumi-data {len(items)} 条")
            except Exception as error:  # noqa: BLE001
                detail.append(f"bangumi-data 失败（{_short(str(error))}）")
            lines.append(
                f"· {season_label(target)}："
                + " / ".join(detail)
                + f"（{time.monotonic() - started:.1f}s）"
            )
        if deps.conf.anime1_enabled:
            try:
                entries = await deps.hub.anime1.refresh(force=True)
                lines.append(f"· anime1 索引：{len(entries)} 条")
            except Exception as error:  # noqa: BLE001
                lines.append(f"· anime1 索引失败（{_short(str(error))}）")
        deps.activity.info("maintain", "数据预热完成：" + "、".join(codes))
        return Reply.plain("\n".join(lines))


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def _probe_card(theme: str):
    """构造一张极小的卡片用于渲染探活，避免占用真实模板的缓存位。"""
    html = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<style>html,body{margin:0;background:#101425;color:#e8ecff;"
        "font:600 28px/1.4 system-ui,sans-serif}"
        ".box{padding:36px 44px}</style></head>"
        f"<body><div class='box'>Bangumi Nexus · {theme} · 渲染自检</div></body></html>"
    )
    return make_card(
        html, plain="渲染自检", title="渲染自检", eyebrow="PROBE", theme=theme, width=680
    )


def _render_stats(payload: object) -> str:
    if isinstance(payload, dict):
        return " ".join(f"{key}={value}" for key, value in payload.items())
    return str(payload)


def _short(text: str, *, limit: int = 60) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _diagnose_plain(results: Sequence[tuple[str, bool, str, float]]) -> str:
    lines = ["番剧中枢自检结果："]
    for name, ok, note, elapsed in results:
        mark = "正常" if ok else "异常"
        lines.append(f"[{mark}] {name} · {note} · {elapsed:.1f}s")
    return "\n".join(lines)


__all__ = ["PROBE_TIMEOUT", "DiagnosticsService"]
