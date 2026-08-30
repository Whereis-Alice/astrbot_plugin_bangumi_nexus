"""WebUI 前端的静态接线检查（不启浏览器）。

「pages/nexus/app.js」 用的是手写的分发表：HTML 里写 「data-act="x"」，
JS 里在 「ACTIONS」 表查 「x」。这种写法很轻，但名字打错时只会在点击的瞬间
弹一句「这个按钮还没接上处理逻辑」—— 属于必须在 CI 里被拦住的错误。

同理，前端 「apiGet("subs/sources")」 与后端 「routes()」 里注册的路径
是两处独立的字符串，对不上就是 404。这里用正则把两边抽出来对一遍。

纯文本分析，不 import 前端，也不需要 node，所以在任何环境都能跑。

Copyright (C) 2026 Whereis-Alice and AstrBot Plugin Authors.
Licensed under the GNU Affero General Public License v3.0 or later.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "pages" / "nexus" / "app.js").read_text(encoding="utf-8")
API_PY = (ROOT / "nexus" / "web" / "api.py").read_text(encoding="utf-8")


def _table_keys(source: str, header: str) -> frozenset[str]:
    """抽出一个顶层对象字面量的键名。

    只认「行首两个空格 + 键 + 冒号」这一种写法 —— 表里的每个 handler 都长这样，
    而嵌套在 handler 内部的对象缩进更深，不会被误收。
    """
    start = source.index(header) + len(header)
    end = source.index("\n};", start)
    body = source[start:end]
    return frozenset(re.findall(r'^  "?([a-z0-9-]+)"?:', body, re.MULTILINE))


ACTION_KEYS = _table_keys(APP_JS, "const ACTIONS = {")
LIVE_KEYS = _table_keys(APP_JS, "const LIVE_SETTERS = {")

# 「segmented()」 把 act 当第三个位置参数传，抽它要先把换行压平。
FLAT_JS = re.sub(r"\s+", " ", APP_JS)
# 「[^;]{0,240}?」 把匹配限制在一条语句内，否则会一路吞到几百行之后去。
SEGMENTED_ACTS = re.findall(
    r'(?<!function )segmented\([^;]{0,240}?"([a-z0-9-]+)"\s*,?\s*\)',
    FLAT_JS,
)

#: 灯箱（放大看卡片）自己挂了一个局部监听器，刻意不进全局 「ACTIONS」 表 ——
#: 它的三个按钮只在灯箱存在期间有效，关掉就该彻底失效。
LIGHTBOX_ACTS = frozenset({"lb-close", "lb-fit", "lb-actual"})

# 四种引用方式：辅助函数的 「act:」 参数、手写的 「data-act="x"」、
# 回车快捷键 「data-enter」，以及 「segmented()」 的位置参数。
REFERENCED_ACTS = (
    frozenset(
        re.findall(r'\bact:\s*"([a-z0-9-]+)"', APP_JS)
        + re.findall(r'data-act="([a-z0-9-]+)"', APP_JS)
        + re.findall(r'data-enter="([a-z0-9-]+)"', APP_JS)
        + SEGMENTED_ACTS
    )
    - LIGHTBOX_ACTS
)
REFERENCED_LIVE = frozenset(re.findall(r'data-live="([a-z0-9-]+)"', APP_JS))

# 后端路由都写成 「prefix + "/xxx"」，前端调用则是 「apiGet("xxx")」。
BACKEND_ROUTES = frozenset(re.findall(r'prefix \+ "/([a-z0-9_/]+)"', API_PY))
FRONTEND_ENDPOINTS = frozenset(re.findall(r'\bapi(?:Get|Post)\("([a-z0-9_/]+)"', APP_JS))


class TestTablesParsed:
    def test_抽取到的表不为空(self) -> None:
        """正则一旦被重构改坏，下面几条断言会变成「空集合恒成立」的假通过。"""

        assert len(ACTION_KEYS) > 40
        assert len(LIVE_KEYS) > 5
        assert len(REFERENCED_ACTS) > 40
        assert len(BACKEND_ROUTES) > 15
        assert len(FRONTEND_ENDPOINTS) > 10
        # 「segmented()」 的位置参数最容易被正则漏掉，单独钉一下条数。
        assert len(SEGMENTED_ACTS) >= 3


class TestActionWiring:
    def test_每个被引用的_act_都有处理函数(self) -> None:
        """名字打错时点击只会弹一句无害的提示，靠人肉点遍全部按钮才能发现。"""

        assert not sorted(REFERENCED_ACTS - ACTION_KEYS)

    def test_没有从未被引用的处理函数(self) -> None:
        """孤儿 handler 是重构没删干净的残留，会让人误以为界面上还有那个入口。"""

        assert not sorted(ACTION_KEYS - REFERENCED_ACTS)

    def test_每个_data_live_都有实时写入器(self) -> None:
        """漏了写入器的输入框最坏：字打进去了，点保存却发的是旧值。"""

        assert not sorted(REFERENCED_LIVE - LIVE_KEYS)

    def test_没有多余的实时写入器(self) -> None:
        assert not sorted(LIVE_KEYS - REFERENCED_LIVE)


class TestApiWiring:
    @pytest.mark.parametrize("endpoint", sorted(FRONTEND_ENDPOINTS))
    def test_前端调用的每个接口后端都注册了(self, endpoint: str) -> None:
        """前后端各写一次路径字符串，对不上就是一个只能在浏览器里发现的 404。"""

        assert endpoint in BACKEND_ROUTES
