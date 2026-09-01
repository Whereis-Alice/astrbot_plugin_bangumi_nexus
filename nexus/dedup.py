"""同一集多版本的归并：简繁、画质、片源各一条时只留最合口味的那份。

为什么单独开一个模块：这里全是纯字符串推理 —— 从发布标题里认出「这是第几集」、
「这是哪个片源」，然后排个优先级。没有 IO、不读配置、不碰数据库，所以可以直接
喂真实标题做回归测试。混进轮询服务里就会跟 「last_checked」 这类状态搅在一起，
一旦推错谁也说不清是解析错了还是状态错了。

典型场景（用户实测）：「Kirara Fantasia」 同一集会同时出现 Baha 与 ABEMA 两个
片源，再各配简体 / 繁体、1080p / 720p —— 一集刷四到六条。用排除项硬屏蔽 「ABEMA」
能治表面，但那天 Baha 没出片就整集收不到；归并是「都收下，只推一条」。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import TypeVar

from .constants import EPISODE_PREFER_CHOICES, EPISODE_PREFER_DEFAULT, RELEASE_TAG_RULES

T = TypeVar("T")

#: 各类括号包住的段落。字幕组名、画质、片源、语言几乎都在括号里，
#: 抠掉之后剩下的就接近「作品名 + 集数」。全角括号也要算进来（「【喵萌奶茶屋】」）。
_BRACKET_RE = re.compile(r"[\[\(【（][^\[\]\(\)【】（）]*[\]\)】）]")

#: 集数写法。顺序即优先级，第一个命中的就作准。
#: ⚠ 位数都卡在 3 位以内：放开到 4 位会把 「[2026]」「1080」 认成集数。
_EPISODE_RES: tuple[re.Pattern[str], ...] = (
    # 第01话 / 第 12 集
    re.compile(r"第\s*(\d{1,3})\s*[话話集]"),
    # S01E05
    re.compile(r"\bS\d{1,2}E(\d{1,3})\b", re.IGNORECASE),
    # [01] / [12v2] / [24END] / [13 END]
    re.compile(r"[\[【]\s*(\d{1,3})\s*(?:v\d)?\s*(?:END|FIN|完|结|終)?\s*[\]】]", re.IGNORECASE),
    # - 01 / - 12v2（Mikan 上最常见的写法）
    re.compile(r"[\s\-–—]-\s*(\d{1,3})(?:v\d)?(?=[\s\[【（(]|$)"),
    # EP01 / E 12
    re.compile(r"\bEP?\s*(\d{1,3})\b", re.IGNORECASE),
)

#: 括号段里「一看就是技术参数」的长词。只要包含其中之一，整段视为元信息。
#: 全是长到不会误伤的词 —— 短缩写放 「_META_EXACT」 走全等匹配。
_META_CONTAINS: tuple[str, ...] = (
    "web-dl",
    "webdl",
    "webrip",
    "bdrip",
    "bdbox",
    "blu-ray",
    "bluray",
    "avc",
    "hevc",
    "x264",
    "x265",
    "h264",
    "h265",
    "aac",
    "flac",
    "opus",
    "8bit",
    "10bit",
    "ma10p",
    "ma444",
    "yuv420",
    "1920x1080",
    "1280x720",
    "3840x2160",
    "1080p",
    "720p",
    "480p",
    "2160p",
    "简日",
    "繁日",
    "简体",
    "繁体",
    "简中",
    "繁中",
    "简繁",
    "繁简",
    "双语",
    "双字",
    "内嵌",
    "外挂",
    "内封",
    "无字幕",
    "生肉",
    "合集",
    "batch",
    "baha",
    "bahamut",
    "abema",
    "crunchyroll",
    "b-global",
    "bglobal",
    "bilibili",
    "netflix",
    "disney",
    "viu",
    "mp4",
    "mkv",
)

#: 短到会误伤的缩写：必须整段完全等于才算元信息。
#: 反例：「[GB]」 是简体标记，但 「[GBC]」 可能是作品名的一部分；
#: 「[CR]」 是 Crunchyroll，可 「[CROSS]」 显然不是。
_META_EXACT: frozenset[str] = frozenset(
    {
        "gb",
        "big5",
        "chs",
        "cht",
        "sc",
        "tc",
        "cn",
        "jp",
        "jpn",
        "jpsc",
        "jptc",
        "ass",
        "srt",
        "pgs",
        "sub",
        "subs",
        "cr",
        "bd",
        "dvd",
        "tv",
        "web",
        "nc",
        "v2",
        "v3",
        "raw",
        "end",
        "fin",
        "ova",
        "oad",
        "sp",
    }
)

#: 纯集数的括号段：「[01]」「[13v2]」「[24END]」「[12 END]」。
_EPISODE_ONLY_RE = re.compile(r"^\d{1,3}\s*(?:v\d)?\s*(?:end|fin|完|结|終)?$", re.IGNORECASE)

#: 修订号 「v2」「v3」。同一集的 v2 是修正版，应该顶掉 v1。
_REVISION_RE = re.compile(r"\b(\d{1,3})v(\d)\b", re.IGNORECASE)

#: 归一化作品名时要抹掉的分隔符与装饰字符。
_NOISE_RE = re.compile(r"[\s_\-–—.·、,，:：!！?？~～★☆\*/\\|'\"]+")


def release_tags(title: str) -> tuple[str, ...]:
    """从单条发布标题里嗅出语言 / 画质 / 片源标记，顺序固定。

    跟 「sources.mikan.release_tags」 用同一份规则表，但那个吃的是一组标题、
    用于在选源列表上概括一个字幕组；这里针对单条发布，用于比较两条谁更合口味。
    """
    text = (title or "").lower()
    return tuple(
        label
        for label, needles in RELEASE_TAG_RULES
        if any(needle.lower() in text for needle in needles)
    )


def episode_number(title: str) -> str:
    """认出标题里的集数，认不出返回空串。

    返回字符串而不是 int：「01」 和 「1」 在同一个 feed 里不会混用，而保留原始
    位数便于日志排查。补零统一到两位，这样 「[1]」 和 「[01]」 仍能归到一起。
    """
    text = title or ""
    for pattern in _EPISODE_RES:
        found = pattern.search(text)
        if found:
            return found.group(1).lstrip("0").zfill(2) or "00"
    return ""


def revision(title: str) -> int:
    """认出 「01v2」 里的修订号，没有就算 1。"""

    found = _REVISION_RE.search(title or "")
    return int(found.group(2)) if found else 1


def _is_metadata(segment: str) -> bool:
    """判断一个括号段是不是技术参数 / 语言画质 / 集数，而不是作品名。

    为什么要分辨：早先的实现把所有括号段一律抠掉，结果 「[喵萌奶茶屋]★10月新番★
    [名探偵プリキュア][22][1080p][简日双语]」 这种把作品名也写在括号里的标题，
    归并键只剩下 「10月新番」 —— 同一个组同一周的两部不同番会被当成同一集互相顶掉。
    那是静默丢消息，比刷屏严重得多，所以宁可少归并也不能错归并。
    """
    text = segment.strip().lower()
    if not text:
        return True
    if _EPISODE_ONLY_RE.match(text):
        return True
    if text in _META_EXACT:
        return True
    return any(word in text for word in _META_CONTAINS)


def series_key(title: str) -> str:
    """抠掉集数与技术参数后剩下的部分，作为归并的分组键。

    刻意保留字幕组名：组名对同一条订阅是常量，留着不影响归并，却能挡住
    「两个组同一集」被误并 —— 用户按番名订阅时一条源里能混进七八个组，
    真要跨组归并应该由用户在选源那一步收敛，而不是在这里猜。
    """
    kept: list[str] = []
    for segment in _BRACKET_RE.findall(title or ""):
        inner = segment[1:-1]
        if not _is_metadata(inner):
            kept.append(inner)
    bare = _BRACKET_RE.sub(" ", title or "")
    for pattern in _EPISODE_RES:
        bare = pattern.sub(" ", bare)
    kept.append(bare)
    return _NOISE_RE.sub("", " ".join(kept)).lower()


def normalize_prefer(values: Iterable[str] | None) -> tuple[str, ...]:
    """把用户填的优选顺序洗成合法标记序列，非法项直接丢掉。

    大小写按 「RELEASE_TAG_RULES」 的展示名回正（用户会写 「baha」「1080P」），
    去重且保持先后顺序 —— 顺序就是优先级，这一点不能被去重打乱。
    """
    canon = {label.lower(): label for label in EPISODE_PREFER_CHOICES}
    result: list[str] = []
    for raw in values or ():
        label = canon.get(str(raw or "").strip().lower())
        if label and label not in result:
            result.append(label)
    return tuple(result)


def prefer_score(title: str, prefer: Sequence[str]) -> int:
    """按优选顺序给一条发布打分，分越高越该留下。

    权重取 「2 的幂」 而不是平方：只有二进制权重才能保证「第一优先命中」严格
    压得住「后面所有项全部命中」（2^n > 2^(n-1) + … + 2^0）。平方权重不够 ——
    5 项时首位 25 分会被 16+9+4+1=30 分反超，用户把 「简体」 放第一位却收到
    繁体版，那是明显的违背预期。
    """
    tags = set(release_tags(title))
    total = len(prefer)
    return sum(1 << (total - index) for index, label in enumerate(prefer) if label in tags)


def _title_of(item: object) -> str:
    """默认的取标题方式：字符串直接用，其余对象取 「.title」 属性。

    ⚠ 不能写成 「getattr(item, "title", item)」 —— 「str」 自带 「title()」 方法，
    喂裸标题时会取到一个绑定方法，分组键全都变成 「built-in method title…」，
    于是「谁都不跟谁同组」，归并静默失效。这个坑只在单测里喂字符串时才暴露。
    """
    if isinstance(item, str):
        return item
    value = getattr(item, "title", None)
    return value if isinstance(value, str) else str(item)


@dataclass(frozen=True)
class DedupOutcome:
    """归并结果：「kept」 是要推的，「dropped」 是同集里落选的。

    落选的也要交回调用方 —— 它们必须一起写进已推送历史，否则下一轮又被当成
    新条目重新参选，最后还是会刷屏。
    """

    kept: tuple = ()
    dropped: tuple = ()

    @property
    def merged(self) -> int:
        """被折叠掉的条数，用于在通知里说明「同集另有 N 个版本」。"""

        return len(self.dropped)


def dedupe_releases(
    items: Iterable[T],
    *,
    prefer: Sequence[str] = EPISODE_PREFER_DEFAULT,
    title_of: Callable[[T], str] = _title_of,
) -> DedupOutcome:
    """同一集只留一条，其余当作已读丢弃。

    分组键是「作品名 + 集数」。认不出集数的条目一律原样保留 —— 剧场版、
    合集、字幕组的公告都长这样，宁可多推一条也不能把它们互相顶掉。

    同组内的排序依据依次是：修订号（v2 顶掉 v1）→ 优选得分 → 原始顺序。
    最后那一档保证结果稳定：RSS 一般按时间倒序，同分时留下更新的那条。
    """
    order = normalize_prefer(prefer)
    groups: dict[tuple[str, str], list[tuple[int, int, int, T]]] = {}
    loners: list[tuple[int, T]] = []
    for index, item in enumerate(items):
        title = title_of(item)
        episode = episode_number(title)
        if not episode:
            loners.append((index, item))
            continue
        key = (series_key(title), episode)
        groups.setdefault(key, []).append(
            (revision(title), prefer_score(title, order), -index, item)
        )

    ranked: list[tuple[int, T]] = list(loners)
    dropped: list[tuple[int, T]] = []
    for bucket in groups.values():
        bucket.sort(key=lambda row: row[:3], reverse=True)
        ranked.append((-bucket[0][2], bucket[0][3]))
        dropped.extend((-row[2], row[3]) for row in bucket[1:])

    ranked.sort(key=lambda row: row[0])
    dropped.sort(key=lambda row: row[0])
    return DedupOutcome(
        kept=tuple(item for _, item in ranked),
        dropped=tuple(item for _, item in dropped),
    )
