"""通知集数的纯函数：小数集数保真，整集进度与人格转述分别校验。"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Sequence
from typing import Any

from .models import Episode


def episode_number(value: Any) -> float:
    """只接受有限的正数，拒绝区间与未展开占位符，不把 12.5 截成 12。"""

    try:
        number = float(str(value).strip())
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return number if math.isfinite(number) and number > 0 else 0.0


def episode_label(value: float) -> str:
    """普通集数补到两位，小数部分原样留下，便于对照下载器。"""

    number = episode_number(value)
    whole = int(number)
    if number == whole:
        return f"{whole:02d}"
    return f"{number:g}"


def progress_episode(value: Any) -> int:
    """追番表只存整集进度，特别篇不能代替同编号的正片记账。"""

    number = episode_number(value)
    return int(number) if number.is_integer() else 0


def inner_episode(episodes: Sequence[Episode], absolute: float) -> float:
    """只使用分集表明确给出的唯一映射；缺 ep 时不能按列表下标猜集数。

    分集表可能只返回一页或中间缺集，数组第几项不等于季内第几集。
    比较时也不能先取整，否则第 27.5 集会错误命中第 27 集。
    """

    number = episode_number(absolute)
    matches = [item for item in episodes if number and item.sort == number]
    if len(matches) != 1:
        return 0.0
    return episode_number(matches[0].ep)


_CN_DIGITS = dict(
    zip("零〇一二两三四五六七八九", (0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9), strict=True)
)
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}
_CLAIM = re.compile(
    r"第\s*([\d.零〇一二两三四五六七八九十百千点]+)\s*[集话話]"
    r"|\b(?:S\d+E|EP?\s*)(\d+(?:\.\d+)?)(?![\d.])",
    re.IGNORECASE,
)


def _claim_number(text: str) -> float:
    """人格常把数字改写为中文；校验时两种写法都要认。"""

    if not any(char in _CN_DIGITS or char in _CN_UNITS for char in text):
        return episode_number(text)
    integer, _, fraction = text.partition("点")
    total, digit = 0, 0
    for char in integer:
        if char in _CN_DIGITS:
            digit = _CN_DIGITS[char]
        elif char in _CN_UNITS:
            total += (digit or 1) * _CN_UNITS[char]
            digit = 0
        else:
            return 0.0
    if any(char not in _CN_DIGITS for char in fraction):
        return 0.0
    decimal = "".join(str(_CN_DIGITS[char]) for char in fraction)
    return episode_number(f"{total + digit}.{decimal or '0'}")


def episode_claims_match(text: str, expected: float) -> bool:
    """拦截人格口播里可识别的错误集数；不提集数的自然转述仍可通过。"""

    text = unicodedata.normalize("NFKC", text)
    return all(
        expected > 0 and _claim_number(match[1] or match[2]) == expected
        for match in _CLAIM.finditer(text)
    )
