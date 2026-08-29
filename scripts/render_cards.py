"""构建脚本：预渲染帮助卡与 logo 位图资源。

Copyright (C) 2026 Whereis-Alice and AstrBot Plugin Authors.
Licensed under the GNU Affero General Public License v3.0 or later.

在仓库的父目录里运行，这样插件包才 importable::

    python -m astrbot_plugin_bangumi_nexus.scripts.render_cards

或者直接::

    python astrbot_plugin_bangumi_nexus/scripts/render_cards.py

产物
----
* 「assets/cards/help_<theme>.webp」 —— 每个主题一张帮助卡。运行期 「/番剧中枢」
  会优先直接发这张烘焙好的图，省掉一次浏览器渲染（冷启动能快 2~3 秒）。
* 「assets/logo.png」 —— 透明背景位图，给 README / 插件市场用。
* 「logo.png」（插件根目录）—— 满幅不透明图标。AstrBot 只认
  「<plugin_dir>/logo.png」 这一个路径，Dashboard 插件卡显示的就是它。

Chromium 通过 Playwright 驱动。由于自带的 headless-shell 经常和已安装的
Playwright 版本不匹配，可以用 「--chromium」 或环境变量
「BANGUMI_NEXUS_CHROMIUM」 显式指定浏览器；否则先尽力扫一遍本地 Playwright
缓存，再退回 Playwright 自己的解析逻辑。
"""

from __future__ import annotations

import argparse
import asyncio
import io
import os
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT.parent))

from astrbot_plugin_bangumi_nexus.nexus.render import (
    HELP_CARD_WIDTH,
    LOGO_ICON_SVG,
    LOGO_SVG,
    build_help_card,
    resolve_theme,
    theme_keys,
)

CARD_DIR = PLUGIN_ROOT / "assets" / "cards"
LOGO_PNG = PLUGIN_ROOT / "assets" / "logo.png"
#: AstrBot 解析插件 logo 时只看 「<plugin_dir>/logo.png」，别的路径都不认。
ICON_PNG = PLUGIN_ROOT / "logo.png"

TARGET_WIDTH = 1950
WEBP_QUALITY = 88
WEBP_METHOD = 6
RENDER_SCALE = 2
LOGO_SIZE = 512

CHROMIUM_ENV = "BANGUMI_NEXUS_CHROMIUM"


def _probe_chromium() -> str | None:
    """在本地 Playwright 缓存里找一个完整的 Chromium 构建。

    只挑目录名不含 「headless」 的，因为 headless-shell 缺字体渲染栈，
    中文卡片会大面积豆腐块。同名多版本时取 revision 最大的那个。
    """

    explicit = os.environ.get(CHROMIUM_ENV)
    if explicit and Path(explicit).exists():
        return explicit

    roots: list[Path] = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(Path(local) / "ms-playwright")
    home = Path.home()
    roots.append(home / "AppData" / "Local" / "ms-playwright")
    roots.append(home / ".cache" / "ms-playwright")
    roots.append(Path("/ms-playwright"))

    relatives = (
        Path("chrome-win64") / "chrome.exe",
        Path("chrome-win") / "chrome.exe",
        Path("chrome-linux") / "chrome",
        Path("Chromium.app") / "Contents" / "MacOS" / "Chromium",
    )

    candidates: list[tuple[int, Path]] = []
    for root in roots:
        if not root.is_dir():
            continue
        for entry in root.iterdir():
            name = entry.name
            if not name.startswith("chromium-") or "headless" in name:
                continue
            suffix = name.split("-", 1)[1]
            revision = int(suffix) if suffix.isdigit() else 0
            for relative in relatives:
                binary = entry / relative
                if binary.exists():
                    candidates.append((revision, binary))
                    break
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return str(candidates[0][1])


def _encode_webp(png_bytes: bytes, destination: Path, *, target_width: int) -> int:
    """把 2x 截图缩到目标宽度再压成 WebP —— 体积比 PNG 小一个数量级。"""

    from PIL import Image

    with Image.open(io.BytesIO(png_bytes)) as raw:
        image = raw.convert("RGB")
    if target_width and image.width > target_width:
        height = max(1, round(image.height * target_width / image.width))
        image = image.resize((target_width, height), Image.LANCZOS)
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination, format="WEBP", quality=WEBP_QUALITY, method=WEBP_METHOD)
    return destination.stat().st_size


