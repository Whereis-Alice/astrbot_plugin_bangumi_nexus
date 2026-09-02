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

#: 「算同一部番」的相似度门槛。回填追番进度、判断表里是不是已经有这部、决定
#: 自动建条目要不要采用 Bangumi 规范名、「只播我追的番」筛选，全部用这一个数 ——
#: 各处各写一个字面量的话，某天调了一处就会出现「匹配得上却不认、于是又插一条」
#: 的重复记录。「similarity」 给子串关系的保底分也钉在这里，保证包含关系一定过线。
MATCH_THRESHOLD = 0.72

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

#: 打好标记后的串里的季数标记，用来把「第 2 季」和「第 3 季」区分开。
#: 注意它只在 「_season_marked」 的产物里出现，归一化键里读不到 —— 「#」 落在
#: 「_PUNCTUATION」 的 「!-/」 区间里，压成键的那一步会把它抹掉。
_SEASON_MARK = re.compile(r"#([0-9]+)")

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

#: 阿拉伯数字 → 中文数字。直接反转 「_CN_DIGITS」，免得同一张对照表写两遍还容易写歪。
_AR_TO_CN = {int(value): cn for cn, value in _CN_DIGITS.items()}

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


def _season_marked(text: str) -> str:
    """半角化 + 小写 + 把各种季度写法统一成 「#N」，其余字符原样保留。

    为什么要把归一化拆成两步：压成键的那一步会把 「#」 当标点抹掉（它落在
    「_PUNCTUATION」 的 「!-/」 区间里），于是归一化键里只剩一个光秃秃的数字，
    再也分不清「第 3 季」和标题里本来就有的 「3」。想读季数就必须在抹标点之前
    截一刀 —— 这个函数就是那一刀。
    """

    value = half_width(str(text or "")).lower()
    for pattern, replacement in _SEASON_PATTERNS:
        value = pattern.sub(replacement, value)
    for cn, arabic in _CN_DIGITS.items():
        value = value.replace(f"#{cn}", f"#{arabic}")
    return value


def _strip_key(marked: str) -> str:
    """把已打好季度标记的串压成最终归一化键：抹标点空白、去噪声词。"""

    value = _PUNCTUATION.sub("", marked)
    for noise in _NOISE_WORDS:
        if value != noise:
            value = value.replace(noise, "")
    return value


def normalize(text: str) -> str:
    """把任意写法的标题压成可比较的归一化键。

    小写 → NFKC → 中文数字季数转阿拉伯数字 → 抹标点空白 → 去噪声词。
    结果只用于比较，不用于展示。
    """

    return _strip_key(_season_marked(text))


def base_title(text: str) -> str:
    """去掉季数标记后的主干，用于「第二季」和第一季互相牵连的场景。

    必须在 「_season_marked」 的产物上剪，不能在归一化键上剪 —— 键里的 「#」
    已经被抹掉，正则 「#[0-9]+」 一个都匹配不上，季数会原封不动留在主干里。
    """

    return _strip_key(_SEASON_MARK.sub("", _season_marked(text)))


def _marked_season(marked: str) -> int:
    """从已打好标记的串里读季数。内部用，省掉重复调用 「_season_marked」。"""

    match = _SEASON_MARK.search(marked)
    return int(match.group(1)) if match else 0


def season_number(text: str) -> int:
    """读出标题里显式写明的季数；没有季度标记返回 0。

    0 的语义是「未知」而不是「第一季」，这个区分是刻意的：下载器推来的标题、
    Bangumi 的首季条目大多根本不写季数，一旦把「没写」当成「第 1 季」，
    第三季的更新通知就再也匹配不上表里那条无季标的旧记录了。
    """

    return _marked_season(_season_marked(text))


