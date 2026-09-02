"""長門番堂（yuc.wiki）季度新番表适配器。

这是所有数据源里信息密度最高的一个：每部番都给出中文名 / 日文名、类型、题材标签、
完整 staff（原作 / 导演 / 编剧 / 人设 / 音乐 / 制作公司）、主要声优表、首播时间与官网。
Bangumi 有评分但没有这些制作信息，两者合起来才是一张「够看」的番剧卡。

页面是手写 HTML，没有 API，因此这里的解析器刻意写得宽容：
* 不依赖 「<hr>」 分段（页面改版时最容易变的就是它），而是按 class 前缀就地识别；
* 每日放送表靠「文档顺序」把标题归到最近出现过的那个星期几表头；
* 任何一块解析失败都只丢那一块，不会让整季表变成空。

Copyright (C) 2026 Whereis-Alice and AstrBot Plugin Authors.
Licensed under the GNU Affero General Public License v3.0 or later.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from ..constants import WEEKDAY_CN, YUC_SEASON_URL
from ..http import FetchError, HttpClient, browser_headers
from ..models import SeasonEntry
from ..titles import MATCH_THRESHOLD, alias_keys, best_match, season_code

YUC_SITE = YUC_SEASON_URL.split("/{", 1)[0].rstrip("/")

_WEEKDAY_RE = re.compile(r"周([一二三四五六日天])")
_WEEKDAY_INDEX = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7}


def weekday_from_text(text: str) -> int:
    """从 「7/5周日深夜」 「周一 (月)」 里读出 1~7；读不到返回 0。"""

    match = _WEEKDAY_RE.search(str(text or ""))
    return _WEEKDAY_INDEX.get(match.group(1), 0) if match else 0


def _lines(tag: Any) -> list[str]:
    """把一个单元格里用 「<br>」 分隔的多行拆开。"""

    if tag is None:
        return []
    raw = tag.get_text("\n", strip=True)
    return [line.strip() for line in raw.split("\n") if line.strip()]


def _pairs(lines: list[str]) -> tuple[tuple[str, str], ...]:
    """「原作：某某」 → 「("原作", "某某")」；没有冒号的整行当值。"""

    result: list[tuple[str, str]] = []
    for line in lines:
        parts = re.split(r"[：:]", line, maxsplit=1)
        if len(parts) == 2 and parts[0].strip():
            result.append((parts[0].strip(), parts[1].strip()))
        elif line:
            result.append(("", line))
    return tuple(result)


def _cast_pairs(lines: list[str]) -> tuple[tuple[str, str], ...]:
    """声优表一行是 「角色名　声优名」（全角空格分隔）。"""

    result: list[tuple[str, str]] = []
    for line in lines:
        parts = re.split(r"[\u3000\t]+|\s{2,}", line, maxsplit=1)
        if len(parts) == 2:
            result.append((parts[0].strip(), parts[1].strip()))
        elif line:
            result.append(("", line.strip()))
    return tuple(result)


def _class_of(tag: Any, prefix: str) -> str:
    for name in tag.get("class") or ():
        if str(name).startswith(prefix):
            return str(name)
    return ""


def _find_by_prefix(scope: Any, prefix: str) -> Any:
    """找第一个 class 以 「prefix」 开头的 「td」/「p」（页面用 _r / _r1 / _r2 做变体）。"""

    return scope.find(lambda tag: tag.name in {"td", "p", "div"} and bool(_class_of(tag, prefix)))


def _find_all_by_prefix(scope: Any, prefix: str) -> list[Any]:
    return scope.find_all(
        lambda tag: tag.name in {"td", "p", "div"} and bool(_class_of(tag, prefix))
    )


@dataclass
class SeasonTable:
    """一整季的解析结果。"""

    code: str
    entries: tuple[SeasonEntry, ...] = ()
    days: dict[int, tuple[str, ...]] = field(default_factory=dict)
    future: tuple[str, ...] = ()
    covers: dict[str, str] = field(default_factory=dict)
    _index: dict[str, SeasonEntry] = field(default_factory=dict, repr=False)

    def build_index(self) -> SeasonTable:
        self._index = {}
        for entry in self.entries:
            for key in alias_keys(entry.title_cn, entry.title_jp):
                self._index.setdefault(key, entry)
        return self

    def find(self, *titles: str, threshold: float = MATCH_THRESHOLD) -> SeasonEntry | None:
        for title in titles:
            if not title:
                continue
            for key in alias_keys(title):
                hit = self._index.get(key)
                if hit is not None:
                    return hit
        for title in titles:
            if not title:
                continue
            hit, _ = best_match(
                title,
                self.entries,
                key=lambda entry: (entry.title_cn, entry.title_jp),
                threshold=threshold,
            )
            if hit:
                return hit
        return None

    def weekday_of(self, title: str) -> int:
        """先按每日放送表反查，再退回 broadcast 文本。"""

        keys = alias_keys(title)
        for weekday, names in self.days.items():
            for name in names:
                if alias_keys(name) & keys:
                    return weekday
        entry = self.find(title)
        return weekday_from_text(entry.broadcast) if entry else 0

    def by_weekday(self) -> dict[int, tuple[SeasonEntry, ...]]:
        """把整季按星期几分组，供「季度总览」卡片使用。"""

        grouped: dict[int, list[SeasonEntry]] = {day: [] for day in range(1, 8)}
        unknown: list[SeasonEntry] = []
        for entry in self.entries:
            weekday = self.weekday_of(entry.display_name)
            if 1 <= weekday <= 7:
                grouped[weekday].append(entry)
            else:
                unknown.append(entry)
        # 日历表里出现、但没有详情表的番（页面更新滞后时常有）也要占位，
        # 否则「季度总览」卡会莫名少几部，用户只会以为插件漏抓了。
        for weekday, names in self.days.items():
            if not 1 <= weekday <= 7:
                continue
            for name in names:
                if self.find(name) is not None:
                    continue
                grouped[weekday].append(SeasonEntry(title_cn=name, cover=self.covers.get(name, "")))
        result = {day: tuple(items) for day, items in grouped.items()}
        if unknown:
            result[0] = tuple(unknown)
        return result

    @property
    def total(self) -> int:
        return len(self.entries)


class YucSource:
    """长门番堂季度表。整页 HTML 几百 KB，缓存 6 小时足够。"""

    key = "yuc"

    def __init__(self, http: HttpClient) -> None:
        self._http = http
        self._tables: dict[str, SeasonTable] = {}
        # 上一次证书出问题的时间戳。上游换证期间会短暂过期（实机撞过），
        # 记下来只为在提示里说清「这不是你的网络问题」。
        self._ssl_failed_at = 0.0

    async def season(self, code: str = "", *, force: bool = False) -> SeasonTable:
        target = re.sub(r"\D", "", str(code or "")) or season_code()
        if not force and target in self._tables:
            return self._tables[target]
        html = await self._fetch(target, force=force)
        table = parse_season(html, target)
        if table.total:
            self._tables[target] = table
        return table

    async def _fetch(self, target: str, *, force: bool) -> str:
        """抓一季的 HTML；证书过期时按配置决定是否降级重试。

        长门番堂 用 Let's Encrypt，上游续期出岔子时整站会短暂 「certificate has
        expired」。那种时候整个季度表会被打空，比「跳过一次证书校验」更糟 ——
        这里读的只是公开的番剧列表，没有凭据外泄风险，所以给一次降级重试的机会。
        """
        url = YUC_SEASON_URL.format(season=target)
        ttl = 0 if force else 6 * 3600
        headers = browser_headers("https://yuc.wiki/")
        try:
            return await self._http.fetch_text(
                url, cache_key=f"yuc:{target}", ttl=ttl, headers=headers
            )
        except FetchError as error:
            if not error.ssl_error:
                raise
            self._ssl_failed_at = time.time()
            return await self._http.fetch_text(
                url,
                cache_key=f"yuc:insecure:{target}",
                ttl=ttl,
                headers=headers,
                insecure=True,
                retries=1,
            )

    async def find(
        self, title: str, *, codes: tuple[str, ...] = ()
    ) -> tuple[SeasonEntry | None, str]:
        """跨若干季度找一部番，返回 「(条目, 季度代号)」。"""

        from ..titles import season_codes_around

        for code in codes or season_codes_around(span=1):
            try:
                table = await self.season(code)
            except Exception:  # noqa: BLE001 - 某一季拉不到就跳过，继续找其它季
                continue
            hit = table.find(title)
            if hit is not None:
                return hit, code
        return None, ""

    def cached_seasons(self) -> tuple[str, ...]:
        return tuple(sorted(self._tables))

    def stats(self) -> dict[str, Any]:
        return {
            "seasons": len(self._tables),
            "entries": sum(table.total for table in self._tables.values()),
            "ssl_degraded": bool(self._ssl_failed_at),
        }


def parse_season(html: str, code: str) -> SeasonTable:
    """解析一整页季度表。BeautifulSoup 缺失时返回空表而不是抛错。"""

    try:
        from bs4 import BeautifulSoup
    except ImportError:  # pragma: no cover - 依赖未装时优雅退化
        return SeasonTable(code=code).build_index()

    soup = BeautifulSoup(html or "", "html.parser")
    body = soup.find("div", class_="post-body") or soup
    days, future, covers = _parse_calendar(body)
    entries = _parse_details(body, covers)
    return SeasonTable(
        code=code,
        entries=entries,
        days=days,
        future=future,
        covers=covers,
    ).build_index()


def _parse_calendar(
    body: Any,
) -> tuple[dict[int, tuple[str, ...]], tuple[str, ...], dict[str, str]]:
    """按文档顺序把番名归到最近的星期几表头，同时收集封面。"""

    days: dict[int, list[str]] = {}
    future: list[str] = []
    covers: dict[str, str] = {}
    current = 0

    def register_cover(cell: Any, title: str) -> None:
        table = cell.find_parent("table")
        image = table.find("img") if table else None
        if image is None:
            return
        src = str(image.get("data-src") or image.get("src") or "").strip()
        if src and title:
            covers.setdefault(title, _absolute_cover(src))

    for cell in body.find_all("td"):
        classes = [str(name) for name in cell.get("class") or ()]
        if any(name.startswith("date2") for name in classes):
            weekday = weekday_from_text(cell.get_text(" ", strip=True))
            if weekday:
                current = weekday
                days.setdefault(current, [])
            continue
        if any(name.startswith("future_title") for name in classes):
            title = cell.get_text(" ", strip=True)
            if title:
                future.append(title)
                register_cover(cell, title)
            continue
        if any(name.startswith("date_title") for name in classes):
            title = cell.get_text(" ", strip=True)
            if not title:
                continue
            register_cover(cell, title)
            if current:
                days.setdefault(current, []).append(title)
    normalised = {weekday: tuple(dict.fromkeys(titles)) for weekday, titles in sorted(days.items())}
    return normalised, tuple(dict.fromkeys(future)), covers


def _parse_details(body: Any, covers: dict[str, str]) -> tuple[SeasonEntry, ...]:
    """详情表：每个含 「title_main*」 单元格的 「<table>」 就是一部番。"""

    entries: list[SeasonEntry] = []
    seen: set[str] = set()
    for main in _find_all_by_prefix(body, "title_main"):
        table = main.find_parent("table")
        if table is None:
            continue
        # 中日文名通常嵌在 「title_main」 单元格内，个别季度会平铺成同表的兄弟单元格，
        # 因此先就近找、再退回整表，最后才拿整格文本兜底。
        title_cn = (
            _text_of(_find_by_prefix(main, "title_cn"))
            or _text_of(_find_by_prefix(table, "title_cn"))
            or main.get_text(" ", strip=True)
        )
        title_jp = _text_of(_find_by_prefix(main, "title_jp")) or _text_of(
            _find_by_prefix(table, "title_jp")
        )
        if not title_cn and not title_jp:
            continue
        marker = f"{title_cn}|{title_jp}"
        if marker in seen:
            continue
        seen.add(marker)

        category = ""
        for prefix in ("type_a", "type_b", "type_c"):
            cell = _find_by_prefix(table, prefix)
            if cell is not None:
                category = cell.get_text(" ", strip=True)
                break
        genre_cell = _find_by_prefix(table, "type_tag")
        genres = tuple(
            part.strip()
            for part in re.split(
                r"[/、,，]", genre_cell.get_text(" ", strip=True) if genre_cell else ""
            )
            if part.strip()
        )
        staff_lines: list[str] = []
        for cell in _find_all_by_prefix(table, "staff_r"):
            staff_lines.extend(_lines(cell))
        cast_lines: list[str] = []
        for cell in _find_all_by_prefix(table, "cast_r"):
            cast_lines.extend(_lines(cell))

        link_cell = _find_by_prefix(table, "link_a")
        official = ""
        if link_cell is not None:
            anchor = link_cell.find("a", href=True)
            if anchor is not None:
                official = str(anchor["href"]).strip()

        # 首播时间同样先在 「link_a」 格内找、再退回整表。
        # 这里刻意不拿单元格文本兜底：上游布局里官网链接和放送信息共格，
        # 兜底会把 「官网」 这种链接文案当成首播时间发给用户。
        broadcast = ""
        for scope in (link_cell, table):
            if scope is None:
                continue
            parts = [
                _text_of(_find_by_prefix(scope, "broadcast_r")),
                _text_of(_find_by_prefix(scope, "broadcast_ex")),
            ]
            broadcast = " ".join(part for part in parts if part)
            if broadcast:
                break

        cover = covers.get(title_cn) or covers.get(title_jp) or ""
        entries.append(
            SeasonEntry(
                title_cn=title_cn,
                title_jp=title_jp,
                category=category,
                genres=genres[:8],
                staff=_pairs(staff_lines)[:10],
                cast=_cast_pairs(cast_lines)[:12],
                broadcast=broadcast.strip(),
                official_site=official,
                cover=cover,
            )
        )
    return tuple(entries)


def _absolute_cover(src: str) -> str:
    """把页面里的封面地址补成绝对 URL。

    上游混用三种写法：完整 URL、「//host/x.jpg」 协议相对、以及 「/x.jpg」 站内绝对路径。
    早先统一按协议相对处理，导致站内路径被拼成 「https:/x.jpg」 这种取不到图的地址。
    """

    src = src.strip()
    if not src:
        return ""
    if src.startswith(("http://", "https://", "data:")):
        return src
    if src.startswith("//"):
        return f"https:{src}"
    return f"{YUC_SITE}/{src.lstrip('/')}"


def _text_of(tag: Any) -> str:
    return tag.get_text(" ", strip=True) if tag is not None else ""


def weekday_label(weekday: int) -> str:
    return WEEKDAY_CN[weekday - 1] if 1 <= weekday <= 7 else "待定"