def _logo_html(svg: str, size: int) -> str:
    """把 SVG 塞进一个零边距页面，方便按精确像素截图。"""

    body = svg.strip()
    return (
        '<!doctype html><html><head><meta charset="utf-8"><style>'
        "html,body{margin:0;padding:0;background:transparent;}"
        f"body{{width:{size}px;height:{size}px;}}"
        f"svg{{display:block;width:{size}px;height:{size}px;}}"
        "</style></head><body>" + body + "</body></html>"
    )


async def _render(
    keys: Sequence[str],
    *,
    chromium: str | None,
    prefix: str,
    version: str,
    columns: int,
    target_width: int,
    logo: bool,
) -> list[str]:
    """一次启动浏览器，串行渲染所有卡片和 logo。"""

    from playwright.async_api import async_playwright

    report: list[str] = []
    launch_kwargs: dict[str, object] = {"args": ["--force-color-profile=srgb"]}
    if chromium:
        launch_kwargs["executable_path"] = chromium

    async with async_playwright() as driver:
        browser = await driver.chromium.launch(**launch_kwargs)
        try:
            context = await browser.new_context(
                viewport={"width": HELP_CARD_WIDTH, "height": 1400},
                device_scale_factor=RENDER_SCALE,
            )
            page = await context.new_page()
            for key in keys:
                theme = resolve_theme(key)
                html = build_help_card(
                    theme,
                    prefix=prefix,
                    version=version,
                    width=HELP_CARD_WIDTH,
                    columns=columns,
                )
                await page.set_content(html, wait_until="load")
                await page.wait_for_timeout(160)
                shot = await page.screenshot(type="png", full_page=True)
                destination = CARD_DIR / f"help_{theme.key}.webp"
                size = _encode_webp(shot, destination, target_width=target_width)
                report.append(f"{destination.name}: {size / 1024:.0f} KiB")
            await context.close()

            if logo:
                logo_context = await browser.new_context(
                    viewport={"width": LOGO_SIZE, "height": LOGO_SIZE},
                    device_scale_factor=1,
                )
                logo_page = await logo_context.new_page()
                targets = ((LOGO_PNG, LOGO_SVG, True), (ICON_PNG, LOGO_ICON_SVG, False))
                for destination, svg, transparent in targets:
                    await logo_page.set_content(_logo_html(svg, LOGO_SIZE), wait_until="load")
                    await logo_page.wait_for_timeout(120)
                    shot = await logo_page.screenshot(type="png", omit_background=transparent)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(shot)
                    label = destination.relative_to(PLUGIN_ROOT).as_posix()
                    report.append(f"{label}: {len(shot) / 1024:.0f} KiB")
                await logo_context.close()
        finally:
            await browser.close()
    return report


def _resolve_keys(requested: Iterable[str] | None) -> list[str]:
    """把命令行给的主题名（可能是别名）解析成去重后的主题 key 列表。"""

    if not requested:
        return list(theme_keys())
    resolved: list[str] = []
    for name in requested:
        key = resolve_theme(name).key
        if key not in resolved:
            resolved.append(key)
    return resolved


def _default_version() -> str:
    """从 「metadata.yaml」 读版本号，避免卡片上印着过期版本。"""

    try:
        text = (PLUGIN_ROOT / "metadata.yaml").read_text(encoding="utf-8")
    except OSError:
        return "v0.0.0"
    for line in text.splitlines():
        if not line.startswith("version:"):
            continue
        value = line.split(":", 1)[1].strip().strip("\"'")
        if value:
            return value
    return "v0.0.0"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="渲染番剧中枢的帮助卡与 logo。")
    parser.add_argument("themes", nargs="*", help="主题 key（默认全部）")
    parser.add_argument("--chromium", default=None, help="Chromium 可执行文件路径")
    parser.add_argument("--prefix", default="/", help="卡片上展示的指令前缀")
    parser.add_argument("--version", default=_default_version(), help="版本号文案")
    parser.add_argument("--columns", type=int, default=3, help="帮助卡列数")
    parser.add_argument(
        "--width",
        type=int,
        default=TARGET_WIDTH,
        help="输出宽度（像素，0 表示保留原始渲染尺寸）",
    )
    parser.add_argument("--no-logo", action="store_true", help="跳过 logo.png / assets/logo.png")
    args = parser.parse_args(argv)

    chromium = args.chromium or _probe_chromium()
    print(f"chromium: {chromium}" if chromium else "chromium: (playwright default)")

    keys = _resolve_keys(args.themes)
    print("themes: " + ", ".join(keys))

    report = asyncio.run(
        _render(
            keys,
            chromium=chromium,
            prefix=args.prefix,
            version=args.version,
            columns=max(1, args.columns),
            target_width=max(0, args.width),
            logo=not args.no_logo,
        )
    )
    for line in report:
        print("  " + line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
