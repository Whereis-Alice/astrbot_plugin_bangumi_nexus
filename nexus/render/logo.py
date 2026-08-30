"""插件标识（logo）的 SVG 源。

一份图形，三种用途：

* 「LOGO_SVG」      —— 独立 512×512 文件，写到 「assets/logo.svg」 与
  「pages/nexus/assets/logo.svg」（构建脚本负责同步，避免两份漂移）；
* 「LOGO_ICON_SVG」 —— 满幅版本（去掉留白与圆角），烘焙成根目录 「logo.png」，
  AstrBot Dashboard 的插件卡只认这个路径；
* 「LOGO_MARK」     —— 嵌进卡片 hero 区的内联版本（带 class="mark"）。

图形语义：五瓣樱花＝五路数据源（长门番堂 / Bangumi / AGE / anime1 / RSSHub）向中枢
汇聚；花心的播放三角＝中枢本体，尺寸缩到 48px 时靠它撑住辨识度；外圈高亮弧与端点
游标＝追番进度，端点就是「当前追到第几话」。配色取自「樱粉」主题的
accent 「#e4547d」 / accent_alt 「#ffab86」，和默认卡片同源，因此插件卡和卡片
放在一起不会像两个产品。

为什么这三份 SVG 由同一个模板生成：它们只差「id 前缀」和「底板矩形」两处，早先各写
一份副本，改一次配色要同步改三遍，极易漏。现在几何与配色只有一处定义，
「_build」 负责把 id 前缀换掉 —— 同一页面里并存三枚 SVG 时 id 撞车会串色，所以前缀
必须不同。

Copyright (C) 2026 Whereis-Alice and AstrBot Plugin Authors.
Licensed under the GNU Affero General Public License v3.0 or later.
"""

from __future__ import annotations

#: 花瓣外缘直接借用圆弧（圆心 (0,-104)、半径 56）画成宽阔的圆瓣，瓣间留约 6.8° 的
#: 真空隙，瓣尖只切一个 22 深的浅缺口。
#:
#: 这个比例是反复试出来的：花瓣画得细长、瓣尖又深切成两个等距凸角时，五瓣的十个凸角
#: 会在视觉上连成一圈，读出来是雏菊或菊花而不是樱花。只有让「瓣间深谷」远深于
#: 「瓣尖浅缺」，人眼才会先数出 5 片瓣、再注意到缺口。
_PETAL_PATH = (
    "M 0 -36 C 18 -38 30 -46 36 -61 "
    "A 56 56 0 0 0 29.7 -151.5 "
    "C 20 -151 10 -146 0 -138 "
    "C -10 -146 -20 -151 -29.7 -151.5 "
    "A 56 56 0 0 0 -36 -61 "
    "C -30 -46 -18 -38 0 -36 Z"
)

#: 瓣面中脉。只画一条极淡的曲线：花瓣是纯白到淡粉的渐变，没有中脉会显得像塑料片，
#: 但画重了又会在小尺寸下变成一道脏线。
_VEIN_PATH = "M 0 -62 C 5 -90 5 -112 0 -128"

#: 五瓣的旋转角。72° 均分，不做随机扰动 —— 图标要的是秩序感，不是写实。
_PETAL_ANGLES = (0, 72, 144, 216, 288)


def _blossom(prefix: str) -> str:
    """把单片花瓣旋转复制成五瓣，返回可直接嵌进模板的 SVG 片段。"""
    return "\n".join(
        f'      <g transform="rotate({angle})">\n'
        f'        <path d="{_PETAL_PATH}" fill="url(#{prefix}Petal)"/>\n'
        f'        <path d="{_VEIN_PATH}" fill="none" stroke="#e0547f"'
        f' stroke-opacity=".15" stroke-width="5" stroke-linecap="round"/>\n'
        f"      </g>"
        for angle in _PETAL_ANGLES
    )


#: 独立文件用的根标签：带 xmlns 与显式宽高，浏览器和图片查看器都能直接打开。
_HEADER_FILE = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="512" height="512"'
    ' viewBox="0 0 512 512" role="img" aria-label="Bangumi Nexus">'
)

#: 内联进卡片 HTML 用的根标签：不写 xmlns（HTML 解析器会自动补），尺寸交给 CSS 的
#: 「.mark」 类控制。
_HEADER_INLINE = '<svg class="mark" viewBox="0 0 512 512" role="img" aria-label="Bangumi Nexus">'

#: 带留白的圆角底板。留白是给桌面端图标的安全边距，圆角半径按 iOS 的 squircle 手感取。
_PLATE_ROUNDED = (
    '  <rect x="24" y="24" width="464" height="464" rx="116" fill="url(#{p}Base)"/>\n'
    '  <rect x="24" y="24" width="464" height="464" rx="116" fill="url(#{p}Sheen)"/>'
)

#: 满幅底板。AstrBot 的插件卡自己会裁圆角，图标再留一层白边就会缩成小方块。
_PLATE_FULL = (
    '  <rect width="512" height="512" fill="url(#{p}Base)"/>\n'
    '  <rect width="512" height="512" fill="url(#{p}Sheen)"/>'
)

