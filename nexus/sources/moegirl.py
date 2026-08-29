"""萌娘百科（zh.moegirl.org.cn）适配器。

用途有两个：给「查番 / 抽番」补一段人类写的介绍，以及作为 LLM 工具让模型能查
角色与作品词条。走官方的 「api.php?action=opensearch」 拿候选，再抓正文首段。

正文里 navbox / 提示框 / 目录 / 信息框全是噪声，必须剔掉，否则摘要会变成
「本条目介绍的是……消歧义……」这种废话。

Copyright (C) 2026 Whereis-Alice and AstrBot Plugin Authors.
Licensed under the GNU Affero General Public License v3.0 or later.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from ..constants import MOEGIRL_API, MOEGIRL_PAGE
from ..http import FetchError, HttpClient
from ..models import MoegirlHit

#: 正文里要整块删掉的元素选择器。
_NOISE_SELECTORS = (
    "table",
    "div.navbox",
    "div.notice",
    "div.dablink",
    "div.hatnote",
    "div.infobox",
    "div.toc",
    "#toc",
    "div.reference",
    "sup.reference",
    "style",
    "script",
    "div.thumb",
    "figure",
    "div.mw-editsection",
)

_BRACKET_NOISE = re.compile(r"\[\d+\]|\[编辑\]|\[來源請求\]|\[来源请求\]")


def clean_extract(html: str, *, limit: int = 420) -> str:
    """从词条 HTML 里抽出干净的开头段落。"""

    try:
        from bs4 import BeautifulSoup
    except ImportError:  # pragma: no cover
        return ""
    soup = BeautifulSoup(html or "", "html.parser")
    root = soup.select_one("div.mw-parser-output") or soup
    for selector in _NOISE_SELECTORS:
        for node in root.select(selector):
            node.decompose()
    paragraphs: list[str] = []
    for node in root.find_all("p", recursive=True):
        text = _BRACKET_NOISE.sub("", node.get_text(" ", strip=True)).strip()
        if len(text) < 12:
            continue
        paragraphs.append(text)
        if sum(len(part) for part in paragraphs) >= limit:
            break
    body = " ".join(paragraphs)
    body = re.sub(r"\s{2,}", " ", body).strip()
    if len(body) > limit:
        body = body[: limit - 1].rstrip() + "…"
    return body


class MoegirlSource:
    """萌娘百科搜索 + 摘要。"""

    key = "moegirl"

    def __init__(self, http: HttpClient) -> None:
        self._http = http

    async def search(self, keyword: str, *, limit: int = 3) -> list[MoegirlHit]:
        token = keyword.strip()
        if not token:
            return []
        try:
            raw = await self._http.fetch_json(
                MOEGIRL_API,
                params={
                    "action": "opensearch",
                    "namespace": "*",
                    "search": token,
                    "limit": max(1, min(10, limit)),
                    "format": "json",
                },
                cache_key=f"moe:search:{token}:{limit}",
                ttl=3600,
            )
        except FetchError:
            return []
        if not isinstance(raw, list) or len(raw) < 4:
            return []
        titles = [str(value) for value in raw[1] or ()]
        descriptions = [str(value) for value in raw[2] or ()]
        urls = [str(value) for value in raw[3] or ()]
        hits: list[MoegirlHit] = []
        for index, title in enumerate(titles[:limit]):
            hits.append(
                MoegirlHit(
                    title=title,
                    url=urls[index]
                    if index < len(urls)
                    else MOEGIRL_PAGE.format(title=quote(title)),
                    summary=descriptions[index] if index < len(descriptions) else "",
                )
            )
        return hits

    async def extract(self, title: str, *, limit: int = 420) -> str:
        """抓词条正文摘要。失败返回空串，调用方自行决定要不要提示。"""

        if not title.strip():
            return ""
        try:
            html = await self._http.fetch_text(
                MOEGIRL_PAGE.format(title=quote(title.strip(), safe="")),
                cache_key=f"moe:page:{title}",
                ttl=12 * 3600,
            )
        except FetchError:
            return ""
        return clean_extract(html, limit=limit)

    async def lookup(self, keyword: str) -> MoegirlHit | None:
        """搜索 + 补正文，返回最相关的一条。"""

        hits = await self.search(keyword, limit=3)
        if not hits:
            return None
        top = hits[0]
        summary = await self.extract(top.title)
        if summary:
            return MoegirlHit(title=top.title, url=top.url, summary=summary)
        return top

    def stats(self) -> dict[str, Any]:
        return {"ready": True}
