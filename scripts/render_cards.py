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
* 「assets/logo.svg」 与 「pages/nexus/assets/logo.svg」 —— 矢量源，由
  「nexus/render/logo.py」 直接导出，避免两份手抄件漂移。

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
from typing import Any

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
#: 矢量 logo 的两个落点。README 引用前者，WebUI 页面引用后者，内容必须逐字一致，
#: 所以统一从 「nexus/render/logo.py」 生成，不手改。
LOGO_SVG_TARGETS = (
    PLUGIN_ROOT / "assets" / "logo.svg",
    PLUGIN_ROOT / "pages" / "nexus" / "assets" / "logo.svg",
)

TARGET_WIDTH = 1950
WEBP_QUALITY = 88
WEBP_METHOD = 6
#: 截图时的设备像素比。成图最终都会被缩到 「TARGET_WIDTH」，所以这里只需要比目标
#: 宽度略大一点就能拿到干净的抗锯齿；早先固定 2x 会让 Chromium 一次性合成
#: 3120x4300 的全页位图，内存吃紧的机器上直接报 「Unable to capture screenshot」。
RENDER_SCALE = 1.5
#: 降级阶梯：请求的缩放失败时按顺序往下退，保证构建脚本在小内存机器上也能跑完。
RENDER_SCALE_LADDER = (2.0, 1.5, 1.25, 1.0)
#: 视口高度只影响首屏，全页截图会自己扩展；给个够高的值免得触发懒加载分支。
VIEWPORT_HEIGHT = 1400
#: 截图前的静置时间，留给 webfont 和渐变绘制。
SETTLE_MS = 160
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
    """把 2x 截图缩到目标宽度再压成 WebP —— 体积比 PNG 小一个数量级。

    「reducing_gap」 让 Pillow 先用整数倍 「reduce()」 粗缩一遍再做 LANCZOS。
    帮助卡在 2x 下是 3000x4300 级别的大图，直接 LANCZOS 需要一次性吃下几百 MB
    浮点缓冲；本机在 Chromium 还开着的时候曾因此抛 「MemoryError」。粗缩一步既
    省内存又更快，肉眼画质无差别。
    """

    from PIL import Image

    with Image.open(io.BytesIO(png_bytes)) as raw:
        image = raw.convert("RGB")
    if target_width and image.width > target_width:
        height = max(1, round(image.height * target_width / image.width))
        image = image.resize((target_width, height), Image.LANCZOS, reducing_gap=2.0)
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination, format="WEBP", quality=WEBP_QUALITY, method=WEBP_METHOD)
    image.close()
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


def _sync_logo_svg() -> list[str]:
    """把 「LOGO_SVG」 写到所有矢量落点。

    早先这两份 svg 是手工复制的，改配色时漏掉一处就会出现 README 和 WebUI 里
    logo 不一样的尴尬情况。这里让构建脚本兜住，源头只有 「logo.py」 一处。
    """

    report: list[str] = []
    for target in LOGO_SVG_TARGETS:
        target.parent.mkdir(parents=True, exist_ok=True)
        # newline="\n" 是为了在 Windows 上也写出 LF，跟 「.gitattributes」 保持一致。
        with target.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(LOGO_SVG)
        label = target.relative_to(PLUGIN_ROOT).as_posix()
        report.append(f"{label}: {len(LOGO_SVG)} B")
    return report


def _scale_ladder(preferred: float) -> tuple[float, ...]:
    """把请求的缩放排在最前，后面接上比它更低的档位作为退路。"""

    lower = tuple(value for value in RENDER_SCALE_LADDER if value < preferred)
    return (preferred, *lower)


async def _bake_card(
    browser: Any,
    html: str,
    destination: Path,
    *,
    target_width: int,
    ladder: Sequence[float],
) -> tuple[int, float]:
    """截图并编码一张帮助卡，返回 「(字节数, 实际缩放)」。

    截图和编码放在同一个重试块里，是因为两处的失败原因是同一个——内存不够：
    Chromium 合成不出大位图会抛 「Error: Unable to capture screenshot」，Pillow
    缩放大图会抛 「MemoryError」。任一处炸了都降一档重来，比让整个构建挂掉好。
    每张卡片用独立 context，跑完就关，峰值内存也更低。
    """

    from playwright.async_api import Error as PlaywrightError

    last: BaseException | None = None
    for scale in ladder:
        context = await browser.new_context(
            viewport={"width": HELP_CARD_WIDTH, "height": VIEWPORT_HEIGHT},
            device_scale_factor=scale,
        )
        try:
            page = await context.new_page()
            await page.set_content(html, wait_until="load")
            await page.wait_for_timeout(SETTLE_MS)
            shot = await page.screenshot(type="png", full_page=True)
            return _encode_webp(shot, destination, target_width=target_width), scale
        except (MemoryError, PlaywrightError) as exc:
            last = exc
            print(f"  ! {destination.name} 在 {scale:g}x 下失败（{type(exc).__name__}），降档重试")
        finally:
            await context.close()
    message = f"{destination.name}: 所有缩放档位都渲染失败"
    raise RuntimeError(message) from last


async def _render(
    keys: Sequence[str],
    *,
    chromium: str | None,
    prefix: str,
    version: str,
    columns: int,
    target_width: int,
    scale: float,
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
            ladder = _scale_ladder(scale)
            for key in keys:
                theme = resolve_theme(key)
                html = build_help_card(
                    theme,
                    prefix=prefix,
                    version=version,
                    width=HELP_CARD_WIDTH,
                    columns=columns,
                )
                destination = CARD_DIR / f"help_{theme.key}.webp"
                size, used = await _bake_card(
                    browser,
                    html,
                    destination,
                    target_width=target_width,
                    ladder=ladder,
                )
                # 一旦降过档就别再往上试：六张卡的清晰度保持一致，看起来才像一套。
                ladder = _scale_ladder(used)
                report.append(f"{destination.name}: {size / 1024:.0f} KiB @{used:g}x")

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
    parser.add_argument(
        "--scale",
        type=float,
        default=RENDER_SCALE,
        help=f"截图设备像素比（默认 {RENDER_SCALE:g}；失败会自动降档）",
    )
    parser.add_argument(
        "--no-logo", action="store_true", help="跳过 logo.png / assets/logo.png / logo.svg"
    )
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
            scale=max(1.0, args.scale),
            logo=not args.no_logo,
        )
    )
    if not args.no_logo:
        report.extend(_sync_logo_svg())

    for line in report:
        print("  " + line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
