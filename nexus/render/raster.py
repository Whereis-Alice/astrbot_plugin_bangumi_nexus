"""Pillow 栅格兜底渲染。

浏览器不可用（没装 Playwright、t2i 端点挂了、容器里没有 Chromium）时，卡片仍然要
能出图 —— 否则用户看到的就是一段裸文本。这里用 Pillow 画一张「同主题、同气质」的
简化卡：渐变底、圆角面板、hero 带、分节标题、正文行、页脚。

刻意不重复 「template.py」 的十种版式：那样等于维护两套排版。约定是调用方把卡片内容
降解成一段带轻标记的纯文本（「## 小节标题」 起一节，「- 」 起一行列表），本模块按这
个约定排版。于是新增一种卡片时，兜底渲染不需要改任何代码。

Copyright (C) 2026 Whereis-Alice and AstrBot Plugin Authors.
Licensed under the GNU Affero General Public License v3.0 or later.
"""

from __future__ import annotations

import io
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .themes import Theme, blend, hex_to_rgba, resolve_theme

try:  # pragma: no cover - 环境相关
    from PIL import Image, ImageDraw, ImageFont

    PILLOW_AVAILABLE = True
except Exception:  # noqa: BLE001 # pragma: no cover - Pillow 缺失时降级为纯文本
    Image = ImageDraw = ImageFont = None  # type: ignore[assignment]
    PILLOW_AVAILABLE = False


# ---------------------------------------------------------------------------
# 字体
# ---------------------------------------------------------------------------

_FONT_CANDIDATES: tuple[tuple[str, ...], ...] = (
    # Windows
    ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyhbd.ttc"),
    ("C:/Windows/Fonts/simhei.ttf", "C:/Windows/Fonts/simhei.ttf"),
    # macOS
    ("/System/Library/Fonts/PingFang.ttc", "/System/Library/Fonts/PingFang.ttc"),
    # Linux 常见发行版
    (
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    ),
    (
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    ),
    (
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
    ),
    (
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    ),
    (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ),
)

_ENV_FONT = "BANGUMI_NEXUS_FONT"
_font_cache: dict[tuple[str, int], object] = {}
_family: tuple[str, str] | None = None


def _resolve_family() -> tuple[str, str]:
    """挑一对（常规体, 粗体）字体路径。允许用环境变量强制指定。"""

    global _family
    if _family is not None:
        return _family
    override = os.environ.get(_ENV_FONT, "").strip()
    if override and Path(override).exists():
        _family = (override, override)
        return _family
    for regular, bold in _FONT_CANDIDATES:
        if Path(regular).exists():
            _family = (regular, bold if Path(bold).exists() else regular)
            return _family
    _family = ("", "")
    return _family


def _font(size: int, *, bold: bool = False):
    """带缓存的字体加载。任何失败都退回 Pillow 内置位图字体。"""

    regular, bold_path = _resolve_family()
    path = (bold_path if bold else regular) or ""
    key = (path, size)
    cached = _font_cache.get(key)
    if cached is not None:
        return cached
    font = None
    if path and ImageFont is not None:
        try:
            font = ImageFont.truetype(path, size)
        except Exception:  # noqa: BLE001 - 字体损坏也不该让卡片渲染整体失败
            font = None
    if font is None and ImageFont is not None:
        try:
            font = ImageFont.load_default(size)
        except Exception:  # noqa: BLE001 - 老版本 Pillow 的 load_default 不收 size
            font = ImageFont.load_default()
    _font_cache[key] = font
    return font


def font_available() -> bool:
    """是否找到了真正的 TrueType 字体（没找到时中文会变成方块）。"""

    return bool(_resolve_family()[0])


# ---------------------------------------------------------------------------
# 内容模型
# ---------------------------------------------------------------------------


@dataclass
class Section:
    heading: str = ""
    lines: tuple[str, ...] = ()


@dataclass
class RasterCard:
    """兜底卡片的内容。字段刻意和 HTML hero 区一一对应，视觉才不会割裂。"""

    title: str
    eyebrow: str = ""
    subtitle: str = ""
    chips: tuple[str, ...] = ()
    sections: tuple[Section, ...] = ()
    footer: str = "番剧中枢 Bangumi Nexus"
    stats: tuple[tuple[str, str], ...] = ()
    extras: dict[str, object] = field(default_factory=dict)


