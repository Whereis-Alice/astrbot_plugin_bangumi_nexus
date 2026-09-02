"""Webhook 请求体的容错解析。

**为什么单独拆一个模块**：ani-rss 这类推送端的请求体是「字符串模板拼出来的」——
占位符要不要加引号全靠用户自己拼对，拼错了服务端只看到一句「不是合法 JSON」。
最典型的一例：ani-rss 用 Hutool 的 「JSONUtil.quote(text, false)」 处理 「${message}」，
只转义、不补外层引号，所以模板里必须自己把它包进一对引号（「"message":"${message}"」）。
少写这一对，每一条推送都会 400，而用户那头只看到「通知失败」。

于是严格解析失败后再试两招，把「一眼能看懂该怎么补」的两类事故救回来：

1. 「strict=False」：字符串里有没转义的换行、制表符（手抄模板最常见的一种）；
2. 补外层引号：「"键":裸值」 —— 就是上面漏掉的那一对。

**刻意不做的事**：不重新转义。ani-rss 已经转义过一遍，再转一次会让卡片里冒出字面的
反斜杠加 n。所以这里只在裸值两端各加一个引号，补完能不能解析交给 json 自己判 ——
判不过就照旧 400。宁可拒绝，也不猜出一份面目全非的 payload。

救回来不等于「没事发生」：调用方拿到 「repairs」 就该打一条 warning 让用户去改模板。
容错是止血，不是终点。

Copyright (C) 2026 Whereis-Alice and AstrBot Plugin Authors.
Licensed under the GNU Affero General Public License v3.0 or later.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

# 最多补几处裸值。ani-rss 的模板通常只错一处（就是 ${message}），给到 6 是为了
# 容忍「顺手把 ${score}、${subgroup} 的引号也一起漏了」这种连环错。
MAX_REPAIR_PASSES = 6

# 日志里贴多少字原文。太少看不出错在哪个字段，太多会把整段通知文本刷进日志。
PREVIEW_LIMIT = 200

REPAIR_CONTROL_CHARS = "字符串里有没转义的换行或制表符"
REPAIR_BARE_VALUE = "有字段的值没被引号包住（多半是模板里 ${message} 少了一对引号）"

# 「, "下一个键":」 —— 给裸值找右边界用。键名按「非引号字符或转义对」匹配，于是值
# 内部被转义过的引号（前面带反斜杠）不会被误认成下一个键的开头。
_NEXT_KEY = re.compile(r',\s*"(?:[^"\\]|\\.)*"\s*:')

# 补引号时加的那个字符。抽成常量是为了让下面的拼接一眼看清「只多了两个字符」——
# 后面判断解析器有没有前进，全靠这个 2。
_QUOTE = '"'


@dataclass(frozen=True, slots=True)
class ParsedBody:
    """解析结果。「repairs」 非空说明是靠容错救回来的，调用方该提醒用户改模板。"""

    payload: Any
    repairs: tuple[str, ...] = ()


def parse_body(text: str) -> ParsedBody:
    """解析请求体；救不回来就把最初那个 「JSONDecodeError」 抛出去。

    抛「最初」那个而不是修补过程中的新异常：只有原文里的出错位置才和用户模板里
    看到的东西对得上，修补后的偏移量对排查毫无帮助。
    """
    raw = text.strip() or "{}"
    try:
        return ParsedBody(json.loads(raw))
    except json.JSONDecodeError as strict_error:
        return _repair(raw, strict_error)


def preview(text: str, limit: int = PREVIEW_LIMIT) -> str:
    """截一段能贴进单行日志的原文：先把换行压成空格，再按字数截断。"""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit] + "……"


def _repair(raw: str, strict_error: json.JSONDecodeError) -> ParsedBody:
    """两级兜底：先放宽控制字符，再补漏掉的引号。"""
    try:
        return ParsedBody(json.loads(raw, strict=False), (REPAIR_CONTROL_CHARS,))
    except json.JSONDecodeError:
        pass
    parsed = _quote_bare_values(raw)
    if parsed is None:
        raise strict_error
    return parsed


def _quote_bare_values(raw: str) -> ParsedBody | None:
    """把 「"键":裸值」 里的裸值逐处补上引号，全补好才返回。

    定位不靠自己写扫描器，而是直接用 json 抛出来的 「JSONDecodeError.pos」——
    它精确指向解析器读不下去的那个字符，也就是裸值的第一个字符。一轮补一处，
    下一轮的报错位置自然指向下一处。
    """
    current = raw
    for _ in range(MAX_REPAIR_PASSES):
        try:
            payload = json.loads(current, strict=False)
        except json.JSONDecodeError as exc:
            fixed = _quote_one(current, exc.pos)
            if fixed is None:
                return None
            current = fixed
            continue
        return ParsedBody(payload, _repairs_of(current))
    return None


def _quote_one(text: str, start: int) -> str | None:
    """给 「text[start:]」 开头的裸值补一对引号，返回补完的整串。

    只修 「"键":」 后面的裸值 —— 数组元素、顶层裸串这些情况硬猜容易猜歪，
    宁可回 400 让用户看见真正的错在哪。
    """
    if not _after_colon(text, start):
        return None
    for end in _value_ends(text, start):
        segment = text[start:end]
        core = segment.rstrip()
        fixed = text[:start] + _QUOTE + core + _QUOTE + segment[len(core) :] + text[end:]
        if _moved_past(fixed, end):
            return fixed
    return None


def _value_ends(text: str, start: int) -> list[int]:
    """裸值可能在哪儿结束，从近到远排。

    两种候选：下一个键前面那个逗号（值在中间），或者整串最后一个右括号（值是最后
    一项）。转义过的引号前面带着反斜杠，所以值内部的引号不会被当成下一个键的开头。
    """
    ends: set[int] = set()
    match = _NEXT_KEY.search(text, start)
    if match is not None:
        ends.add(match.start())
    for closer in ("}", "]"):
        index = text.rfind(closer)
        if index >= start:
            ends.add(index)
    return sorted(ends)


def _moved_past(text: str, end: int) -> bool:
    """补完之后，解析器有没有真的越过刚补好的这一段。

    只插了两个引号，所以原来 「end」 处的字符现在落在 「end + 2」。报错位置到了那里
    或更后面，说明这一段已经被当成一个完整字符串读过去了 —— 后面可能还有别的裸值，
    交给外层再来一轮。位置没往前挪就说明右边界猜错了，换下一个候选。
    """
    try:
        json.loads(text, strict=False)
    except json.JSONDecodeError as exc:
        return exc.pos >= end + 2
    return True


def _after_colon(text: str, start: int) -> bool:
    """「start」 是不是紧跟在一个冒号后面（中间只允许空白）。

    这一条是整个容错的安全边界：只有「键: 值」 里的值才补引号，别的位置一律不猜。
    """
    index = start - 1
    while index >= 0 and text[index] in " \t\r\n":
        index -= 1
    return index >= 0 and text[index] == ":"


def _repairs_of(text: str) -> tuple[str, ...]:
    """补完引号后再严格判一次：还过不了就说明里头也有裸控制字符。

    两件事一起说清楚，省得用户改完一处又撞另一处。
    """
    try:
        json.loads(text)
    except json.JSONDecodeError:
        return (REPAIR_BARE_VALUE, REPAIR_CONTROL_CHARS)
    return (REPAIR_BARE_VALUE,)
