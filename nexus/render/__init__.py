"""渲染层门面。

对外只暴露三样东西：主题、卡片构造器、渲染引擎。上层服务 import 这里，不直接
import 子模块，于是内部拆分（比如以后把 raster 换成别的实现）不会波及调用方。

Copyright (C) 2026 Whereis-Alice and AstrBot Plugin Authors.
Licensed under the GNU Affero General Public License v3.0 or later.
"""

from __future__ import annotations

from pathlib import Path

from ..constants import PLUGIN_ROOT
from .engine import CardEngine, CardRequest, RenderedCard, plain_lines
from .logo import LOGO_ICON_SVG, LOGO_MARK, LOGO_SVG
from .raster import PILLOW_AVAILABLE, RasterCard, Section, card_from_text, font_available
from .template import (
    CARD_WIDTH,
    HELP_CARD_WIDTH,
    build_anirss_card,
    build_calendar_card,
    build_diagnose_card,
    build_episode_card,
    build_feed_card,
    build_gacha_card,
    build_help_card,
    build_notice_card,
    build_picker_card,
    build_recommend_card,
    build_search_card,
    build_season_card,
    build_subject_card,
    build_today_card,
    build_watchlist_card,
    clip,
    esc,
    flatten,
)
from .themes import (
    DEFAULT_THEME,
    THEMES,
    Theme,
    resolve_theme,
    theme_keys,
    themes_payload,
)

# 预烘焙的帮助卡（scripts/render_cards.py 产出），Dashboard 与 README 直接引用，
# 免得为了看一眼指令表就启动一次浏览器。
ASSET_DIR = PLUGIN_ROOT / "assets" / "cards"


def asset_card_path(theme: str = DEFAULT_THEME) -> Path | None:
    """返回某主题的预烘焙帮助卡路径；不存在时返回 None。"""

    candidate = ASSET_DIR / f"help_{resolve_theme(theme).key}.webp"
    return candidate if candidate.exists() else None


def available_asset_themes() -> tuple[str, ...]:
    """哪些主题已经烘焙好了静态帮助卡。"""

    if not ASSET_DIR.is_dir():
        return ()
    found = []
    for key in theme_keys():
        if (ASSET_DIR / f"help_{key}.webp").exists():
            found.append(key)
    return tuple(found)


__all__ = [
    "ASSET_DIR",
    "CARD_WIDTH",
    "DEFAULT_THEME",
    "HELP_CARD_WIDTH",
    "LOGO_ICON_SVG",
    "LOGO_MARK",
    "LOGO_SVG",
    "PILLOW_AVAILABLE",
    "THEMES",
    "CardEngine",
    "CardRequest",
    "RasterCard",
    "RenderedCard",
    "Section",
    "Theme",
    "asset_card_path",
    "available_asset_themes",
    "build_anirss_card",
    "build_calendar_card",
    "build_diagnose_card",
    "build_episode_card",
    "build_feed_card",
    "build_gacha_card",
    "build_help_card",
    "build_notice_card",
    "build_picker_card",
    "build_recommend_card",
    "build_search_card",
    "build_season_card",
    "build_subject_card",
    "build_today_card",
    "build_watchlist_card",
    "card_from_text",
    "clip",
    "esc",
    "flatten",
    "font_available",
    "plain_lines",
    "resolve_theme",
    "theme_keys",
    "themes_payload",
]
