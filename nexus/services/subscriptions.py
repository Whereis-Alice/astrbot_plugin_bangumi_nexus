"""RSS 订阅的增删改查与轮询。

上游 「astrbot_plugin_rsshub」 用了一整套 DDD 分层来做同样的事，这里只保留必要的
行为，并补了三个它没有的东西：

1. **只给番名也能订阅** —— 先跨源匹配出 Mikan 的番剧 ID，再拼出单番 RSS，
   用户不必自己去 Mikan 翻页找 「bangumiId」；
2. **新订阅静默入库** —— 建立订阅时先把当前全部条目标记为已读，
   否则第一次轮询会把整个 RSS 历史一次性推出来（上游的经典翻车点）；
3. **失败只报一次** —— 源挂掉时只在「首次转为失败」时发一条告警，
   恢复后再发一条恢复通知，不会每轮都刷屏。

Copyright (C) 2026 Whereis-Alice and AstrBot Plugin Authors.
Licensed under the GNU Affero General Public License v3.0 or later.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Mapping, Sequence
from typing import Any

from ..config import RENDERERS
from ..constants import MAX_SUBSCRIPTIONS_PER_SESSION
from ..models import MatchResult, Notification, Subscription
from ..render import build_feed_card, build_notice_card, theme_keys
from ..sources.rss import dmhy_feed, normalize_feed_url, rsshub_feed
from .base import (
    PREF_DAILY,
    PREF_RENDERER,
    PREF_TARGET,
    PREF_THEME,
    Deps,
    Reply,
    cover_uri,
    make_card,
    parse_switch,
    style_for,
)

FEED_PREFIXES = ("http://", "https://", "rsshub:", "mikan:", "dmhy:", "/")
RAW_NOTE = "raw"


def _looks_like_feed(token: str) -> bool:
    """判断一个 token 是不是订阅地址（而不是番名的一部分）。"""

    lowered = token.strip().lower()
    return bool(lowered) and lowered.startswith(FEED_PREFIXES)


def split_target(text: str) -> tuple[str, str]:
    """把 「名称 地址」 拆开。

    从右边切而不是从左边切：番名里可以有空格，订阅地址里不可能有。
    末尾那一段不像地址时，就认为用户只给了名称。
    """

    stripped = text.strip()
    parts = stripped.rsplit(None, 1)
    if len(parts) == 2 and _looks_like_feed(parts[1]):
        return parts[0].strip(), parts[1].strip()
    return stripped, ""


class SubscriptionService:
    """订阅表的门面，同时也是 RSS 轮询的执行体。"""

    def __init__(self, deps: Deps) -> None:
        self._deps = deps
        self._polls = 0
        self._pushed = 0

    # ------------------------------------------------------------------
    # /sub
    # ------------------------------------------------------------------
    async def add(self, umo: str, raw: str) -> Reply:
        """新增或更新一条订阅。地址留空时自动去找 Mikan 单番源。"""

        deps = self._deps
        conf = deps.conf
        name, target = split_target(raw)
        if not name:
            return Reply.plain(
                "用法：/sub <名称> <RSS地址>\n"
                "地址可以写完整 URL，也可以用简写：mikan:番剧ID、rsshub:路由、dmhy:关键词。\n"
                "只写名称的话，我会自己去 Mikan 找这部番的源。"
            )

        subject_id = 0
        cover = ""
        notes: list[str] = []
        if not target:
            match = await deps.matcher.by_title(name)
            target = match.mikan_rss
            if not target:
                return Reply.plain(
                    f"没能自动找到「{name}」的 RSS 源。"
                    f"请手动给一个地址，例如：/sub {name} mikan:3141"
                )
            if match.subject is not None:
                subject_id = match.subject.id
                cover = match.subject.image
            notes.extend(match.notes)

        url = normalize_feed_url(target, rsshub_base=conf.rsshub_base, mikan_base=conf.mikan_base)
        if not url:
            return Reply.plain("这个地址我看不懂，给一个 http(s) 开头的 RSS 链接吧。")

        ok, detail, count = await deps.hub.rss.probe(url)
        sub = Subscription(
            id=0,
            umo=umo,
            name=name,
            url=url,
            enabled=True,
            subject_id=subject_id,
            error="" if ok else detail,
        )
        try:
            sub = await deps.store.add_subscription(sub)
        except ValueError as error:
            return Reply.plain(str(error))

        silenced = 0
        if ok and conf.rss_first_poll_silent:
            silenced = await self._silence_history(sub)
        await deps.store.set_subscription_state(
            sub.id, last_checked=time.time() if ok else 0.0, error="" if ok else detail
        )

        lines = [f"名称：{sub.name}", f"地址：{url}"]
        if ok:
            lines.append(f"探测：正常，当前 {count} 条")
            lines.append(f"最新：{detail}")
            if silenced:
                lines.append(f"已把现有 {silenced} 条标记为已读，只推之后的新条目")
        else:
            lines.append(f"探测：失败（{detail}）")
            lines.append("订阅仍然建好了，下一轮会自动重试")
        if subject_id:
            lines.append(f"关联条目：bgm.tv/subject/{subject_id}")
        lines.extend(notes)
        return await self._notice(
            umo,
            eyebrow="SUBSCRIBE",
            title="订阅已就绪" if ok else "订阅已建立（源暂时不可用）",
            subtitle=f"每 {conf.rss_interval_minutes} 分钟检查一次",
            lines=lines,
            cover=cover,
            link=url,
            stamp="FEED" if ok else "ALERT",
        )

    async def _silence_history(self, sub: Subscription) -> int:
        """把订阅当前已有的条目一次性标记为已读，返回条数。"""

        deps = self._deps
        try:
            items = await deps.hub.rss.poll(sub.url, limit=200, ttl=0)
        except Exception as error:  # noqa: BLE001 - 静默失败不影响订阅建立
            deps.activity.warn("rss", f"{sub.name} 首次入库拉取失败：{error}")
            return 0
        uids = [item.uid for item in items if sub.matches(item.title)]
        if uids:
            await deps.store.mark_seen(uids, umo=sub.umo)
        return len(uids)

    # ------------------------------------------------------------------
    # /unsub /unsub_all
    # ------------------------------------------------------------------
    async def remove(self, umo: str, name: str) -> Reply:
        name = name.strip()
        if not name:
            return Reply.plain("用法：/unsub <名称>，名称用 /sub_list 查。")
        sub = await self._find(umo, name)
        if sub is None:
            return Reply.plain(f"这个会话里没有叫「{name}」的订阅。")
        await self._deps.store.delete_subscription(sub.id)
        self._deps.activity.info("rss", f"退订 {sub.name}")
        return Reply.plain(f"已退订「{sub.name}」。")

    async def clear(self, umo: str) -> Reply:
        removed = await self._deps.store.delete_subscriptions(umo)
        if not removed:
            return Reply.plain("这个会话本来就没有订阅。")
        return Reply.plain(f"已清空这个会话的 {removed} 条订阅。")

    # ------------------------------------------------------------------
    # /sub_list /sub_state /sub_status
    # ------------------------------------------------------------------
    async def listing(self, umo: str) -> Reply:
        deps = self._deps
        subs = await deps.store.list_subscriptions(umo)
        if not subs:
            return Reply.plain("还没有订阅。试试 /sub 迷宫饭 —— 只给名字我会自己找源。")
        lines = []
        for index, sub in enumerate(subs, start=1):
            flag = "启用" if sub.enabled else "暂停"
            checked = _stamp(sub.last_checked)
            tail = f"上次检查 {checked}" if checked else "尚未检查"
            if sub.error:
                tail = f"异常：{sub.error[:60]}"
            lines.append(f"{index}. {sub.name}（{flag}）· {tail}")
            lines.append(f"    {sub.url}")
        target = await deps.store.get_pref(umo, PREF_TARGET)
        if target:
            lines.append(f"推送目标已改为：{target}")
        return await self._notice(
            umo,
            eyebrow="SUBSCRIPTIONS",
            title=f"订阅清单（{len(subs)}/{MAX_SUBSCRIPTIONS_PER_SESSION}）",
            subtitle="/sub_state <名称> 看单条详情",
            lines=lines,
        )

    async def state(self, umo: str, name: str) -> Reply:
        sub = await self._find(umo, name.strip())
        if sub is None:
            return Reply.plain(f"没找到「{name.strip()}」这条订阅。")
        lines = [
            f"地址：{sub.url}",
            f"状态：{'启用' if sub.enabled else '暂停'}",
            f"上次检查：{_stamp(sub.last_checked) or '尚未检查'}",
            f"最近条目：{sub.last_item or '—'}",
            f"创建于：{_stamp(sub.created_at) or '—'}",
        ]
        if sub.keywords:
            lines.append("关键词：" + "、".join(sub.keywords))
        if sub.excludes:
            lines.append("排除词：" + "、".join(sub.excludes))
        if sub.error:
            lines.append(f"最近错误：{sub.error}")
        return await self._notice(
            umo,
            eyebrow="SUBSCRIPTION",
            title=sub.name,
            subtitle="订阅详情",
            lines=lines,
            link=sub.url,
        )

    async def status(self, umo: str, snapshot: Mapping[str, Any] | None = None) -> Reply:
        """轮询任务总览：给 「/sub_status」 用。"""

        deps = self._deps
        conf = deps.conf
        subs = await deps.store.list_subscriptions(umo)
        enabled = sum(1 for sub in subs if sub.enabled)
        broken = [sub.name for sub in subs if sub.error]
        lines = [
            f"轮询总开关：{'开' if conf.rss_enabled else '关'}",
            f"轮询间隔：{conf.rss_interval_minutes} 分钟",
            f"本会话订阅：{enabled} 启用 / {len(subs)} 总数",
            f"单源单轮上限：{conf.rss_max_items_per_poll} 条",
            f"去重记录保留：{conf.rss_history_days} 天",
        ]
        if snapshot:
            for label, key in (
                ("调度器", "running"),
                ("下次轮询", "next_rss"),
                ("下次播报", "next_push"),
                ("已轮询轮次", "polls"),
            ):
                value = snapshot.get(key)
                if value not in (None, ""):
                    lines.append(f"{label}：{_readable(value)}")
        if broken:
            lines.append("异常源：" + "、".join(broken[:6]))
        daily = await deps.store.get_pref(umo, PREF_DAILY)
        lines.append(f"本会话每日播报：{'开' if daily == '1' else '关'}")
        return await self._notice(
            umo,
            eyebrow="STATUS",
            title="订阅与推送状态",
            subtitle="番剧中枢运行情况",
            lines=lines,
        )

    # ------------------------------------------------------------------
    # /sub_test
    # ------------------------------------------------------------------
    async def test(self, umo: str, token: str) -> Reply:
        """试拉一次：参数既可以是订阅名，也可以是任意地址。"""

        deps = self._deps
        conf = deps.conf
        token = token.strip()
        if not token:
            return Reply.plain("用法：/sub_test <名称|RSS地址>")
        sub = await self._find(umo, token)
        url = (
            sub.url
            if sub is not None
            else normalize_feed_url(token, rsshub_base=conf.rsshub_base, mikan_base=conf.mikan_base)
        )
        if not url:
            return Reply.plain("这个参数既不是订阅名，也不像一个地址。")
        started = time.monotonic()
        try:
            items = await deps.hub.rss.poll(url, limit=conf.rss_max_items_per_poll, ttl=0)
        except Exception as error:  # noqa: BLE001
            return Reply.plain(f"拉取失败：{error}")
        elapsed = time.monotonic() - started
        if not items:
            return Reply.plain(
                f"能连上（{elapsed:.1f}s），但没解析出条目。确认这是 RSS/Atom 地址。"
            )
        theme, _ = await style_for(deps, umo)
        html = build_feed_card(
            theme,
            sub.name if sub is not None else "订阅测试",
            items,
            width=conf.card_width,
            subtitle=f"{url}｜耗时 {elapsed:.1f}s（测试拉取，不计入去重）",
        )
        plain = "\n".join([f"测试通过，{len(items)} 条：", *(f"· {item.title}" for item in items)])
        return Reply(
            text=plain,
            card=make_card(
                html,
                plain=plain,
                title=sub.name if sub is not None else "订阅测试",
                eyebrow="FEED TEST",
                subtitle=url,
                theme=theme,
                width=conf.card_width,
            ),
        )

    # ------------------------------------------------------------------
    # 批量启停
    # ------------------------------------------------------------------
    async def set_enabled(self, umo: str, enabled: bool) -> Reply:
        changed = await self._deps.store.set_subscriptions_enabled(umo, enabled)
        if not changed:
            return Reply.plain("没有需要改动的订阅。")
        word = "恢复" if enabled else "暂停"
        return Reply.plain(f"已{word}这个会话的 {changed} 条订阅（订阅记录还留着）。")

    # ------------------------------------------------------------------
    # 导入导出
    # ------------------------------------------------------------------
    async def export(self, umo: str) -> Reply:
        payload = await self._deps.store.export_all(umo)
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return Reply(text=text, notes=(RAW_NOTE,))

    async def import_payload(self, umo: str, raw: str) -> Reply:
        raw = raw.strip()
        if not raw:
            return Reply.plain("用法：/sub_import <JSON>，JSON 用 /sub_export 导出。")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as error:
            return Reply.plain(f"这段 JSON 解析不了：{error}")
        if not isinstance(payload, dict):
            return Reply.plain("JSON 顶层应该是一个对象。")
        counters = await self._deps.store.import_all(payload, umo=umo)
        return Reply.plain(
            "导入完成：追番 {watchlist} 条、订阅 {subscriptions} 条、偏好 {prefs} 条。".format(
                **counters
            )
        )

    # ------------------------------------------------------------------
    # 会话偏好
    # ------------------------------------------------------------------
    async def profile(self, umo: str, action: str, value: str) -> Reply:
        """「/sub_profile get|set <主题|渲染器>」。"""

        deps = self._deps
        action = action.strip().lower()
        value = value.strip().lower()
        themes = theme_keys()
        if action in {"", "get", "查看"}:
            theme, renderer = await style_for(deps, umo)
            return Reply.plain(
                f"当前会话卡片主题：{theme}\n渲染方式：{renderer}\n"
                f"可选主题：{'、'.join(themes)}\n可选渲染：{'、'.join(RENDERERS)}"
            )
        if action not in {"set", "设置"}:
            return Reply.plain("用法：/sub_profile get 或 /sub_profile set <主题|渲染器>")
        if not value:
            return Reply.plain("要设成什么？主题：" + "、".join(themes))
        if value in themes:
            await deps.store.set_pref(umo, PREF_THEME, value)
            return Reply.plain(f"这个会话的卡片主题改成 {value} 了。")
        if value in RENDERERS:
            await deps.store.set_pref(umo, PREF_RENDERER, value)
            return Reply.plain(f"这个会话的渲染方式改成 {value} 了。")
        if value in {"reset", "默认", "重置"}:
            await deps.store.set_pref(umo, PREF_THEME, "")
            await deps.store.set_pref(umo, PREF_RENDERER, "")
            return Reply.plain("已恢复跟随全局配置。")
        return Reply.plain(
            f"不认识「{value}」。主题：{'、'.join(themes)}；渲染：{'、'.join(RENDERERS)}"
        )

    async def session(self, umo: str, action: str, value: str) -> Reply:
        """「/sub_session get|set [会话ID]」：把本会话的推送改投到别处。

        典型用法是在管理群里配好订阅，但让通知发到另一个群。
        """

        deps = self._deps
        action = action.strip().lower()
        value = value.strip()
        if action in {"", "get", "查看"}:
            target = await deps.store.get_pref(umo, PREF_TARGET)
            return Reply.plain(
                f"当前会话 ID：{umo}\n推送目标：{target or '（就发在本会话）'}\n"
                "改投别处：/sub_session set <会话ID>；恢复：/sub_session set 本会话"
            )
        if action not in {"set", "设置"}:
            return Reply.plain("用法：/sub_session get 或 /sub_session set <会话ID>")
        if not value or value in {"本会话", "self", "reset", "默认"}:
            await deps.store.set_pref(umo, PREF_TARGET, "")
            return Reply.plain("推送目标已恢复为本会话。")
        await deps.store.set_pref(umo, PREF_TARGET, value)
        return Reply.plain(f"这个会话的订阅通知以后会发到：{value}")

    async def daily(self, umo: str, switch: str) -> Reply:
        """「/日历订阅 开|关」：让本会话加入每日新番播报。"""

        deps = self._deps
        wanted = parse_switch(switch)
        if wanted is None:
            current = await deps.store.get_pref(umo, PREF_DAILY)
            return Reply.plain(
                f"本会话每日播报：{'开' if current == '1' else '关'}\n用 /日历订阅 开 或 /日历订阅 关 切换。"
            )
        await deps.store.set_pref(umo, PREF_DAILY, "1" if wanted else "")
        times = "、".join(deps.conf.push_times) or "未配置"
        if wanted:
            return Reply.plain(f"好，以后每天 {times} 在这里播报当日新番。")
        return Reply.plain("已关闭本会话的每日新番播报。")

    # ------------------------------------------------------------------
    # /rsshelp
    # ------------------------------------------------------------------
    async def help(self, umo: str) -> Reply:
        conf = self._deps.conf
        lines = [
            "完整地址：/sub 迷宫饭 https://mikanani.me/RSS/Bangumi?bangumiId=3141",
            "Mikan 单番：/sub 迷宫饭 mikan:3141",
            "Mikan 搜索：/sub 迷宫饭 mikan:迷宫饭",
            f"RSSHub 路由：/sub 每日放送 rsshub:/bangumi/tv/calendar/today（当前实例 {conf.rsshub_base}）",
            "动漫花园：/sub 迷宫饭 dmhy:迷宫饭 简体",
            "只给名字：/sub 迷宫饭 —— 自动匹配 Mikan 番剧 ID",
            "—",
            "/sub_test <名称|地址> 试拉一次，不影响去重记录",
            "/sub_list 看清单，/sub_state <名称> 看单条",
            "/sub_stop 暂停全部，/activate_subs 恢复全部",
            "/sub_session set <会话ID> 把通知改投到别的群",
            "/sub_profile set <主题> 换这个会话的卡片主题",
            "/sub_export 与 /sub_import 搬家备份",
        ]
        return await self._notice(
            umo,
            eyebrow="RSS HELP",
            title="订阅地址怎么写",
            subtitle="支持简写，不用背域名",
            lines=lines,
            stamp="HELP",
        )

    # ------------------------------------------------------------------
    # 轮询
    # ------------------------------------------------------------------
    async def poll(self, *, umo: str = "", force: bool = False) -> list[tuple[str, Notification]]:
        """轮询订阅，返回 「[(目标会话, 通知)]」，实际投递交给 Notifier。

        服务层不碰消息发送，这样 WebUI 的「立即检查」按钮和定时任务可以走同一条路径。
        """

        deps = self._deps
        conf = deps.conf
        subs = await deps.store.list_subscriptions(umo, enabled_only=True)
        if not subs:
            return []
        self._polls += 1
        gate = asyncio.Semaphore(max(1, conf.max_concurrency))

        async def one(sub: Subscription) -> tuple[str, Notification] | None:
            async with gate:
                return await self._poll_one(sub, force=force)

        outcomes = await asyncio.gather(*(one(sub) for sub in subs), return_exceptions=True)
        results: list[tuple[str, Notification]] = []
        for sub, outcome in zip(subs, outcomes, strict=False):
            if isinstance(outcome, Exception):
                deps.activity.error("rss", f"{sub.name} 轮询异常：{outcome}")
                continue
            if outcome is not None:
                results.append(outcome)
        self._pushed += len(results)
        return results

    async def _poll_one(self, sub: Subscription, *, force: bool) -> tuple[str, Notification] | None:
        deps = self._deps
        conf = deps.conf
        ttl = 0.0 if force else max(30.0, conf.rss_interval_minutes * 30.0)
        try:
            items = await deps.hub.rss.poll(sub.url, limit=120, ttl=ttl)
        except Exception as error:  # noqa: BLE001
            return await self._on_failure(sub, str(error)[:200])

        recovered = bool(sub.error)
        matched = [item for item in items if sub.matches(item.title)]
        uids = [item.uid for item in matched]
        fresh_uids = set(await deps.store.filter_unseen(uids, umo=sub.umo))
        fresh = [item for item in matched if item.uid in fresh_uids]
        first_run = sub.last_checked <= 0
        if uids:
            await deps.store.mark_seen(uids, umo=sub.umo)
        await deps.store.set_subscription_state(
            sub.id,
            last_checked=time.time(),
            last_item=matched[0].title if matched else sub.last_item,
            error="",
        )
        if recovered:
            deps.activity.info("rss", f"{sub.name} 已恢复正常")
        if not fresh or (first_run and conf.rss_first_poll_silent):
            return None

        limited = fresh[: max(1, conf.rss_max_items_per_poll)]
        hidden = len(fresh) - len(limited)
        lines = [item.title for item in limited]
        if hidden:
            lines.append(f"另有 {hidden} 条本轮没展示，避免刷屏")
        cover = ""
        if sub.subject_id:
            subject = await deps.hub.bangumi.subject(sub.subject_id)
            if subject is not None:
                cover = subject.image
        notification = Notification(
            kind="rss_update",
            title=sub.name,
            subtitle=f"{len(fresh)} 条新更新",
            lines=tuple(lines),
            link=limited[0].link,
            cover=cover,
            payload={"feed_items": limited, "subscription": sub.name, "hidden": hidden},
        )
        deps.activity.info("rss", f"{sub.name} 有 {len(fresh)} 条新更新")
        return await self.target_for(sub.umo), notification

    async def _on_failure(self, sub: Subscription, detail: str) -> tuple[str, Notification] | None:
        """记录失败。只在「从正常变为失败」的那一次发告警，避免每轮刷屏。"""

        deps = self._deps
        await deps.store.set_subscription_state(sub.id, last_checked=time.time(), error=detail)
        deps.activity.warn("rss", f"{sub.name} 拉取失败：{detail}")
        if sub.error:
            return None
        notification = Notification(
            kind="rss_error",
            title=f"订阅「{sub.name}」拉不动了",
            subtitle="下一轮会自动重试，不用管",
            lines=(f"地址：{sub.url}", f"原因：{detail}"),
            link=sub.url,
        )
        return await self.target_for(sub.umo), notification

    async def target_for(self, umo: str) -> str:
        """订阅通知实际要发到哪个会话（可被 「/sub_session」 改投）。"""

        try:
            target = await self._deps.store.get_pref(umo, PREF_TARGET)
        except Exception:  # noqa: BLE001
            target = ""
        return target or umo

    # ------------------------------------------------------------------
    # 给追番流程用的源推荐
    # ------------------------------------------------------------------
    def suggest(self, match: MatchResult) -> tuple[tuple[str, str], ...]:
        """一部番可用的订阅源候选，「/追番」 之后顺手推荐给用户。"""

        conf = self._deps.conf
        title = match.title
        options: list[tuple[str, str]] = []
        if match.mikan_rss:
            label = "Mikan 单番" if match.data_item and match.data_item.mikan_id else "Mikan 搜索"
            options.append((label, match.mikan_rss))
        if title:
            options.append(("动漫花园", dmhy_feed(title)))
            if match.subject is not None and match.subject.id:
                options.append(
                    (
                        "RSSHub 条目",
                        rsshub_feed(conf.rsshub_base, f"/bangumi/tv/subject/{match.subject.id}"),
                    )
                )
        return tuple(options)

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------
    async def _find(self, umo: str, name: str) -> Subscription | None:
        if not name:
            return None
        found = await self._deps.store.find_subscription(umo, name)
        if found is not None:
            return found
        if name.isdigit():
            index = int(name)
            subs = await self._deps.store.list_subscriptions(umo)
            if 1 <= index <= len(subs):
                return subs[index - 1]
        return None

    async def _notice(
        self,
        umo: str,
        *,
        eyebrow: str,
        title: str,
        lines: Sequence[str],
        subtitle: str = "",
        cover: str = "",
        link: str = "",
        stamp: str = "NOTICE",
    ) -> Reply:
        """把一组文本行包成通知卡；订阅相关的回复大多是这种形态。"""

        deps = self._deps
        conf = deps.conf
        theme, _ = await style_for(deps, umo)
        cover_data = await cover_uri(deps, cover)
        html = build_notice_card(
            theme,
            eyebrow=eyebrow,
            title=title,
            lines=list(lines),
            subtitle=subtitle,
            cover=cover_data,
            link=link,
            width=conf.card_width,
            stamp=stamp,
        )
        plain = "\n".join([title, *(line for line in lines if str(line).strip())])
        return Reply(
            text=plain,
            card=make_card(
                html,
                plain=plain,
                title=title,
                eyebrow=eyebrow,
                subtitle=subtitle,
                theme=theme,
                width=conf.card_width,
            ),
        )

    def stats(self) -> dict[str, int]:
        return {"polls": self._polls, "pushed": self._pushed}


def _stamp(moment: float) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(moment)) if moment else ""


def _readable(value: Any) -> str:
    if isinstance(value, bool):
        return "运行中" if value else "已停止"
    if isinstance(value, float) and value > 1_000_000_000:
        return _stamp(value)
    return str(value)


__all__ = ["RAW_NOTE", "SubscriptionService", "split_target"]
