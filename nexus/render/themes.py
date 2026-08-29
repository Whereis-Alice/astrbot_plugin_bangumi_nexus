"""卡片与 WebUI 共用的视觉主题（design token）。

Copyright (C) 2026 Whereis-Alice and AstrBot Plugin Authors.
Licensed under the GNU Affero General Public License v3.0 or later.

每个主题都只由 「#RRGGBB」 / 「#RRGGBBAA」 这类纯色 token 描述，因此同一套配色可以
同时驱动三个完全不同的渲染后端：

* HTML 模板（浏览器 / AstrBot t2i 渲染）；
* Pillow 栅格兜底（无浏览器、无网络也能出图）；
* WebUI 样式表（CSS 自定义属性）。

版式本身住在 「template.py」 与 「raster.py」，主题只决定颜色、字体与表面质感 ——
所以新增一个主题就是在下面多加一个条目，不需要动任何布局代码。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

SANS_STACK = (
    '"Noto Sans SC", "PingFang SC", "Microsoft YaHei", "Source Han Sans SC", '
    '"Hiragino Sans GB", system-ui, -apple-system, "Segoe UI", sans-serif'
)
SERIF_STACK = (
    '"Noto Serif SC", "Source Han Serif SC", "Songti SC", "SimSun", '
    'Georgia, "Times New Roman", serif'
)
MONO_STACK = (
    '"JetBrains Mono", "Cascadia Code", "SF Mono", "Sarasa Mono SC", '
    'Consolas, "DejaVu Sans Mono", ui-monospace, monospace'
)


def hex_to_rgba(value: str) -> tuple[int, int, int, int]:
    """Parse 「#RGB」, 「#RRGGBB」 or 「#RRGGBBAA」 into an RGBA tuple."""

    text = (value or "").strip().lstrip("#")
    if len(text) == 3:
        text = "".join(char * 2 for char in text)
    if len(text) == 6:
        text += "ff"
    if len(text) != 8:
        raise ValueError(f"unsupported colour token: {value!r}")
    return tuple(int(text[index : index + 2], 16) for index in (0, 2, 4, 6))  # type: ignore[return-value]


def to_css(value: str) -> str:
    """Render a token as a CSS colour, using rgba() only when transparency is present."""

    red, green, blue, alpha = hex_to_rgba(value)
    if alpha >= 255:
        return f"#{red:02x}{green:02x}{blue:02x}"
    return f"rgba({red}, {green}, {blue}, {alpha / 255:.3f})"


def blend(foreground: str, background: str, alpha: float | None = None) -> str:
    """Flatten a translucent token over an opaque one (used by the raster renderer)."""

    fr, fg, fb, fa = hex_to_rgba(foreground)
    br, bg, bb, _ = hex_to_rgba(background)
    ratio = fa / 255 if alpha is None else max(0.0, min(1.0, alpha))
    mix = lambda front, back: round(front * ratio + back * (1 - ratio))  # noqa: E731
    return f"#{mix(fr, br):02x}{mix(fg, bg):02x}{mix(fb, bb):02x}ff"


@dataclass(frozen=True)
class Palette:
    """Colour tokens shared by all three renderers."""

    canvas_from: str
    canvas_to: str
    canvas_mid: str
    surface: str
    surface_alt: str
    border: str
    border_strong: str
    text: str
    muted: str
    faint: str
    accent: str
    accent_alt: str
    accent_ink: str
    chip: str
    chip_text: str
    code: str
    code_text: str


