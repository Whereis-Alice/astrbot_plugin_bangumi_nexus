"""单番通知的资料补全：有边界的网络请求、纯展示裁剪，不修改事件和追番进度。"""

from __future__ import annotations

import asyncio
import html
import re

from ..links import URL_RE
from ..models import CharacterProfile, NoticeDetails, Notification
from ..sources.bangumi import staff_from_infobox
from .base import Deps

ANIME_EVENTS = frozenset(
    {
        "new_episode",
        "download_start",
        "download_complete",
        "rename_complete",
        "series_completed",
        "episode_missing",
        "idle_warning",
    }
)


def subject_id_of(notification: Notification) -> int:
    try:
        return max(0, int(notification.payload.get("subject_id") or 0))
    except (TypeError, ValueError):
        return 0


def can_enrich(notification: Notification) -> bool:
    # 未关联 Bangumi 的资讯 RSS / 测试 / 批量摘要不应被猜成某部番。
    return subject_id_of(notification) > 0 or notification.kind in ANIME_EVENTS


def snippet(value: str, limit: int, *, first_paragraph: bool = False) -> str:
    """去网址、HTML、BBCode；人物只摘首段，避免把整份生平塞进通知。"""
    text = html.unescape(str(value or ""))
    text = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", "", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>|\[/?[a-zA-Z][^\]]*\]", "", text)
    text = URL_RE.sub("", text).strip()
    if first_paragraph:
        text = next((line.strip() for line in text.splitlines() if line.strip()), "")
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


async def load_notice_details(deps: Deps, notification: Notification) -> NoticeDetails:
    """通知只查 Bangumi；关闭跨站或文字链接不影响基础资料。"""
    if not can_enrich(notification):
        return NoticeDetails()
    try:
        subject = await asyncio.wait_for(
            deps.matcher.notification_subject(subject_id_of(notification), notification.title),
            timeout=3,
        )
    except Exception as error:  # noqa: BLE001 - 辅助资料不可阻断主通知
        deps.activity.warn("notify", f"通知作品资料未完成：{type(error).__name__}")
        return NoticeDetails()
    if subject is None:
        return NoticeDetails()
    staff, _ = staff_from_infobox(subject.infobox)
    core_roles = {"原作", "导演", "动画制作", "系列构成", "人物设定", "音乐"}
    characters = ()
    limit = max(0, min(6, deps.conf.notify_character_limit))
    if limit:
        try:
            characters = await asyncio.wait_for(
                deps.hub.bangumi.main_characters(subject.id, limit=limit),
                timeout=4.5,
            )
        except Exception as error:  # noqa: BLE001 - 保留已经拿到的简介和制作资料
            deps.activity.warn("notify", f"通知主角资料未完成：{type(error).__name__}")
    return NoticeDetails(
        summary=snippet(subject.summary, 220),
        staff=tuple((role, snippet(value, 60)) for role, value in staff if role in core_roles),
        characters=tuple(
            CharacterProfile(
                c.id,
                snippet(c.name, 32),
                snippet(c.summary, 90, first_paragraph=True),
                snippet(c.voice, 40),
            )
            for c in characters[:limit]
        ),
        cover=subject.image,
    )
