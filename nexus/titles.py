"""标题归一化、相似度匹配与季度 / 放送时间解析。

跨源匹配全靠这一层：各站点的写法差异极大（半角全角、罗马数字、季数后缀、
繁简、日文原名 vs 中文译名），所以先把标题压成一个「归一化键」，再用别名集合
求交集，最后才退化到模糊相似度。全部是纯函数，方便单测。

Copyright (C) 2026 Whereis-Alice and AstrBot Plugin Authors.
Licensed under the GNU Affero General Public License v3.0 or later.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any, TypeVar

T = TypeVar("T")

#: 归一化时直接抹掉的字符：标点、空白、装饰符号。
_PUNCTUATION = re.compile(
    r"[\s\u3000!-/:-@\[-`{-~！-｀。、，．・：；？「」『』（）【】〈〉《》～—－ー〜♪★☆♥♡]+"
)

#: 常见的「第 N 季 / Season N / 2nd Season」后缀，归一化成 「#N」。
_SEASON_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"第([0-9一二三四五六七八九十]+)[期季部]"), r"#\1"),
    (re.compile(r"season\s*([0-9]+)", re.I), r"#\1"),
    (re.compile(r"([0-9]+)(?:st|nd|rd|th)\s*season", re.I), r"#\1"),
    (re.compile(r"\bs([0-9]{1,2})\b", re.I), r"#\1"),
)

_CN_DIGITS = {
    "一": "1",
    "二": "2",
    "三": "3",
    "四": "4",
    "五": "5",
    "六": "6",
    "七": "7",
    "八": "8",
    "九": "9",
    "十": "10",
}

#: 标题里频繁出现、对区分作品毫无帮助的噪声词。
_NOISE_WORDS = (
    "tv动画",
    "tvanime",
    "剧场版",
    "劇場版",
    "ova",
    "oad",
    "特别篇",
    "特別篇",
    "movie",
    "the animation",
    "theanimation",
    "动画",
    "動畫",
)


def half_width(text: str) -> str:
    """全角转半角，并做 Unicode 兼容分解（把 ㍿ Ⅲ ① 之类拍平）。"""

    return unicodedata.normalize("NFKC", text or "")


def normalize(text: str) -> str:
    """把任意写法的标题压成可比较的归一化键。

    小写 → NFKC → 中文数字季数转阿拉伯数字 → 抹标点空白 → 去噪声词。
    结果只用于比较，不用于展示。
    """

    value = half_width(str(text or "")).lower()
    for pattern, replacement in _SEASON_PATTERNS:
        value = pattern.sub(replacement, value)
    for cn, arabic in _CN_DIGITS.items():
        value = value.replace(f"#{cn}", f"#{arabic}")
    value = _PUNCTUATION.sub("", value)
    for noise in _NOISE_WORDS:
        if value != noise:
            value = value.replace(noise, "")
    return value


def base_title(text: str) -> str:
    """去掉季数标记后的主干，用于「第二季」和第一季互相牵连的场景。"""

    return re.sub(r"#[0-9]+", "", normalize(text))


def alias_keys(*titles: str | Iterable[str] | None) -> frozenset[str]:
    """把一批标题（可混入可迭代对象）展开成归一化键集合。"""

    keys: set[str] = set()
    for entry in titles:
        if entry is None:
            continue
        candidates: Iterable[str]
        candidates = [entry] if isinstance(entry, str) else entry
        for title in candidates:
            key = normalize(title)
            if len(key) >= 2:
                keys.add(key)
    return frozenset(keys)


def similarity(left: str, right: str) -> float:
    """0~1 的标题相似度，两侧先归一化。子串关系直接给高分。"""

    a, b = normalize(left), normalize(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        # 「进击的巨人」vs「进击的巨人 最终季」这类包含关系，按长度比例给分，
        # 但保底 0.72，确保它一定压过纯字符重合的巧合。
        ratio = min(len(a), len(b)) / max(len(a), len(b))
        return max(0.72, min(0.95, ratio + 0.2))
    return SequenceMatcher(None, a, b).ratio()


def best_match(
    query: str,
    candidates: Sequence[T],
    *,
    key: Any,
    threshold: float = 0.62,
) -> tuple[T | None, float]:
    """在候选里挑与 `query` 最像的一个。

    `key` 接收候选返回单个标题或标题序列（别名越多命中率越高）。低于
    `threshold` 一律视为没匹配上，宁缺毋滥 —— 匹配错比匹配不到更糟。
    """

    best: T | None = None
    best_score = 0.0
    for candidate in candidates:
        raw = key(candidate)
        titles: Iterable[str] = [raw] if isinstance(raw, str) else (raw or ())
        score = max((similarity(query, title) for title in titles), default=0.0)
        if score > best_score:
            best, best_score = candidate, score
    if best_score < threshold:
        return None, best_score
    return best, best_score


# ---------------------------------------------------------------------------
# 季度
# ---------------------------------------------------------------------------

SEASON_MONTHS = (1, 4, 7, 10)
SEASON_LABELS = {1: "冬", 4: "春", 7: "夏", 10: "秋"}


def season_code(moment: datetime | None = None) -> str:
    """当前所属季度的 `YYYYMM` 代号（月份取 01/04/07/10）。"""

    now = moment or datetime.now()
    month = max(month for month in SEASON_MONTHS if month <= now.month)
    return f"{now.year}{month:02d}"


def next_season_code(code: str) -> str:
    """下一季的代号。非法输入直接回落到当前季度。"""

    year, month = parse_season_code(code)
    if year == 0:
        return season_code()
    index = SEASON_MONTHS.index(month)
    if index == len(SEASON_MONTHS) - 1:
        return f"{year + 1}01"
    return f"{year}{SEASON_MONTHS[index + 1]:02d}"


def parse_season_code(code: str) -> tuple[int, int]:
    """`202607` → `(2026, 7)`；解析失败返回 `(0, 0)`。"""

    text = re.sub(r"\D", "", str(code or ""))
    if len(text) != 6:
        return 0, 0
    year, month = int(text[:4]), int(text[4:])
    if not 1900 <= year <= 2200 or month not in SEASON_MONTHS:
        return 0, 0
    return year, month


def season_label(code: str) -> str:
    """`202607` → `2026 年夏季`。"""

    year, month = parse_season_code(code)
    if not year:
        return str(code)
    return f"{year} 年{SEASON_LABELS[month]}季"


def season_codes_around(code: str | None = None, span: int = 1) -> tuple[str, ...]:
    """以某季为中心，前后各 `span` 季的代号（用于跨季度找番）。"""

    center = code or season_code()
    year, month = parse_season_code(center)
    if not year:
        year, month = parse_season_code(season_code())
    codes: list[str] = []
    index = SEASON_MONTHS.index(month) + year * 4
    for offset in range(-span, span + 1):
        pointer = index + offset
        codes.append(f"{pointer // 4}{SEASON_MONTHS[pointer % 4]:02d}")
    return tuple(codes)


def data_months(code: str) -> tuple[tuple[int, int], ...]:
    """一个季度覆盖的 `(year, month)`，用于拉 bangumi-data 的月度分片。"""

    year, month = parse_season_code(code)
    if not year:
        return ()
    return tuple((year, month + offset) for offset in range(3) if month + offset <= 12)


# ---------------------------------------------------------------------------
# 放送时间
# ---------------------------------------------------------------------------

_BROADCAST_RE = re.compile(r"^R/(?P<start>[^/]+)/P(?P<count>\d+)(?P<unit>[DWMY])$")


def parse_datetime(text: str) -> datetime | None:
    """宽松解析 ISO 8601（含 `Z` 结尾），失败返回 None。"""

    raw = str(text or "").strip()
    if not raw:
        return None
    raw = raw.replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m"):
            try:
                moment = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment


#: 日本标准时间。放送表的「星期几」在业界一律按日本当地日期算，
#: Bangumi 的 「air_weekday」 也是这个口径，所以归组必须用它，
#: 不能用 Bot 所在时区 —— 否则同一部深夜番会在两个星期里各出现一次。
JST = timezone(timedelta(hours=9), "JST")


@dataclass(frozen=True)
class Broadcast:
    """一条重复放送规则。"""

    start: datetime
    interval_days: int = 7

    @property
    def weekday(self) -> int:
        """1=周一 … 7=周日，按 Bot 本地时区换算。"""

        return self.start.astimezone().isoweekday()

    @property
    def air_weekday(self) -> int:
        """按日本放送日归组用的星期（1=周一 … 7=周日）。

        跟 Bangumi 每日放送的口径对齐，深夜档**不**归到前一天：
        日本时间周日 00:30 的番算周日，不算周六。
        """

        return self.start.astimezone(JST).isoweekday()

    @property
    def local_time(self) -> str:
        return self.start.astimezone().strftime("%H:%M")

    @property
    def slot_label(self) -> str:
        """「24:30」 风格的本地放送时刻。

        深夜档写成 「24:30」 而不是 「00:30」 是圈内惯例，
        一眼能看出它属于前一天晚上那一档，比裸的 「00:30」 更不容易误读。
        """

        local = self.start.astimezone()
        if local.hour < 5:
            return f"{local.hour + 24}:{local.minute:02d}"
        return f"{local.hour:02d}:{local.minute:02d}"

    def label(self) -> str:
        from .constants import WEEKDAY_CN

        return f"{WEEKDAY_CN[self.weekday - 1]} {self.local_time}"

    def next_after(self, moment: datetime | None = None) -> datetime:
        """下一次放送时刻（含当下这一刻之后的第一次）。"""

        now = moment or datetime.now(UTC)
        if self.interval_days <= 0 or self.start >= now:
            return self.start
        elapsed = (now - self.start).days
        steps = elapsed // self.interval_days + 1
        return self.start + timedelta(days=self.interval_days * steps)


def parse_broadcast(text: str) -> Broadcast | None:
    """解析 bangumi-data 的 `R/2026-07-01T13:00:00.000Z/P7D` 形式。"""

    match = _BROADCAST_RE.match(str(text or "").strip())
    if not match:
        return None
    start = parse_datetime(match.group("start"))
    if start is None:
        return None
    count = int(match.group("count"))
    unit_days = {"D": 1, "W": 7, "M": 30, "Y": 365}[match.group("unit")]
    return Broadcast(start=start, interval_days=count * unit_days)


def humanize_delta(target: datetime | None, *, now: datetime | None = None) -> str:
    """把「距离下次放送」写成人话。已过去的时间返回空串。"""

    if target is None:
        return ""
    moment = now or datetime.now(UTC)
    seconds = (target - moment).total_seconds()
    if seconds < 0:
        return ""
    if seconds < 3600:
        return f"{max(1, int(seconds // 60))} 分钟后"
    if seconds < 86400:
        return f"{int(seconds // 3600)} 小时后"
    return f"{int(seconds // 86400)} 天后"