@dataclass(frozen=True)
class Theme:
    """One complete visual identity for the card and the WebUI."""

    key: str
    name: str
    tagline: str
    mode: str
    palette: Palette
    canvas_css: str
    surface_css: str
    hero_css: str
    overlay_css: str = ""
    body_font: str = SANS_STACK
    heading_font: str = SANS_STACK
    mono_font: str = MONO_STACK
    heading_spacing: str = "-0.01em"
    radius: int = 22
    glass: bool = False
    shadow: str = "0 24px 60px -30px rgba(0, 0, 0, 0.55)"
    accent_shadow: str = "0 10px 30px -12px rgba(0, 0, 0, 0.5)"
    keywords: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_dark(self) -> bool:
        return self.mode == "dark"

    def css_variables(self) -> dict[str, str]:
        """Expose every token as a CSS custom property for the WebUI."""

        palette = self.palette
        variables = {
            "--canvas": self.canvas_css,
            "--canvas-from": to_css(palette.canvas_from),
            "--canvas-mid": to_css(palette.canvas_mid),
            "--canvas-to": to_css(palette.canvas_to),
            "--surface": to_css(palette.surface),
            "--surface-alt": to_css(palette.surface_alt),
            "--surface-css": self.surface_css,
            "--hero": self.hero_css,
            "--overlay": self.overlay_css or "none",
            "--border": to_css(palette.border),
            "--border-strong": to_css(palette.border_strong),
            "--text": to_css(palette.text),
            "--muted": to_css(palette.muted),
            "--faint": to_css(palette.faint),
            "--accent": to_css(palette.accent),
            "--accent-alt": to_css(palette.accent_alt),
            "--accent-ink": to_css(palette.accent_ink),
            "--chip": to_css(palette.chip),
            "--chip-text": to_css(palette.chip_text),
            "--code": to_css(palette.code),
            "--code-text": to_css(palette.code_text),
            "--font-body": self.body_font,
            "--font-heading": self.heading_font,
            "--font-mono": self.mono_font,
            "--radius": f"{self.radius}px",
            "--shadow": self.shadow,
            "--accent-shadow": self.accent_shadow,
            "--heading-spacing": self.heading_spacing,
        }
        return variables

    def css_variable_block(self, indent: int | str = 6) -> str:
        """Render the palette as CSS custom properties, one per line.

        「indent」 accepts either a literal prefix string or a number of spaces.
        """

        pad = indent if isinstance(indent, str) else " " * max(0, int(indent))
        return "\n".join(f"{pad}{name}: {value};" for name, value in self.css_variables().items())

    def payload(self) -> dict[str, object]:
        """Serialise for the WebUI theme picker."""

        return {
            "key": self.key,
            "name": self.name,
            "tagline": self.tagline,
            "mode": self.mode,
            "swatches": [
                to_css(self.palette.accent),
                to_css(self.palette.accent_alt),
                to_css(self.palette.canvas_from),
                to_css(self.palette.surface_alt),
            ],
            "variables": self.css_variables(),
        }


MIDNIGHT = Theme(
    key="midnight",
    name="午夜霓虹",
    tagline="深藏青底色 · 青紫霓虹描边",
    mode="dark",
    palette=Palette(
        canvas_from="#070a16",
        canvas_mid="#0c1226",
        canvas_to="#111a33",
        surface="#141b30e6",
        surface_alt="#1b2440",
        border="#27314f",
        border_strong="#3a4670",
        text="#e9eefb",
        muted="#93a0c2",
        faint="#66718f",
        accent="#4de2ff",
        accent_alt="#b06bff",
        accent_ink="#04121c",
        chip="#4de2ff26",
        chip_text="#9ce9ff",
        code="#0a1122",
        code_text="#8fd8ff",
    ),
    canvas_css=(
        "radial-gradient(1200px 620px at 12% -8%, rgba(77, 226, 255, 0.20), transparent 62%),"
        " radial-gradient(1000px 640px at 92% 4%, rgba(176, 107, 255, 0.22), transparent 60%),"
        " linear-gradient(168deg, #070a16 0%, #0c1226 52%, #111a33 100%)"
    ),
    surface_css="linear-gradient(160deg, rgba(28, 38, 66, 0.92), rgba(16, 22, 41, 0.94))",
    hero_css="linear-gradient(115deg, rgba(77, 226, 255, 0.16), rgba(176, 107, 255, 0.16))",
    overlay_css=(
        "repeating-linear-gradient(115deg, rgba(255, 255, 255, 0.028) 0 1px, transparent 1px 4px)"
    ),
    keywords=("neon", "cyber", "dark"),
)

AURORA = Theme(
    key="aurora",
    name="极光",
    tagline="半透明玻璃层 · 薄荷与蓝的流光",
    mode="dark",
    palette=Palette(
        canvas_from="#04191c",
        canvas_mid="#08243c",
        canvas_to="#161244",
        surface="#ffffff12",
        surface_alt="#ffffff1c",
        border="#ffffff26",
        border_strong="#ffffff3d",
        text="#eafff8",
        muted="#a2c6c2",
        faint="#7d9c9c",
        accent="#63ffc9",
        accent_alt="#7fa9ff",
        accent_ink="#052018",
        chip="#63ffc924",
        chip_text="#bdffe8",
        code="#04121a99",
        code_text="#a6f2de",
    ),
    canvas_css=(
        "radial-gradient(900px 520px at 8% 0%, rgba(99, 255, 201, 0.26), transparent 60%),"
        " radial-gradient(880px 560px at 78% 6%, rgba(127, 169, 255, 0.28), transparent 58%),"
        " radial-gradient(760px 620px at 50% 108%, rgba(186, 128, 255, 0.20), transparent 62%),"
        " linear-gradient(170deg, #04191c 0%, #08243c 48%, #161244 100%)"
    ),
    surface_css="linear-gradient(155deg, rgba(255, 255, 255, 0.11), rgba(255, 255, 255, 0.045))",
    hero_css="linear-gradient(118deg, rgba(99, 255, 201, 0.18), rgba(127, 169, 255, 0.14))",
    overlay_css="",
    radius=26,
    glass=True,
    shadow="0 30px 70px -34px rgba(0, 20, 24, 0.75)",
    keywords=("glass", "aurora", "dark"),
)