def parse_markup(text: str) -> tuple[Section, ...]:
    """把带轻标记的纯文本解析成小节。

    约定：「## 标题」 开一个新小节，其余行归入当前小节；连续空行折叠成一个。
    """

    sections: list[Section] = []
    heading = ""
    buffer: list[str] = []

    def flush() -> None:
        nonlocal heading, buffer
        if heading or buffer:
            sections.append(Section(heading, tuple(buffer)))
        heading, buffer = "", []

    for raw in str(text or "").splitlines():
        line = raw.rstrip()
        if line.startswith("## "):
            flush()
            heading = line[3:].strip()
            continue
        if not line.strip():
            if buffer and buffer[-1]:
                buffer.append("")
            continue
        buffer.append(line.strip())
    flush()
    return tuple(sections)


def card_from_text(
    title: str,
    text: str,
    *,
    eyebrow: str = "",
    subtitle: str = "",
    chips: Iterable[str] = (),
    footer: str = "番剧中枢 Bangumi Nexus",
) -> RasterCard:
    """便捷构造：直接吃「纯文本兜底文案」。"""

    return RasterCard(
        title=title,
        eyebrow=eyebrow,
        subtitle=subtitle,
        chips=tuple(chip for chip in chips if str(chip or "").strip()),
        sections=parse_markup(text),
        footer=footer,
    )


# ---------------------------------------------------------------------------
# 绘图工具
# ---------------------------------------------------------------------------

_PAD = 30
_SHEET_PAD_X = 28
_HERO_TOP = 24


def _rgb(token: str) -> tuple[int, int, int, int]:
    return hex_to_rgba(token)


def _flat(token: str, base: str) -> tuple[int, int, int, int]:
    """把带透明度的 token 压平到不透明底色上，Pillow 不做合成更省事。"""

    return hex_to_rgba(blend(token, base))


def _vertical_gradient(size: tuple[int, int], stops: Sequence[str]) -> Image.Image:
    """竖向多停靠点渐变。用 1px 宽的条带再拉伸，比逐像素快得多。"""

    width, height = size
    strip = Image.new("RGB", (1, max(2, height)))
    pixels = strip.load()
    colours = [hex_to_rgba(stop)[:3] for stop in stops] or [(20, 20, 30)]
    if len(colours) == 1:
        colours = colours * 2
    segments = len(colours) - 1
    for y in range(strip.height):
        position = y / max(1, strip.height - 1) * segments
        index = min(segments - 1, int(position))
        ratio = position - index
        start, end = colours[index], colours[index + 1]
        pixels[0, y] = tuple(  # type: ignore[index]
            round(start[channel] + (end[channel] - start[channel]) * ratio) for channel in range(3)
        )
    return strip.resize((width, height), Image.BILINEAR)


def _wrap(text: str, font, max_width: int) -> list[str]:
    """逐字符换行。中文没有空格，按词换行会整段溢出。"""

    words = str(text or "")
    if not words:
        return [""]
    lines: list[str] = []
    current = ""
    for char in words:
        candidate = current + char
        if _measure(candidate, font) <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = char
    if current:
        lines.append(current)
    return lines


_measure_cache: dict[tuple[int, str], int] = {}


def _measure(text: str, font) -> int:
    key = (id(font), text)
    cached = _measure_cache.get(key)
    if cached is not None:
        return cached
    try:
        width = int(font.getlength(text))
    except Exception:  # noqa: BLE001 - 量不出宽度就用等宽估算兜底
        width = len(text) * 8
    if len(_measure_cache) > 40000:
        _measure_cache.clear()
    _measure_cache[key] = width
    return width


def _ellipsize(text: str, font, max_width: int) -> str:
    if _measure(text, font) <= max_width:
        return text
    tail = "\u2026"
    budget = max_width - _measure(tail, font)
    out = ""
    for char in text:
        if _measure(out + char, font) > budget:
            break
        out += char
    return out + tail


# ---------------------------------------------------------------------------
# 排版：先量后画
# ---------------------------------------------------------------------------


@dataclass
class _Op:
    kind: str
    y: int = 0
    height: int = 0
    text: str = ""
    payload: tuple = ()


