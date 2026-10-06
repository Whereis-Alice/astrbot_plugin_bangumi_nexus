"""链接只在消息正文展示；图片保留信息，不绘制无法点击的网址。"""

from __future__ import annotations

import re
from collections.abc import Sequence
from urllib.parse import urlsplit

Link = tuple[str, str]
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
RELATED_LABELS = frozenset(
    {
        "bangumi",
        "bangumi 条目",
        "官网",
        "萌娘百科",
        "mikan",
        "動漫花園",
        "动漫花园",
        "资源详情",
        "订阅源",
    }
)


def valid_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return parsed.scheme in {"http", "https"} and bool(parsed.hostname)
    except ValueError:
        return False


def link_label(url: str) -> str:
    """已知条目站标明用途，其余事件链接称为资源详情，不冒充播放地址。"""
    if not valid_url(url):
        return "资源详情"
    host = (urlsplit(url).hostname or "").lower()
    if host in {"bgm.tv", "bangumi.tv", "chii.in"}:
        return "Bangumi 条目"
    return "资源详情"


def link_caption(links: Sequence[Link], *, limit: int = 5) -> str:
    """观看入口优先展示；条目、官网与资源页另列，按 URL 去重。"""
    watch: list[str] = []
    related: list[str] = []
    seen: set[str] = set()
    for name, url in links:
        name, url = str(name).strip(), str(url).strip()
        if not name or not valid_url(url) or url in seen:
            continue
        seen.add(url)
        target = related if name.lower() in RELATED_LABELS else watch
        if len(target) < limit:
            target.append(f"{name} {url}")
    return "\n\n".join(
        title + "\n" + "\n".join(rows)
        for title, rows in (("▶ 在线观看", watch), ("相关链接", related))
        if rows
    )


def without_links(text: str) -> str:
    """供图片回退使用，保留普通文字并去掉网址及其孤立标题。"""
    return "\n".join(
        line
        for line in text.splitlines()
        if not URL_RE.search(line)
        and line.strip() not in {"▶ 在线观看", "相关链接", "在线观看", "链接"}
    ).strip()


def append_caption(text: str, caption: str) -> str:
    return "\n\n".join(part for part in (text.strip(), caption.strip()) if part)
