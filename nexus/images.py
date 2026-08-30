"""封面图的瘦身工具：URL 尺寸改写 + 本地降采样。

为什么需要这一层：卡片渲染走的是「把图片 base64 内联进 HTML」的路子，
远端 t2i / html_render 服务对单次请求的体积很敏感。而 bgm 图床的 「l」
（large）尺寸单张就有 0.9~1.2 MB，今日放送卡一次要 12 张，base64 之后
HTML 能涨到十几 MB —— 服务端直接超时或 OOM，卡片最后整体退化成首字占位块。
所以这里做两件事：

1. 能改 URL 就改 URL：bgm 图床同一张图有 「l/c/m/s/g」 五档，
   直接换成小档位，省掉的是下载流量，最省事也最可靠。
2. 改不了 URL 的源（AGE / 長門番堂 / 萌娘百科各有各的图床）则本地降采样，
   按卡片实际显示尺寸压到够用就行。

本模块不做 IO，可以被任意层安全引用；Pillow 缺席时全部函数优雅退化成「原样返回」。

Copyright (C) 2026 Whereis-Alice and AstrBot Plugin Authors.
Licensed under the GNU Affero General Public License v3.0 or later.
"""

from __future__ import annotations

import io
import re

try:  # pragma: no cover - 环境差异
    from PIL import Image

    PILLOW_AVAILABLE = True
except Exception:  # noqa: BLE001 - Pillow 缺席时只是不降采样，不该让插件起不来
    Image = None  # type: ignore[assignment]
    PILLOW_AVAILABLE = False

#: bgm 图床的尺寸档位，从大到小。「l」 是原图级别，「g」 是列表格子用的小图。
BGM_COVER_SIZES = ("l", "c", "m", "s", "g")

#: 形如 「/pic/cover/l/9c/8f/1234_abcd.jpg」，中间那一段就是尺寸档位。
_BGM_COVER_RE = re.compile(r"(/pic/cover/)([lcmsg])(/)", re.IGNORECASE)

#: 部分链接还带一层 「/r/400/」 的服务端缩放前缀，改尺寸时要一并清掉，
#: 否则会出现「先缩到 400 再取小图」的重复处理。
_BGM_RESIZE_RE = re.compile(r"/r/\d+(?=/pic/)", re.IGNORECASE)

#: 有 alpha 通道时必须留 PNG，否则透明区会被压成黑块。
_ALPHA_MODES = frozenset({"RGBA", "LA", "PA", "P"})


def bgm_cover_size(url: str, size: str = "c") -> str:
    """把 bgm 图床链接改写成指定尺寸档位；非 bgm 链接原样返回。

    只认 「/pic/cover/<档位>/」 这一种结构，认不出来就不动，
    宁可下大图也不要拼出一个 404 的地址。
    """

    target = size.lower()
    if target not in BGM_COVER_SIZES:
        return url
    text = str(url or "")
    if not _BGM_COVER_RE.search(text):
        return text
    return _BGM_RESIZE_RE.sub("", _BGM_COVER_RE.sub(rf"\g<1>{target}\g<3>", text))


def shrink(payload: bytes, *, max_edge: int, quality: int = 80) -> tuple[bytes, str]:
    """把图片降采样到长边不超过 「max_edge」，返回 「(字节, MIME)」。

    任何一步出问题（没装 Pillow、格式不认、动图）都返回空 MIME，
    调用方据此判断「没压成，用原图」——降采样是优化而不是功能，不该让它成为故障点。
    """

    if not PILLOW_AVAILABLE or max_edge <= 0 or not payload:
        return payload, ""
    try:
        with Image.open(io.BytesIO(payload)) as image:
            if getattr(image, "is_animated", False):
                # 动图重编码会丢帧，交给原图
                return payload, ""
            width, height = image.size
            longest = max(width, height)
            if longest <= max_edge:
                return payload, ""
            ratio = max_edge / float(longest)
            resized = image.convert("RGBA" if image.mode in _ALPHA_MODES else "RGB").resize(
                (max(1, round(width * ratio)), max(1, round(height * ratio))),
                Image.LANCZOS,
            )
            buffer = io.BytesIO()
            if resized.mode == "RGBA":
                resized.save(buffer, format="PNG", optimize=True)
                mime = "image/png"
            else:
                resized.save(buffer, format="JPEG", quality=quality, optimize=True)
                mime = "image/jpeg"
    except Exception:  # noqa: BLE001 - 压缩失败一律退回原图
        return payload, ""
    shrunk = buffer.getvalue()
    # 极少数情况下重编码反而更大（原图已是高压缩 WebP），那就别换
    return (shrunk, mime) if shrunk and len(shrunk) < len(payload) else (payload, "")


__all__ = ["BGM_COVER_SIZES", "PILLOW_AVAILABLE", "bgm_cover_size", "shrink"]