SAKURA = Theme(
    key="sakura",
    name="樱绯",
    tagline="暖粉纸感 · 衬线标题的柔和秩序",
    mode="light",
    palette=Palette(
        canvas_from="#fff8f9",
        canvas_mid="#ffeef3",
        canvas_to="#fdf3ec",
        surface="#ffffff",
        surface_alt="#fff5f7",
        border="#f6dae1",
        border_strong="#efc2ce",
        text="#3d2b31",
        muted="#8d6c76",
        faint="#b39aa2",
        accent="#e4547d",
        accent_alt="#ffab86",
        accent_ink="#ffffff",
        chip="#e4547d1f",
        chip_text="#b83f63",
        code="#fff1f4",
        code_text="#a44366",
    ),
    canvas_css=(
        "radial-gradient(900px 480px at 14% -6%, rgba(228, 84, 125, 0.14), transparent 60%),"
        " radial-gradient(820px 520px at 88% 2%, rgba(255, 171, 134, 0.20), transparent 58%),"
        " linear-gradient(172deg, #fff8f9 0%, #ffeef3 55%, #fdf3ec 100%)"
    ),
    surface_css="linear-gradient(158deg, #ffffff, #fff7f9)",
    hero_css="linear-gradient(116deg, rgba(228, 84, 125, 0.12), rgba(255, 171, 134, 0.18))",
    overlay_css="",
    heading_font=SERIF_STACK,
    heading_spacing="0.005em",
    radius=20,
    shadow="0 22px 50px -30px rgba(180, 108, 128, 0.45)",
    accent_shadow="0 10px 26px -12px rgba(228, 84, 125, 0.45)",
    keywords=("light", "warm", "serif"),
)

BLUEPRINT = Theme(
    key="blueprint",
    name="蓝图",
    tagline="工程网格 · 等宽标注的技术感",
    mode="dark",
    palette=Palette(
        canvas_from="#062138",
        canvas_mid="#082a46",
        canvas_to="#04182a",
        surface="#ffffff0d",
        surface_alt="#ffffff16",
        border="#7fc4ff3d",
        border_strong="#7fc4ff5c",
        text="#dcecfb",
        muted="#93b7d6",
        faint="#6d90ae",
        accent="#ffd166",
        accent_alt="#6ec8ff",
        accent_ink="#0a1a28",
        chip="#ffd1661f",
        chip_text="#ffe3a3",
        code="#03121f99",
        code_text="#8fd0ff",
    ),
    canvas_css="linear-gradient(168deg, #062138 0%, #082a46 55%, #04182a 100%)",
    surface_css="linear-gradient(150deg, rgba(255, 255, 255, 0.075), rgba(255, 255, 255, 0.035))",
    hero_css="linear-gradient(118deg, rgba(255, 209, 102, 0.14), rgba(110, 200, 255, 0.14))",
    overlay_css=(
        "linear-gradient(rgba(146, 200, 255, 0.075) 1px, transparent 1px) 0 0 / 100% 34px,"
        " linear-gradient(90deg, rgba(146, 200, 255, 0.075) 1px, transparent 1px) 0 0 / 34px 100%"
    ),
    body_font=MONO_STACK,
    heading_font=MONO_STACK,
    heading_spacing="0.04em",
    radius=8,
    shadow="0 20px 46px -28px rgba(0, 12, 24, 0.8)",
    keywords=("technical", "grid", "mono"),
)