def _layout(
    card: RasterCard,
    width: int,
    fonts: dict[str, object],
    unit,
) -> tuple[list[_Op], int]:
    """把卡片内容排成一串绘图指令，同时算出总高度。

    「unit」 把逻辑像素换算成实际像素（超采样倍率），字号和行距必须走同一个换算，
    否则放大后文字会互相压行。
    """

    inner = width - 2 * unit(_PAD) - 2 * unit(_SHEET_PAD_X)
    ops: list[_Op] = []
    y = unit(_HERO_TOP)

    if card.eyebrow:
        ops.append(_Op("eyebrow", y, unit(20), card.eyebrow.upper()))
        y += unit(24)
    for line in _wrap(card.title, fonts["title"], inner)[:2]:
        ops.append(_Op("title", y, unit(40), line))
        y += unit(40)
    if card.subtitle:
        for line in _wrap(card.subtitle, fonts["sub"], inner)[:2]:
            ops.append(_Op("sub", y, unit(26), line))
            y += unit(26)
    if card.chips:
        y += unit(6)
        for row in _chip_rows(card.chips, fonts["chip"], inner, unit):
            ops.append(_Op("chips", y, unit(30), "", tuple(row)))
            y += unit(32)
    y += unit(16)
    hero_height = y
    ops.append(_Op("hero-rule", y, 1))
    y += unit(22)

    for section in card.sections:
        if section.heading:
            ops.append(_Op("heading", y, unit(28), section.heading))
            y += unit(34)
        for line in section.lines:
            if not line:
                y += unit(8)
                continue
            bullet = line.startswith(("- ", "\u2022 "))
            content = line[2:].strip() if bullet else line
            indent = unit(18) if bullet else 0
            for order, part in enumerate(_wrap(content, fonts["body"], inner - indent)):
                kind = "bullet" if bullet and order == 0 else "body"
                ops.append(_Op(kind, y, unit(26), part, (indent,)))
                y += unit(26)
        y += unit(12)

    y += unit(6)
    ops.append(_Op("footer-rule", y, 1))
    y += unit(20)
    ops.append(_Op("footer", y, unit(24), card.footer))
    y += unit(30)

    ops.insert(0, _Op("hero-bg", 0, hero_height))
    return ops, y + unit(_PAD)


def _chip_rows(chips: Sequence[str], font, max_width: int, unit) -> list[list[tuple[str, int]]]:
    """把 chip 按宽度折行，返回每行 (文本, 宽度)。"""

    rows: list[list[tuple[str, int]]] = [[]]
    used = 0
    gap = unit(7)
    for chip in chips:
        text = str(chip)
        chip_width = _measure(text, font) + unit(24)
        if used + chip_width > max_width and rows[-1]:
            rows.append([])
            used = 0
        rows[-1].append((text, chip_width))
        used += chip_width + gap
    return [row for row in rows if row]


