"""渲染引擎：一条从「最好看」到「一定能出」的回退链。

同一张卡有四种可能的出图方式，可用性依次递减、观感依次递减：

1. 「html」  —— AstrBot 的 「html_render」（Playwright / 远端 t2i），完整 CSS 支持，最好看；
2. 「raster」—— 本地 Pillow 画简化卡，无浏览器也能出图；
3. 「t2i」   —— AstrBot 的 「text_to_image」，把纯文本排成图；
4. 「text」  —— 直接发文字，永远不会失败。

上层服务只负责「把内容变成 HTML 和纯文本」，选后端、重试、降级都交给这里。这样
一来，某个环境里浏览器坏了，用户看到的是稍朴素的卡片而不是报错。

Copyright (C) 2026 Whereis-Alice and AstrBot Plugin Authors.
Licensed under the GNU Affero General Public License v3.0 or later.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from astrbot.api import logger

from ..activity import ActivityLog
from ..config import NexusConfig
from . import raster as raster_backend
from .raster import RasterCard, card_from_text
from .themes import Theme, resolve_theme

# html_render 偶发超时（远端 t2i 冷启动、外链封面慢）。三次足够覆盖抖动，再多就是
# 让用户干等 —— 直接降级更礼貌。
HTML_ATTEMPTS = 3
# 视口高度故意压得很矮：远端 t2i 服务的 full_page 截图高度是 max(内容高, 视口高)，
# 视口给大了内容不足时下方会留一片空白。压矮后由内容自己撑开，永远贴合。
HTML_VIEWPORT_HEIGHT = 240
HTML_RETRY_DELAY = 1.2

CACHE_SUBDIR = "cards"
CACHE_MAX_FILES = 240
CACHE_MAX_AGE = 6 * 3600


@dataclass
class RenderedCard:
    """渲染结果。至少有一个字段非空，「text」 永远可用作兜底。"""

    text: str = ""
    image_path: str = ""
    image_url: str = ""
    backend: str = "text"
    elapsed: float = 0.0
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def has_image(self) -> bool:
        return bool(self.image_path or self.image_url)


@dataclass
class CardRequest:
    """一次渲染请求。「html」 与 「plain」 都由调用方准备好。"""

    html: str
    plain: str
    title: str = ""
    eyebrow: str = ""
    subtitle: str = ""
    chips: tuple[str, ...] = ()
    theme: str = ""
    width: int = 0
    raster: RasterCard | None = None

    def raster_card(self) -> RasterCard:
        """没有显式提供栅格内容时，从纯文本按轻标记约定推导。"""

        if self.raster is not None:
            return self.raster
        return card_from_text(
            self.title or "番剧中枢",
            self.plain,
            eyebrow=self.eyebrow,
            subtitle=self.subtitle,
            chips=self.chips,
        )


class CardEngine:
    """负责把 「CardRequest」 变成一张图（或者至少一段文字）。"""

    def __init__(
        self,
        star: Any,
        data_dir: Path,
        activity: ActivityLog | None = None,
    ) -> None:
        self._star = star
        self._cache_dir = Path(data_dir) / CACHE_SUBDIR
        self._activity = activity
        self._lock = asyncio.Lock()
        self._html_broken_until = 0.0
        self._stats: dict[str, int] = {}

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        """各后端命中次数，WebUI 概览页会展示。"""

        return {
            "backends": dict(self._stats),
            "html_cooling_down": self._html_broken_until > time.time(),
            "pillow": raster_backend.PILLOW_AVAILABLE,
            "font": raster_backend.font_available(),
        }

    async def render(self, request: CardRequest, config: NexusConfig) -> RenderedCard:
        """按配置选择后端并逐级降级。任何异常都不会往外抛。"""

        started = time.perf_counter()
        theme = resolve_theme(request.theme or config.card_theme)
        width = request.width or config.card_width
        chain = self._chain(config.card_renderer)
        notes: list[str] = []

        for backend in chain:
            try:
                result = await self._run(backend, request, config, theme, width)
            except Exception as error:  # noqa: BLE001 - 降级本身就是兜底策略
                notes.append(f"{backend}: {type(error).__name__}")
                self._note_failure(backend, error)
                continue
            if result is None:
                notes.append(f"{backend}: 不可用")
                continue
            result.elapsed = time.perf_counter() - started
            result.notes = tuple(notes)
            self._stats[backend] = self._stats.get(backend, 0) + 1
            return result

        return RenderedCard(
            text=request.plain,
            backend="text",
            elapsed=time.perf_counter() - started,
            notes=tuple(notes),
        )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _chain(self, preference: str) -> tuple[str, ...]:
        """把配置里的偏好展开成完整回退链。"""

        if preference == "text":
            return ("text",)
        if preference == "t2i":
            return ("t2i", "text")
        if preference == "raster":
            return ("raster", "t2i", "text")
        if preference == "html":
            return ("html", "raster", "t2i", "text")
        # auto：浏览器最近连续失败时先跳过它，避免每张卡都白等三次超时
        if self._html_broken_until > time.time():
            return ("raster", "html", "t2i", "text")
        return ("html", "raster", "t2i", "text")

    async def _run(
        self,
        backend: str,
        request: CardRequest,
        config: NexusConfig,
        theme: Theme,
        width: int,
    ) -> RenderedCard | None:
        if backend == "html":
            return await self._render_html(request, width)
        if backend == "raster":
            return await self._render_raster(request, theme, width)
        if backend == "t2i":
            return await self._render_t2i(request)
        if backend == "text":
            return RenderedCard(text=request.plain, backend="text")
        return None

    async def _render_html(self, request: CardRequest, width: int) -> RenderedCard | None:
        renderer = getattr(self._star, "html_render", None)
        if renderer is None or not request.html:
            return None
        options = {
            "type": "png",
            "full_page": True,
            "timeout": 60000,
            "viewport_width": int(width),
            "viewport_height": HTML_VIEWPORT_HEIGHT,
            "device_scale_factor_level": "ultra",
        }
        last: Exception | None = None
        for attempt in range(1, HTML_ATTEMPTS + 1):
            try:
                # 传空 data：模板已经是成品 HTML，不需要 Jinja 再插值。
                path = await renderer(request.html, {}, return_url=False, options=options)
                if path:
                    self._html_broken_until = 0.0
                    return RenderedCard(text=request.plain, image_path=str(path), backend="html")
                last = RuntimeError("html_render 返回空路径")
            except Exception as error:  # noqa: BLE001
                last = error
                if attempt < HTML_ATTEMPTS:
                    await asyncio.sleep(HTML_RETRY_DELAY * attempt)
        # 连续三次失败：接下来两分钟先走栅格，别让每条消息都卡在超时上
        self._html_broken_until = time.time() + 120
        if last is not None:
            raise last
        return None

    async def _render_raster(
        self, request: CardRequest, theme: Theme, width: int
    ) -> RenderedCard | None:
        if not raster_backend.PILLOW_AVAILABLE:
            return None
        card = request.raster_card()
        payload = await asyncio.to_thread(raster_backend.render, card, theme, width=width)
        path = await asyncio.to_thread(self._write_cache, payload)
        return RenderedCard(text=request.plain, image_path=path, backend="raster")

    async def _render_t2i(self, request: CardRequest) -> RenderedCard | None:
        renderer = getattr(self._star, "text_to_image", None)
        if renderer is None or not request.plain:
            return None
        url = await renderer(request.plain, return_url=True)
        if not url:
            return None
        return RenderedCard(text=request.plain, image_url=str(url), backend="t2i")

    # ------------------------------------------------------------------
    # 缓存目录
    # ------------------------------------------------------------------

    def _write_cache(self, payload: bytes) -> str:
        """把栅格结果落盘。文件名用内容哈希，同一张卡重复渲染直接命中。"""

        self._cache_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha1(payload).hexdigest()[:20]
        target = self._cache_dir / f"{digest}.png"
        if not target.exists():
            temporary = target.with_suffix(".png.part")
            temporary.write_bytes(payload)
            temporary.replace(target)
        self._prune_cache()
        return str(target)

    def _prune_cache(self) -> None:
        """按数量与年龄清理缓存，避免长期运行把磁盘吃满。"""

        try:
            files = sorted(
                self._cache_dir.glob("*.png"),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return
        deadline = time.time() - CACHE_MAX_AGE
        for index, item in enumerate(files):
            try:
                if index >= CACHE_MAX_FILES or item.stat().st_mtime < deadline:
                    item.unlink(missing_ok=True)
            except OSError:
                continue

    def _note_failure(self, backend: str, error: Exception) -> None:
        message = f"{backend} 渲染失败：{type(error).__name__}: {error}"
        logger.warning(message)
        if self._activity is not None:
            self._activity.warn("render", message)


def plain_lines(*groups: Iterable[str]) -> str:
    """把若干组文本拼成兜底纯文本，自动去掉空行组。"""

    blocks = []
    for group in groups:
        lines = [str(line).rstrip() for line in group if str(line or "").strip()]
        if lines:
            blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


__all__ = [
    "CardEngine",
    "CardRequest",
    "RenderedCard",
    "plain_lines",
]