PAPER = Theme(
    key="paper",
    name="素笺",
    tagline="米白纸张 · 墨黑与朱红的编排",
    mode="light",
    palette=Palette(
        canvas_from="#f7f4ed",
        canvas_mid="#f2eee4",
        canvas_to="#eae5d8",
        surface="#fffdf8",
        surface_alt="#f7f3e9",
        border="#ded7c6",
        border_strong="#c8bfa9",
        text="#221f1a",
        muted="#726b5d",
        faint="#9c9484",
        accent="#b0402c",
        accent_alt="#2f4858",
        accent_ink="#fffdf8",
        chip="#b0402c1a",
        chip_text="#8f3323",
        code="#f2eee2",
        code_text="#4a4438",
    ),
    canvas_css=(
        "radial-gradient(1000px 520px at 50% -12%, rgba(255, 255, 255, 0.9), transparent 60%),"
        " linear-gradient(174deg, #f7f4ed 0%, #f2eee4 58%, #eae5d8 100%)"
    ),
    surface_css="linear-gradient(160deg, #fffdf8, #faf7ee)",
    hero_css="linear-gradient(120deg, rgba(176, 64, 44, 0.10), rgba(47, 72, 88, 0.08))",
    overlay_css="",
    heading_font=SERIF_STACK,
    heading_spacing="0.01em",
    radius=6,
    shadow="0 18px 40px -28px rgba(80, 70, 50, 0.4)",
    accent_shadow="0 8px 22px -12px rgba(176, 64, 44, 0.38)",
    keywords=("editorial", "paper", "serif"),
)

SUNSET = Theme(
    key="sunset",
    name="落日",
    tagline="炭灰暖底 · 珊瑚与琥珀的渐层",
    mode="dark",
    palette=Palette(
        canvas_from="#1d1219",
        canvas_mid="#2c1a24",
        canvas_to="#3a2028",
        surface="#2a1c24e6",
        surface_alt="#35232c",
        border="#4a3038",
        border_strong="#6a434c",
        text="#fdeee6",
        muted="#c8a49c",
        faint="#9d7d78",
        accent="#ff8a5c",
        accent_alt="#ffc46b",
        accent_ink="#2a1109",
        chip="#ff8a5c24",
        chip_text="#ffc4a8",
        code="#1a1013",
        code_text="#ffb894",
    ),
    canvas_css=(
        "radial-gradient(1000px 560px at 88% -10%, rgba(255, 138, 92, 0.30), transparent 62%),"
        " radial-gradient(900px 520px at 6% 8%, rgba(255, 196, 107, 0.18), transparent 58%),"
        " linear-gradient(166deg, #1d1219 0%, #2c1a24 52%, #3a2028 100%)"
    ),
    surface_css="linear-gradient(158deg, rgba(60, 40, 50, 0.94), rgba(36, 24, 31, 0.94))",
    hero_css="linear-gradient(116deg, rgba(255, 138, 92, 0.20), rgba(255, 196, 107, 0.16))",
    overlay_css=(
        "repeating-linear-gradient(0deg, rgba(255, 255, 255, 0.022) 0 1px, transparent 1px 3px)"
    ),
    radius=18,
    shadow="0 26px 60px -32px rgba(24, 8, 12, 0.8)",
    keywords=("warm", "sunset", "dark"),
)

THEMES: dict[str, Theme] = {
    theme.key: theme for theme in (MIDNIGHT, AURORA, SAKURA, BLUEPRINT, PAPER, SUNSET)
}
DEFAULT_THEME = "midnight"

THEME_ALIASES: dict[str, str] = {
    "午夜": "midnight",
    "午夜霓虹": "midnight",
    "霓虹": "midnight",
    "neon": "midnight",
    "极光": "aurora",
    "玻璃": "aurora",
    "glass": "aurora",
    "樱": "sakura",
    "樱绯": "sakura",
    "樱花": "sakura",
    "粉": "sakura",
    "蓝图": "blueprint",
    "工程": "blueprint",
    "grid": "blueprint",
    "素笺": "paper",
    "纸": "paper",
    "纸张": "paper",
    "落日": "sunset",
    "夕阳": "sunset",
    "暖": "sunset",
}


def theme_keys() -> tuple[str, ...]:
    return tuple(THEMES)


def resolve_theme(name: str | None) -> Theme:
    """Look a theme up by key, Chinese alias or display name; never raises."""

    if not name:
        return THEMES[DEFAULT_THEME]
    token = str(name).strip()
    lowered = token.lower()
    if lowered in THEMES:
        return THEMES[lowered]
    if token in THEME_ALIASES:
        return THEMES[THEME_ALIASES[token]]
    if lowered in THEME_ALIASES:
        return THEMES[THEME_ALIASES[lowered]]
    for theme in THEMES.values():
        if token == theme.name or token in theme.keywords:
            return theme
    return THEMES[DEFAULT_THEME]


def themes_payload(keys: Iterable[str] | None = None) -> list[dict[str, object]]:
    selected = list(keys) if keys else list(THEMES)
    return [THEMES[key].payload() for key in selected if key in THEMES]