def season_conflict(left: str, right: str) -> bool:
    """判断两个标题是否分属不同季。

    只有**双方都写明**季数、且数字不同才算冲突。一侧没写就当未知、继续宽容匹配 ——
    否则「超超超超超喜欢你的100个女朋友」（表里的旧条目）会被判定成和
    「……第三季」（推送标题）无关，那个会话将永远收不到通知。
    """

    left_season, right_season = season_number(left), season_number(right)
    return bool(left_season and right_season and left_season != right_season)


def qualify_season(title: str, season: int) -> str:
    """把下载器分成两个字段给的「标题 + 季号」合成一个能直接匹配的完整标题。

    ani-rss 之类的下载器把季度单独放在 「season」 字段，标题里往往一个字都不提，
    于是「第三季」的更新看上去和第一季长得一模一样。这里补回后缀，让卡片、
    追番匹配、进度回填三处共用同一个带季度的标题。

    第 1 季刻意不加后缀：首季条目几乎都不写「第一季」，硬加反而制造出
    「双方都写明且不同」的显式冲突，把本来该命中的记录排除掉（见 「season_conflict」）。
    """

    if season <= 1 or not title or season_number(title):
        return title
    suffix = _AR_TO_CN.get(season, str(season))
    return f"{title} 第{suffix}季"


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
    """0~1 的标题相似度，两侧先归一化。子串关系直接给高分。

    季数显式冲突时一律压到 「MATCH_THRESHOLD」 之下：同一部作品的不同季主干完全
    一致，「#2」和「#3」的字符相似度能到 0.94，光靠字符比较必然串台 —— 第三季的
    更新会被回填进第二季的追番记录。反之只要有一侧没写季数就保持宽容，理由见
    「season_conflict」。
    """

    marked_left, marked_right = _season_marked(left), _season_marked(right)
    a, b = _strip_key(marked_left), _strip_key(marked_right)
    if not a or not b:
        return 0.0
    left_season, right_season = _marked_season(marked_left), _marked_season(marked_right)
    if left_season and right_season and left_season != right_season:
        return min(SequenceMatcher(None, a, b).ratio(), MATCH_THRESHOLD - 0.02)
    if a == b:
        return 1.0
    if a in b or b in a:
        # 「进击的巨人」vs「进击的巨人 最终季」这类包含关系，按长度比例给分，
        # 但保底 「MATCH_THRESHOLD」，确保它一定压过纯字符重合的巧合。
        ratio = min(len(a), len(b)) / max(len(a), len(b))
        return max(MATCH_THRESHOLD, min(0.95, ratio + 0.2))
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
    def air_weekday(self) -> int:
        """按日本放送日归组用的星期（1=周一 … 7=周日）。

        跟 Bangumi 每日放送的口径对齐，深夜档**不**归到前一天：
        日本时间周日 00:30 的番算周日，不算周六。
        """

        return self.start.astimezone(JST).isoweekday()

    @property
    def jst_time(self) -> str:
        """日本当地放送时刻，「HH:MM」。"""

        return self.start.astimezone(JST).strftime("%H:%M")

    @property
    def slot_label(self) -> str:
        """放送时刻，跟 「air_weekday」 同一个时区口径（日本当地）。

        这里以前按 Bot 本地时区算、并把凌晨改写成 「24:xx」，跟按日本日期归组的
        「air_weekday」 撞车了：日本时间周日 01:00 的番被归进「周日」那一栏，
        标签却写成 「24:00」 —— 按圈内惯例这读作「周六深夜」，等于把同一场放送
        说成了两个不同的日子，实际差了近 23 小时。所以显示与归组统一走日本时间，
        不再改写钟点，只给凌晨档加「深夜」前缀提示它属于前一晚的档期。
        """

        moment = self.start.astimezone(JST)
        text = f"{moment.hour:02d}:{moment.minute:02d}"
        return f"深夜 {text}" if moment.hour < 5 else text

    def label(self) -> str:
        """「周日 01:00」 这样的一行，星期与钟点都按日本时间。"""

        from .constants import WEEKDAY_CN

        return f"{WEEKDAY_CN[self.air_weekday - 1]} {self.jst_time}"

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