#: 图形本体。「{p}」 是 id 前缀占位符，「{plate}」 是底板，「{blossom}」 是五瓣花，
#: 「{petal}」 是散落花瓣复用的同一条路径。除这四个占位符外模板里不含花括号，
#: 因此可以安全地交给 「str.format」。
_TEMPLATE = """{header}
  <defs>
    <linearGradient id="{p}Base" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#ffc9b6"/>
      <stop offset=".32" stop-color="#ff9dbe"/>
      <stop offset=".66" stop-color="#ea5585"/>
      <stop offset="1" stop-color="#b13d72"/>
    </linearGradient>
    <linearGradient id="{p}Sheen" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#ffffff" stop-opacity=".34"/>
      <stop offset=".56" stop-color="#ffffff" stop-opacity="0"/>
    </linearGradient>
    <radialGradient id="{p}Glow" cx=".5" cy=".46" r=".56">
      <stop offset="0" stop-color="#fff4f7" stop-opacity=".5"/>
      <stop offset=".58" stop-color="#ffffff" stop-opacity=".08"/>
      <stop offset="1" stop-color="#ffffff" stop-opacity="0"/>
    </radialGradient>
    <linearGradient id="{p}Petal" x1=".5" y1="0" x2=".5" y2="1">
      <stop offset="0" stop-color="#ffffff"/>
      <stop offset=".58" stop-color="#fff4f8"/>
      <stop offset="1" stop-color="#ffcedd"/>
    </linearGradient>
    <linearGradient id="{p}Core" x1=".15" y1="0" x2=".85" y2="1">
      <stop offset="0" stop-color="#f4638f"/>
      <stop offset=".55" stop-color="#dc4576"/>
      <stop offset="1" stop-color="#b23060"/>
    </linearGradient>
    <filter id="{p}Drop" x="-40%" y="-40%" width="180%" height="180%">
      <feDropShadow dx="0" dy="10" stdDeviation="12" flood-color="#7d2145" flood-opacity=".3"/>
    </filter>
    <filter id="{p}Soft" x="-60%" y="-60%" width="220%" height="220%">
      <feDropShadow dx="0" dy="4" stdDeviation="6" flood-color="#7d2145" flood-opacity=".32"/>
    </filter>
  </defs>

{plate}
  <circle cx="256" cy="252" r="200" fill="url(#{p}Glow)"/>

  <!-- 追番进度环：淡轨 + 高亮弧 + 端点游标。端点即「当前追到第几话」 -->
  <g fill="none" stroke="#ffffff" stroke-linecap="round">
    <circle cx="256" cy="252" r="198" stroke-opacity=".13" stroke-width="3"/>
    <circle cx="256" cy="252" r="182" stroke-opacity=".24" stroke-width="11"/>
    <path d="M 256 70 A 182 182 0 1 1 131 386" stroke-opacity=".95" stroke-width="11"/>
  </g>
  <g filter="url(#{p}Soft)">
    <circle cx="131" cy="386" r="14" fill="#ffffff"/>
  </g>
  <circle cx="131" cy="386" r="6" fill="url(#{p}Core)"/>

  <!-- 两片飘落的花瓣：填住四角留白，也让「樱」这个主题在缩略图里也站得住 -->
  <g fill="#ffffff">
    <g transform="translate(126 128) scale(.17) rotate(30)" fill-opacity=".6">
      <path d="{petal}"/>
    </g>
    <g transform="translate(396 396) scale(.145) rotate(-148)" fill-opacity=".48">
      <path d="{petal}"/>
    </g>
  </g>

  <!-- 五瓣樱花＝五路数据源向中枢汇聚 -->
  <g filter="url(#{p}Drop)">
    <g transform="translate(256 252) scale(.875)">
{blossom}
    </g>
  </g>

  <!-- 花心＝中枢：播放三角压在深樱色圆盘上，缩到 48px 也认得出是「看番」 -->
  <circle cx="256" cy="252" r="58" fill="#ffffff" fill-opacity=".97"/>
  <circle cx="256" cy="252" r="51" fill="url(#{p}Core)"/>
  <path d="M 236 227 A 10 10 0 0 1 250 220 L 292 245 A 11 11 0 0 1 292 259
           L 250 284 A 10 10 0 0 1 236 277 Z" fill="#ffffff"/>

  <!-- 星芒：右上角的一点闪光，避免大面积渐变显得空 -->
  <path d="M 418 108 L 425 133 L 450 140 L 425 147 L 418 172 L 411 147 L 386 140 L 411 133 Z"
        fill="#ffffff" fill-opacity=".88"/>
</svg>
"""


def _build(prefix: str, header: str, plate: str) -> str:
    """按 id 前缀与底板样式渲染出一份完整 SVG。"""
    return _TEMPLATE.format(
        header=header,
        plate=plate.format(p=prefix),
        blossom=_blossom(prefix),
        petal=_PETAL_PATH,
        p=prefix,
    )


LOGO_SVG = _build("bn", _HEADER_FILE, _PLATE_ROUNDED)
LOGO_ICON_SVG = _build("bi", _HEADER_FILE, _PLATE_FULL)
LOGO_MARK = _build("mk", _HEADER_INLINE, _PLATE_ROUNDED)

__all__ = ["LOGO_ICON_SVG", "LOGO_MARK", "LOGO_SVG"]
