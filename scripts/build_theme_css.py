"""从 「nexus/render/themes.py」 生成 WebUI 样式表。

Copyright (C) 2026 Whereis-Alice and AstrBot Plugin Authors.
Licensed under the GNU Affero General Public License v3.0 or later.

WebUI 与卡片共用同一套配色 token，这样 Dashboard 截图和聊天里收到的卡片看起来
就是同一个产品。改完 「nexus/render/themes.py」 之后重新跑一次::

    python astrbot_plugin_bangumi_nexus/scripts/build_theme_css.py

之所以「生成」而不是手写 CSS：主题一共 6 套 × 28 个变量 = 168 行纯机械内容，
手写必然和 Python 侧漂移。生成物提交进仓库，运行期不需要 Python 再算一遍。
"""

from __future__ import annotations

import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT.parent))

from astrbot_plugin_bangumi_nexus.nexus.render.themes import (
    DEFAULT_THEME,
    THEMES,
    Theme,
)

OUTPUT = PLUGIN_ROOT / "pages" / "nexus" / "theme.css"
ATTRIBUTE = "data-nexus-theme"

HEADER = """/* ---------------------------------------------------------------------------
 * 自动生成，请勿手工编辑。
 * 来源: astrbot_plugin_bangumi_nexus/nexus/render/themes.py
 * 重建: python astrbot_plugin_bangumi_nexus/scripts/build_theme_css.py
 *
 * 每个主题都暴露与卡片渲染器完全相同的 28 个自定义属性，
 * 外加若干只对 WebUI 有意义的派生变量（滚动条、玻璃模糊、语义色等）。
 * ------------------------------------------------------------------------- */
"""


def extras(theme: Theme) -> dict[str, str]:
    """只在 WebUI 里有意义、静态卡片上没有对应概念的变量。"""

    dark = theme.is_dark
    return {
        "--mode": theme.mode,
        "--glass-blur": "20px" if theme.glass else "10px",
        "--rail": theme.palette.canvas_from if dark else theme.palette.surface_alt,
        "--track": "rgba(255, 255, 255, 0.10)" if dark else "rgba(15, 23, 42, 0.10)",
        "--hairline": "rgba(255, 255, 255, 0.06)" if dark else "rgba(15, 23, 42, 0.05)",
        "--lift": "rgba(255, 255, 255, 0.05)" if dark else "rgba(255, 255, 255, 0.72)",
        "--sunken": "rgba(0, 0, 0, 0.28)" if dark else "rgba(15, 23, 42, 0.045)",
        "--danger": "#ff7a86" if dark else "#d02c46",
        "--success": "#54e3ae" if dark else "#12855f",
        "--warn": "#ffc46b" if dark else "#a35c05",
    }


def block(selector: str, variables: dict[str, str], *, scheme: str | None = None) -> str:
    """把一组自定义属性渲染成一条 CSS 规则。"""

    lines = [f"{selector} {{"]
    lines.extend(f"  {name}: {value};" for name, value in variables.items())
    if scheme:
        lines.append(f"  color-scheme: {scheme};")
    lines.append("}")
    return "\n".join(lines)


def render() -> str:
    """产出整张样式表：「:root」 兜底 + 每个主题一条属性选择器规则。"""

    chunks = [HEADER]
    default = THEMES[DEFAULT_THEME]
    chunks.append("/* app.js 还没启动时的兜底，避免首屏白闪。 */")
    chunks.append(
        block(":root", {**default.css_variables(), **extras(default)}, scheme=default.mode)
    )
    for key, theme in THEMES.items():
        variables = {**theme.css_variables(), **extras(theme)}
        chunks.append(f"/* {theme.name} - {theme.tagline} */")
        chunks.append(block(f'[{ATTRIBUTE}="{key}"]', variables, scheme=theme.mode))
    return "\n\n".join(chunks) + "\n"


def main() -> int:
    css = render()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(css, encoding="utf-8")
    print(f"wrote {OUTPUT.relative_to(PLUGIN_ROOT)} ({len(css.encode('utf-8'))} bytes)")
    print(f"themes: {', '.join(THEMES)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