def render(
    card: RasterCard,
    theme: Theme | str = "midnight",
    *,
    width: int = 880,
    scale: float = 2.0,
) -> bytes:
    """把卡片画成 PNG 字节流。Pillow 缺失时抛 「RuntimeError」。"""

    if not PILLOW_AVAILABLE:
        raise RuntimeError("Pillow 未安装，无法使用栅格兜底渲染")
    resolved = theme if isinstance(theme, Theme) else resolve_theme(theme)
    factor = max(1.0, min(3.0, float(scale)))
    logical_width = max(560, min(1600, int(width)))
    canvas_width = int(logical_width * factor)

    def size(base: int) -> int:
        return max(8, round(base * factor))

    fonts = {
        "eyebrow": _font(size(12), bold=True),
        "title": _font(size(30), bold=True),
        "sub": _font(size(15)),
        "chip": _font(size(13)),
        "heading": _font(size(18), bold=True),
        "body": _font(size(15)),
        "footer": _font(size(12)),
    }
    ops, total_height = _layout(card, canvas_width, fonts, size)

    palette = resolved.palette
    base = palette.canvas_from
    canvas = _vertical_gradient(
        (canvas_width, total_height),
        (palette.canvas_from, palette.canvas_mid, palette.canvas_to),
    ).convert("RGBA")
    draw = ImageDraw.Draw(canvas)

    pad = size(_PAD)  # 与 _layout 使用同一换算
    sheet_box = (pad, pad, canvas_width - pad, total_height - pad)
    radius = size(resolved.radius)
    draw.rounded_rectangle(
        sheet_box,
        radius=radius,
        fill=_flat(palette.surface, base),
        outline=_flat(palette.border, base),
        width=max(1, size(1)),
    )

    left = pad + size(_SHEET_PAD_X)
    right = canvas_width - pad - size(_SHEET_PAD_X)
    text_colour = _rgb(palette.text)
    muted = _rgb(palette.muted)
    faint = _rgb(palette.faint)
    accent = _rgb(palette.accent)
    chip_fill = _flat(palette.chip, blend(palette.surface, base))
    chip_text = _rgb(palette.chip_text)
    border = _flat(palette.border, blend(palette.surface, base))

    for op in ops:
        top = pad + op.y
        if op.kind == "hero-bg":
            hero_bottom = pad + op.height
            draw.rounded_rectangle(
                (pad, pad, canvas_width - pad, hero_bottom),
                radius=radius,
                fill=_flat(palette.surface_alt, base),
            )
            draw.rectangle(
                (pad, hero_bottom - radius, canvas_width - pad, hero_bottom),
                fill=_flat(palette.surface_alt, base),
            )
            continue
        if op.kind in {"hero-rule", "footer-rule"}:
            draw.rectangle((left, top, right, top + max(1, size(1) - 1)), fill=border)
            continue
        if op.kind == "eyebrow":
            draw.text((left, top), _spaced(op.text), font=fonts["eyebrow"], fill=accent)
            continue
        if op.kind == "title":
            draw.text((left, top), op.text, font=fonts["title"], fill=text_colour)
            continue
        if op.kind == "sub":
            draw.text((left, top), op.text, font=fonts["sub"], fill=muted)
            continue
        if op.kind == "chips":
            x = left
            for text, chip_width in op.payload:
                draw.rounded_rectangle(
                    (x, top, x + chip_width, top + size(26)),
                    radius=size(13),
                    fill=chip_fill,
                    outline=border,
                    width=1,
                )
                draw.text((x + size(12), top + size(5)), text, font=fonts["chip"], fill=chip_text)
                x += chip_width + size(7)
            continue
        if op.kind == "heading":
            draw.text((left, top), op.text, font=fonts["heading"], fill=text_colour)
            head_width = _measure(op.text, fonts["heading"])
            rule_left = left + head_width + size(12)
            mid = top + size(12)
            if rule_left < right:
                draw.rectangle((rule_left, mid, right, mid + max(1, size(1) - 1)), fill=border)
            continue
        if op.kind == "bullet":
            indent = op.payload[0] if op.payload else 0
            dot = size(4)
            centre = top + size(10)
            draw.ellipse(
                (left + size(4), centre - dot // 2, left + size(4) + dot, centre + dot // 2),
                fill=accent,
            )
            draw.text((left + indent, top), op.text, font=fonts["body"], fill=text_colour)
            continue
        if op.kind == "body":
            indent = op.payload[0] if op.payload else 0
            draw.text((left + indent, top), op.text, font=fonts["body"], fill=muted)
            continue
        if op.kind == "footer":
            draw.text((left, top), op.text, font=fonts["footer"], fill=faint)
            stamp = "BANGUMI NEXUS"
            stamp_width = _measure(stamp, fonts["footer"])
            draw.text((right - stamp_width, top), stamp, font=fonts["footer"], fill=faint)
            continue

    # 顶部一条强调色细线，和 HTML 卡 hero 的渐变呼应
    draw.rectangle((pad + radius, pad, canvas_width - pad - radius, pad + size(3)), fill=accent)

    buffer = io.BytesIO()
    canvas.convert("RGB").save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _spaced(text: str) -> str:
    """给 eyebrow 加字距。Pillow 不支持 letter-spacing，只能插空格。"""

    return " ".join(str(text or ""))


def render_text(
    title: str,
    text: str,
    theme: Theme | str = "midnight",
    *,
    eyebrow: str = "",
    subtitle: str = "",
    chips: Iterable[str] = (),
    width: int = 880,
) -> bytes:
    """最常用的入口：纯文本 + 主题 → PNG 字节流。"""

    card = card_from_text(title, text, eyebrow=eyebrow, subtitle=subtitle, chips=chips)
    return render(card, theme, width=width)


__all__ = [
    "PILLOW_AVAILABLE",
    "RasterCard",
    "Section",
    "card_from_text",
    "font_available",
    "parse_markup",
    "render",
    "render_text",
]
