"""卡片的 HTML 模板。

一份骨架 + 一份 CSS + 六套主题变量，拼出八种卡片版式。这样做而不是每个主题写一
套模板，是因为「版式 × 主题」会指数爆炸；把配色全部收敛成 CSS 自定义属性之后，
新增主题不需要动这里一行。

所有构造器都是纯函数：吃 models 里的 dataclass，吐字符串，不做 IO、不碰配置对象。
真正决定「用哪个渲染后端」的逻辑住在 「engine.py」。

Copyright (C) 2026 Whereis-Alice and AstrBot Plugin Authors.
Licensed under the GNU Affero General Public License v3.0 or later.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterable, Mapping, Sequence

from ..catalog import CATEGORIES, Category
from ..models import (
    AgeItem,
    CalendarDay,
    Episode,
    FeedItem,
    MatchResult,
    SeasonEntry,
    Subject,
    WatchItem,
)
from .logo import LOGO_MARK
from .themes import Theme, resolve_theme

CARD_WIDTH = 880
HELP_CARD_WIDTH = 1560
HELP_COLUMNS = 3
MAX_ALIASES = 3

# 一张卡里最多嵌多少张封面。封面是 base64 data URI，数量失控会把渲染请求撑爆。
MAX_COVERS = 24


# ---------------------------------------------------------------------------
# 文本工具
# ---------------------------------------------------------------------------


def esc(value: object) -> str:
    """HTML 转义。None 与空值统一变成空串，省掉调用方的判空。"""

    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def clip(text: str, limit: int, *, tail: str = "\u2026") -> str:
    """按「CJK 记 2 宽」的视觉宽度截断，避免中英混排时长短不一。"""

    if limit <= 0:
        return ""
    total = 0
    out: list[str] = []
    for char in str(text or ""):
        total += 2 if ord(char) > 0x2E7F else 1
        if total > limit:
            return "".join(out).rstrip() + tail
        out.append(char)
    return "".join(out)


def text_width(text: str) -> int:
    """视觉宽度，用于分栏时估算行长。"""

    return sum(2 if ord(char) > 0x2E7F else 1 for char in str(text or ""))


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[\r\n\t\u3000 ]+")


def flatten(text: str) -> str:
    """把带标签/换行的简介压成单行纯文本。"""

    return _WS_RE.sub(" ", _TAG_RE.sub(" ", str(text or ""))).strip()


def paragraphs(text: str, limit: int = 3) -> tuple[str, ...]:
    """按空行切段并去掉标签，最多保留 「limit」 段。"""

    raw = _TAG_RE.sub("", str(text or "")).replace("\r", "")
    parts = [_WS_RE.sub(" ", block).strip() for block in raw.split("\n")]
    return tuple(part for part in parts if part)[:limit]


# ---------------------------------------------------------------------------
# 样式
# ---------------------------------------------------------------------------

_CSS = """
*{margin:0;padding:0;box-sizing:border-box}
html,body{background:transparent}
body{width:__WIDTH__px;font-family:var(--font-body);-webkit-font-smoothing:antialiased}
.canvas{position:relative;width:__WIDTH__px;padding:32px;background:var(--canvas);overflow:hidden}
.canvas>.veil{position:absolute;inset:0;background:var(--overlay);opacity:.9;pointer-events:none}
.sheet{position:relative;border:1px solid var(--border);border-radius:var(--radius);
  background:var(--surface-css);box-shadow:var(--shadow);overflow:hidden}

/* ---------- hero ---------- */
.hero{display:flex;gap:20px;align-items:flex-start;padding:26px 30px 22px;
  background:var(--hero);border-bottom:1px solid var(--border)}
.hero .mark{flex:0 0 62px;width:62px;height:62px;filter:drop-shadow(0 8px 18px rgba(0,0,0,.28))}
.hero-main{flex:1 1 auto;min-width:0}
.eyebrow{font-family:var(--font-mono);font-size:12px;letter-spacing:.2em;
  text-transform:uppercase;color:var(--accent)}
.title{font-family:var(--font-heading);font-size:31px;line-height:1.18;margin-top:7px;
  letter-spacing:var(--heading-spacing);color:var(--text);word-break:break-word}
.title.sm{font-size:26px}
.sub{margin-top:7px;font-size:15px;line-height:1.55;color:var(--muted);word-break:break-word}
.hero-aside{flex:0 0 auto;display:flex;gap:10px;align-items:stretch}
.stat{min-width:88px;padding:10px 14px;border-radius:14px;text-align:center;
  border:1px solid var(--border);background:var(--surface-alt)}
.stat b{display:block;font-family:var(--font-heading);font-size:24px;line-height:1.1;color:var(--text)}
.stat b.accent{color:var(--accent)}
.stat span{display:block;margin-top:4px;font-size:11px;letter-spacing:.1em;color:var(--faint)}

/* ---------- chips ---------- */
.chips{display:flex;flex-wrap:wrap;gap:7px;margin-top:13px}
.chip{padding:4px 12px;border-radius:999px;font-size:13px;line-height:1.5;white-space:nowrap;
  border:1px solid var(--border);background:var(--chip);color:var(--chip-text)}
.chip.solid{background:var(--accent);color:var(--accent-ink);border-color:transparent;
  box-shadow:var(--accent-shadow);font-weight:600}
.chip.ghost{background:transparent;color:var(--muted)}
.chip.mono{font-family:var(--font-mono);font-size:12px}

/* ---------- body & blocks ---------- */
.body{padding:24px 30px 6px}
.block{margin-bottom:22px}
.block:last-child{margin-bottom:14px}
.block-head{display:flex;align-items:center;gap:10px;margin-bottom:13px}
.block-head .label{font-family:var(--font-heading);font-size:18px;color:var(--text);
  letter-spacing:var(--heading-spacing);white-space:nowrap}
.block-head .hint{font-family:var(--font-mono);font-size:12px;color:var(--faint);white-space:nowrap}
.block-head .rule{flex:1 1 auto;height:1px;background:var(--border)}
.para{font-size:14.5px;line-height:1.72;color:var(--muted);margin-bottom:8px}
.para:last-child{margin-bottom:0}
.para em{color:var(--text);font-style:normal}
.empty{padding:26px;border-radius:16px;text-align:center;font-size:14px;color:var(--faint);
  border:1px dashed var(--border);background:var(--surface-alt)}

/* ---------- grid & tiles ---------- */
.grid{display:grid;gap:14px}
.grid.c1{grid-template-columns:1fr}
.grid.c2{grid-template-columns:repeat(2,minmax(0,1fr))}
.grid.c3{grid-template-columns:repeat(3,minmax(0,1fr))}
.grid.c4{grid-template-columns:repeat(4,minmax(0,1fr))}
.tile{display:flex;gap:13px;padding:13px;border-radius:16px;
  border:1px solid var(--border);background:var(--surface-alt)}
.tile.plain{border-style:none;background:transparent;padding:0}
.tile.stack{flex-direction:column;gap:10px}
.thumb{position:relative;flex:0 0 auto;width:78px;height:110px;border-radius:11px;overflow:hidden;
  background:var(--chip);border:1px solid var(--border)}
.thumb.wide{width:100%;height:172px}
.thumb.lg{width:150px;height:212px;border-radius:14px}
.thumb img{width:100%;height:100%;object-fit:cover;display:block}
.thumb .ph{display:flex;width:100%;height:100%;align-items:center;justify-content:center;
  font-family:var(--font-heading);font-size:26px;color:var(--chip-text)}
.thumb .rank{position:absolute;left:0;top:0;padding:2px 8px 3px;font-family:var(--font-mono);
  font-size:11px;background:var(--accent);color:var(--accent-ink);border-bottom-right-radius:9px}
.tile-main{flex:1 1 auto;min-width:0}
.tile-title{font-family:var(--font-heading);font-size:16px;line-height:1.35;color:var(--text);
  letter-spacing:var(--heading-spacing);word-break:break-word}
.tile-alt{margin-top:3px;font-size:12.5px;line-height:1.4;color:var(--faint);word-break:break-word}
.tile-meta{display:flex;flex-wrap:wrap;gap:6px 12px;margin-top:8px;font-size:12.5px;color:var(--muted)}
.tile-meta i{font-style:normal;color:var(--faint)}
.tile-meta b{color:var(--accent);font-weight:600}
.tile-note{margin-top:8px;font-size:12.5px;line-height:1.6;color:var(--muted)}
.tile .chips{margin-top:9px}
.tile .chips .chip{padding:2px 9px;font-size:11.5px}

/* ---------- rows ---------- */
.rows{display:flex;flex-direction:column;gap:1px;border-radius:14px;overflow:hidden;
  border:1px solid var(--border)}
.row{display:flex;gap:12px;align-items:center;padding:11px 14px;background:var(--surface-alt)}
.row .idx{flex:0 0 34px;font-family:var(--font-mono);font-size:13px;color:var(--faint);text-align:right}
.row .txt{flex:1 1 auto;min-width:0;font-size:14px;line-height:1.45;color:var(--text);
  word-break:break-word}
.row .txt small{display:block;margin-top:3px;font-size:12px;color:var(--faint)}
.row .tail{flex:0 0 auto;font-family:var(--font-mono);font-size:12px;color:var(--muted);
  white-space:nowrap}
.row .tail.accent{color:var(--accent)}

/* ---------- meter ---------- */
.meter{position:relative;height:8px;border-radius:99px;background:var(--chip);overflow:hidden;
  margin-top:9px}
.meter .fill{position:absolute;left:0;top:0;bottom:0;border-radius:99px;
  background:linear-gradient(90deg,var(--accent),var(--accent-alt))}
.meter-tag{display:flex;justify-content:space-between;margin-top:6px;
  font-family:var(--font-mono);font-size:11.5px;color:var(--faint)}
.meter-tag b{color:var(--text);font-weight:600}

/* ---------- key/value ---------- */
.kv{display:grid;grid-template-columns:auto 1fr;gap:8px 16px;font-size:13.5px;line-height:1.6}
.kv dt{color:var(--faint);white-space:nowrap}
.kv dd{color:var(--text);word-break:break-word}

/* ---------- links ---------- */
.links{display:flex;flex-wrap:wrap;gap:8px}
.link{display:flex;flex-direction:column;gap:2px;padding:9px 13px;border-radius:12px;max-width:100%;
  border:1px solid var(--border);background:var(--surface-alt)}
.link b{font-size:13px;color:var(--text);font-weight:600}
.link span{font-family:var(--font-mono);font-size:11px;color:var(--faint);
  word-break:break-all;max-width:340px}

/* ---------- calendar ---------- */
.week{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:13px}
.day{padding:13px;border-radius:16px;border:1px solid var(--border);background:var(--surface-alt)}
.day.today{border-color:var(--accent);box-shadow:var(--accent-shadow)}
.day-head{display:flex;align-items:baseline;gap:8px;padding-bottom:9px;margin-bottom:10px;
  border-bottom:1px solid var(--border)}
.day-head b{font-family:var(--font-heading);font-size:15px;color:var(--text)}
.day.today .day-head b{color:var(--accent)}
.day-head span{font-family:var(--font-mono);font-size:11px;color:var(--faint)}
.day ul{list-style:none;display:flex;flex-direction:column;gap:7px}
.day li{display:flex;gap:8px;align-items:baseline;font-size:13px;line-height:1.4;color:var(--text)}
.day li em{flex:0 0 auto;font-style:normal;font-family:var(--font-mono);font-size:11px;
  color:var(--accent)}
.day li span{flex:1 1 auto;min-width:0;word-break:break-word}
.day li.more{color:var(--faint);font-size:12px}

/* ---------- help board ---------- */
.board{display:grid;grid-template-columns:repeat(__COLUMNS__,minmax(0,1fr));gap:16px;
  align-items:start}
.col{display:flex;flex-direction:column;gap:16px}
.cat{padding:15px 16px 13px;border-radius:16px;border:1px solid var(--border);
  background:var(--surface-alt)}
.cat-head{display:flex;gap:10px;align-items:center;margin-bottom:4px}
.cat-head .ico{font-size:19px;line-height:1}
.cat-head b{font-family:var(--font-heading);font-size:17px;color:var(--text)}
.cat-head i{margin-left:auto;font-style:normal;font-family:var(--font-mono);font-size:11px;
  color:var(--faint)}
.cat>.blurb{font-size:12.5px;line-height:1.5;color:var(--faint);margin-bottom:11px}
.cmd{padding:9px 0;border-top:1px dashed var(--border)}
.cmd:first-of-type{border-top:none;padding-top:2px}
.cmd .use{display:flex;flex-wrap:wrap;gap:7px;align-items:baseline}
.cmd code{font-family:var(--font-mono);font-size:13px;padding:2px 8px;border-radius:7px;
  background:var(--code);color:var(--code-text);word-break:break-word}
.cmd .badge{padding:1px 7px;border-radius:6px;font-size:10.5px;letter-spacing:.06em;
  background:var(--accent);color:var(--accent-ink)}
.cmd .alias{font-family:var(--font-mono);font-size:11px;color:var(--faint)}
.cmd .desc{margin-top:5px;font-size:12.5px;line-height:1.6;color:var(--muted)}

/* ---------- prefix note & footer ---------- */
.notice{display:flex;gap:12px;align-items:center;margin-bottom:20px;padding:12px 16px;
  border-radius:14px;border:1px solid var(--border-strong);background:var(--chip)}
.notice .ico{font-size:17px}
.notice p{font-size:13px;line-height:1.6;color:var(--chip-text)}
.notice code{font-family:var(--font-mono);padding:1px 6px;border-radius:5px;
  background:var(--code);color:var(--code-text)}
.footer{display:flex;gap:14px;align-items:center;padding:16px 30px 18px;
  border-top:1px solid var(--border);background:var(--surface-alt)}
.footer .brand{font-family:var(--font-heading);font-size:13.5px;color:var(--text)}
.footer .brand span{color:var(--faint);font-weight:400}
.footer .meta{margin-left:auto;font-family:var(--font-mono);font-size:11.5px;color:var(--faint);
  text-align:right;line-height:1.6}
.stamp{position:absolute;right:-6px;bottom:-4px;font-family:var(--font-heading);font-size:74px;
  font-weight:700;letter-spacing:-.04em;color:var(--text);opacity:.045;pointer-events:none;
  white-space:nowrap}
"""

_GLASS_CSS = """
.sheet{backdrop-filter:blur(22px) saturate(150%)}
.tile,.day,.cat,.stat,.link,.row{backdrop-filter:blur(10px)}
"""


def _document(
    theme: Theme,
    *,
    width: int,
    body: str,
    columns: int = HELP_COLUMNS,
    extra_css: str = "",
) -> str:
    """把主题变量、CSS 与卡片主体组装成一个完整的 HTML 文档。"""

    css = _CSS.replace("__WIDTH__", str(int(width))).replace("__COLUMNS__", str(int(columns)))
    if theme.glass:
        css += _GLASS_CSS
    if extra_css:
        css += extra_css
    variables = theme.css_variable_block("      ")
    return (
        '<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
        "<style>\n:root{\n"
        + variables
        + "\n}\n"
        + css
        + "\n</style>\n</head>\n<body>\n"
        + body
        + "\n</body>\n</html>"
    )


# ---------------------------------------------------------------------------
# 片段构造器
# ---------------------------------------------------------------------------


def _chip(text: object, *, variant: str = "") -> str:
    label = esc(text)
    if not label:
        return ""
    cls = "chip" + (f" {variant}" if variant else "")
    return f'<span class="{cls}">{label}</span>'


def _chips(items: Iterable[object], *, variant: str = "", limit: int = 0) -> str:
    rendered = [_chip(item, variant=variant) for item in items if str(item or "").strip()]
    rendered = [item for item in rendered if item]
    if limit > 0:
        rendered = rendered[:limit]
    return f'<div class="chips">{"".join(rendered)}</div>' if rendered else ""


def _stat(value: object, label: str, *, accent: bool = False) -> str:
    cls = "accent" if accent else ""
    return f'<div class="stat"><b class="{cls}">{esc(value)}</b><span>{esc(label)}</span></div>'


def _hero(
    *,
    eyebrow: str,
    title: str,
    sub: str = "",
    chips: str = "",
    stats: Sequence[str] = (),
    mark: bool = True,
    small: bool = False,
) -> str:
    """卡片顶部区。「stats」 已经是 「_stat()」 的产物。"""

    parts = ['<div class="hero">']
    if mark:
        parts.append(LOGO_MARK)
    parts.append('<div class="hero-main">')
    parts.append(f'<div class="eyebrow">{esc(eyebrow)}</div>')
    parts.append(f'<div class="title{" sm" if small else ""}">{esc(title)}</div>')
    if sub:
        parts.append(f'<div class="sub">{esc(sub)}</div>')
    if chips:
        parts.append(chips)
    parts.append("</div>")
    if stats:
        parts.append(f'<div class="hero-aside">{"".join(stats)}</div>')
    parts.append("</div>")
    return "".join(parts)


def _block(label: str, body: str, *, hint: str = "") -> str:
    if not body:
        return ""
    head = [f'<div class="block-head"><span class="label">{esc(label)}</span>']
    if hint:
        head.append(f'<span class="hint">{esc(hint)}</span>')
    head.append('<span class="rule"></span></div>')
    return f'<div class="block">{"".join(head)}{body}</div>'


def _empty(message: str) -> str:
    return f'<div class="empty">{esc(message)}</div>'


def _thumb(
    cover: str,
    fallback: str,
    *,
    size: str = "",
    rank: str = "",
) -> str:
    """封面。「cover」 通常是 data URI；缺图时退化成首字占位块。"""

    cls = "thumb" + (f" {size}" if size else "")
    inner = (
        f'<img src="{esc(cover)}" alt="">'
        if cover
        else f'<div class="ph">{esc((fallback or "?").strip()[:1])}</div>'
    )
    badge = f'<span class="rank">{esc(rank)}</span>' if rank else ""
    return f'<div class="{cls}">{inner}{badge}</div>'


def _meter(percent: int, left: str = "", right: str = "") -> str:
    value = max(0, min(100, int(percent)))
    tag = ""
    if left or right:
        tag = f'<div class="meter-tag"><span>{esc(left)}</span><b>{esc(right)}</b></div>'
    return f'<div class="meter"><div class="fill" style="width:{value}%"></div></div>{tag}'


def _kv(pairs: Sequence[tuple[str, str]]) -> str:
    rows = [
        f"<dt>{esc(key)}</dt><dd>{esc(value)}</dd>"
        for key, value in pairs
        if str(value or "").strip()
    ]
    return f'<dl class="kv">{"".join(rows)}</dl>' if rows else ""


def _dedupe_facts(pairs: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
    """去空、按键去重、单行截断。同一个键可能被多个数据源各填一次，先到先得。"""

    seen: set[str] = set()
    result: list[tuple[str, str]] = []
    for key, value in pairs:
        text = str(value or "").strip()
        if not text or key in seen:
            continue
        seen.add(key)
        result.append((key, clip(text, 96)))
    return result


def _merge_staff(
    primary: Sequence[tuple[str, str]],
    fallback: Sequence[tuple[str, str]],
    *,
    limit: int = 8,
) -> list[tuple[str, str]]:
    """人工整理过的阵容优先，缺的岗位再用兜底数据补。

    不做「同岗位取更长的那个」这种聪明合并：两边写法不同（一边中文岗位名、
    一边日文原文），拼在一起只会自相矛盾，认准一个来源更可信。
    """
    merged = _dedupe_facts(primary)
    have = {key for key, _ in merged}
    merged.extend(pair for pair in _dedupe_facts(fallback) if pair[0] not in have)
    return merged[: max(1, limit)]


def _links(items: Sequence[tuple[str, str]]) -> str:
    cards = [
        f'<div class="link"><b>{esc(name)}</b><span>{esc(url)}</span></div>'
        for name, url in items
        if str(url or "").strip()
    ]
    return f'<div class="links">{"".join(cards)}</div>' if cards else ""


def _rows(items: Sequence[tuple[str, str, str, str]]) -> str:
    """通用列表。每项为 (序号, 主文本, 副文本, 尾标)。"""

    out = []
    for index, (idx, text, note, tail) in enumerate(items, start=1):
        small = f"<small>{esc(note)}</small>" if note else ""
        tail_html = f'<span class="tail accent">{esc(tail)}</span>' if tail else ""
        out.append(
            f'<div class="row"><span class="idx">{esc(idx or index)}</span>'
            f'<span class="txt">{esc(text)}{small}</span>{tail_html}</div>'
        )
    return f'<div class="rows">{"".join(out)}</div>' if out else ""


def _footer(brand: str, note: str, meta: Sequence[str] = ()) -> str:
    lines = "<br>".join(esc(line) for line in meta if str(line or "").strip())
    return (
        '<div class="footer">'
        f'<span class="brand">{esc(brand)} <span>{esc(note)}</span></span>'
        f'<span class="meta">{lines}</span>'
        "</div>"
    )


def _sheet(*fragments: str, stamp: str = "") -> str:
    watermark = f'<div class="stamp">{esc(stamp)}</div>' if stamp else ""
    inner = "".join(fragment for fragment in fragments if fragment)
    return (
        '<div class="canvas"><div class="veil"></div>'
        f'<div class="sheet">{inner}{watermark}</div></div>'
    )


def pack_columns(weights: Sequence[int], columns: int) -> list[list[int]]:
    """LPT 贪心分栏：把重量最大的块先放进当前最矮的一栏。

    帮助卡有六个分类、长短不一，顺序填栏会出现某一栏特别长。这里返回的是索引
    分组，调用方再按分组取内容 —— 于是排版代码不必知道权重是怎么算的。
    """

    count = max(1, int(columns))
    buckets: list[list[int]] = [[] for _ in range(count)]
    loads = [0] * count
    order = sorted(range(len(weights)), key=lambda index: -weights[index])
    for index in order:
        target = loads.index(min(loads))
        buckets[target].append(index)
        loads[target] += weights[index]
    for bucket in buckets:
        bucket.sort()
    return [bucket for bucket in buckets if bucket]


# ---------------------------------------------------------------------------
# 1. 帮助卡
# ---------------------------------------------------------------------------


def _category_weight(category: Category, width: int) -> int:
    """估算一个分类块渲染后的高度，单位是「行」。"""

    weight = 5  # 标题 + 说明 + 内边距
    per_line = max(24, (width // HELP_COLUMNS - 60) // 7)
    for command in category.commands:
        weight += 2
        weight += max(1, text_width(command.summary) // per_line)
        if command.aliases:
            weight += 1
    return weight


def _command_html(command, prefix: str) -> str:
    badge = '<span class="badge">管理员</span>' if command.admin else ""
    aliases = ""
    if command.aliases:
        shown = command.aliases[:MAX_ALIASES]
        more = "\u2026" if len(command.aliases) > MAX_ALIASES else ""
        aliases = (
            '<span class="alias">别名 '
            + esc(" / ".join(f"{prefix}{alias}" for alias in shown))
            + esc(more)
            + "</span>"
        )
    return (
        '<div class="cmd"><div class="use">'
        f"<code>{esc(prefix)}{esc(command.usage)}</code>{badge}{aliases}"
        f'</div><div class="desc">{esc(command.summary)}</div></div>'
    )


def _category_html(category: Category, prefix: str) -> str:
    commands = "".join(_command_html(command, prefix) for command in category.commands)
    return (
        '<div class="cat"><div class="cat-head">'
        f'<span class="ico">{esc(category.icon)}</span><b>{esc(category.title)}</b>'
        f"<i>{len(category.commands)}</i></div>"
        f'<div class="blurb">{esc(category.blurb)}</div>{commands}</div>'
    )


def build_help_card(
    theme: Theme | str,
    *,
    prefix: str = "/",
    version: str = "",
    categories: Sequence[Category] = CATEGORIES,
    width: int = HELP_CARD_WIDTH,
    columns: int = HELP_COLUMNS,
    footnote: str = "",
) -> str:
    """指令总览卡。这是插件的门面，也是 「scripts/render_cards.py」 烘焙的静态图。"""

    resolved = theme if isinstance(theme, Theme) else resolve_theme(theme)
    total = sum(len(category.commands) for category in categories)
    aliases = sum(len(command.aliases) for category in categories for command in category.commands)
    hero = _hero(
        eyebrow="Bangumi Nexus",
        title="番剧中枢",
        sub="搜番 · 追番 · 订阅 · 播报 —— 八个数据源汇成一张卡",
        chips=_chips(
            (
                "Bangumi",
                "bangumi-data",
                "長門番堂",
                "anime1.me",
                "AGE 动漫",
                "萌娘百科",
                "Mikan",
                "RSSHub",
            ),
            variant="ghost",
        ),
        stats=(
            _stat(total, "COMMANDS", accent=True),
            _stat(len(categories), "GROUPS"),
            _stat(aliases, "ALIASES"),
        ),
    )
    notice = (
        '<div class="notice"><span class="ico">\U0001f4a1</span>'
        f"<p>下面所有指令都以 <code>{esc(prefix)}</code> 开头；"
        f"发送 <code>{esc(prefix)}番剧中枢 极光</code> 可临时换个配色看看。"
        "别名与主指令完全等价。</p></div>"
    )
    weights = [_category_weight(category, width) for category in categories]
    buckets = pack_columns(weights, columns)
    column_html = "".join(
        '<div class="col">'
        + "".join(_category_html(categories[index], prefix) for index in bucket)
        + "</div>"
        for bucket in buckets
    )
    body = f'<div class="body">{notice}<div class="board">{column_html}</div></div>'
    footer = _footer(
        "番剧中枢 Bangumi Nexus",
        footnote or "AGPL-3.0 · 数据来自各站公开接口",
        (f"主题 {resolved.name}", version) if version else (f"主题 {resolved.name}",),
    )
    return _document(
        resolved,
        width=width,
        columns=columns,
        body=_sheet(hero, body, footer, stamp="NEXUS"),
    )


# ---------------------------------------------------------------------------
# 2. 每日放送 / 整周日历卡
# ---------------------------------------------------------------------------


def build_calendar_card(
    theme: Theme | str,
    days: Sequence[CalendarDay],
    *,
    width: int = 1180,
    today: int = 0,
    per_day: int = 10,
    title: str = "每日放送",
    subtitle: str = "",
    version: str = "",
) -> str:
    """整周日历卡。四列布局，最后一格放统计，避免七列在窄屏上挤成面条。"""

    resolved = theme if isinstance(theme, Theme) else resolve_theme(theme)
    total = sum(len(day.items) for day in days)
    cells = []
    for day in days:
        marked = " today" if day.weekday == today else ""
        items = []
        for subject in day.items[:per_day]:
            score = f"{subject.score:.1f}" if subject.score else "\u2014"
            items.append(
                f"<li><em>{esc(score)}</em><span>{esc(clip(subject.display_name, 30))}</span></li>"
            )
        if len(day.items) > per_day:
            items.append(f'<li class="more">\u2026 另有 {len(day.items) - per_day} 部</li>')
        if not items:
            items.append('<li class="more">今天休息</li>')
        cells.append(
            f'<div class="day{marked}"><div class="day-head"><b>{esc(day.label)}</b>'
            f"<span>{len(day.items)}</span></div><ul>{''.join(items)}</ul></div>"
        )
    hero = _hero(
        eyebrow="WEEKLY CALENDAR",
        title=title,
        sub=subtitle or "评分来自 Bangumi 用户投票，仅作参考",
        stats=(_stat(total, "TITLES", accent=True), _stat(len(days), "DAYS")),
    )
    body = f'<div class="body"><div class="week">{"".join(cells)}</div></div>'
    footer = _footer("番剧中枢", "数据来源 bgm.tv", ("每日放送", version) if version else ())
    return _document(resolved, width=width, body=_sheet(hero, body, footer, stamp="CALENDAR"))


def build_today_card(
    theme: Theme | str,
    day: CalendarDay,
    *,
    width: int = CARD_WIDTH,
    limit: int = 12,
    covers: dict[int, str] | None = None,
    times: Mapping[int, str] | None = None,
    long_running: Sequence[Subject] = (),
    order_note: str = "按评分从高到低排列",
    version: str = "",
) -> str:
    """今日放送卡。带封面的两列瓦片，比整周卡更适合单日细看。

    「times」 是 「{条目 ID: 日本时间钟点}」。Bangumi 的每日放送接口只给「星期几」，
    钟点得从 bangumi-data 补，那是一次带缓存的异步取数 —— 模板必须是纯函数，
    所以由调用方查好了传进来，而不是在这里现查。

    「long_running」 是年番、半年番那类「今天也在播、但已经从当季日历里消失」的条目。
    它们单独占一栏而不是混进主列表：混进去会让「今天共 N 部」这个数字失真，
    而且它们的序号会插进当季番的排名里，看着像评分比人家高。
    """

    resolved = theme if isinstance(theme, Theme) else resolve_theme(theme)
    covers = covers or {}
    clock = times or {}
    # 封面预算两栏共用：封面是 base64 data URI，数量失控会把渲染请求撑爆。
    # 按「真的嵌进去了才扣」计数，比按下标截断更省 —— 缺图的条目不该占额度。
    budget = MAX_COVERS

    def tile(subject: Subject, rank: str = "") -> str:
        nonlocal budget
        cover = covers.get(subject.id, "") if budget > 0 else ""
        if cover:
            budget -= 1
        meta = []
        air = clock.get(subject.id, "")
        if air:
            # 放送钟点排在评分前面：这张卡是「今天几点看什么」，时间才是主信息。
            meta.append(f"<span><i>放送</i> <b>{esc(air)}</b></span>")
        meta.append(f"<span><i>评分</i> <b>{esc(subject.score_label)}</b></span>")
        if subject.doing:
            meta.append(f"<span><i>在看</i> {subject.doing}</span>")
        if subject.eps:
            meta.append(f"<span><i>话数</i> {subject.eps}</span>")
        return (
            '<div class="tile">'
            + _thumb(cover, subject.display_name, rank=rank)
            + '<div class="tile-main">'
            + f'<div class="tile-title">{esc(clip(subject.display_name, 36))}</div>'
            + (
                f'<div class="tile-alt">{esc(clip(subject.alt_name, 40))}</div>'
                if subject.alt_name
                else ""
            )
            + f'<div class="tile-meta">{"".join(meta)}</div>'
            + _chips(subject.tags[:3], variant="ghost")
            + "</div></div>"
        )

    tiles = [tile(subject, str(index)) for index, subject in enumerate(day.items[:limit], start=1)]
    # 长期连载不编号：它们和上面那栏不是同一个排名体系。
    extra_tiles = [tile(subject) for subject in long_running]
    sections = []
    if tiles:
        sections.append(f'<div class="grid c2">{"".join(tiles)}</div>')
    elif not extra_tiles:
        sections.append(_empty("今天没有查到放送记录"))
    if extra_tiles:
        sections.append(
            _block(
                "长期连载",
                f'<div class="grid c2">{"".join(extra_tiles)}</div>',
                hint="年番 / 半年番，今天也在播",
            )
        )
    body = f'<div class="body">{"".join(sections)}</div>'
    stats = [_stat(len(day.items), "TITLES", accent=True)]
    if long_running:
        stats.append(_stat(len(long_running), "LONG RUN"))
    # 「TITLES」 统计的是今天的总数，主栏却只列前 「limit」 部。两个数字对不上时
    # 必须在副标题里说清楚，否则用户会以为插件把番漏掉了 —— 这正是 1.1.3 的用户反馈。
    sub = order_note
    if len(day.items) > limit:
        sub = f"{order_note}\u00b7今天共 {len(day.items)} 部，这里展示前 {limit} 部"
    hero = _hero(
        eyebrow="TODAY ON AIR",
        title=f"{day.label}\u00b7今日放送",
        sub=sub,
        stats=tuple(stats),
    )
    footer = _footer("番剧中枢", "数据来源 bgm.tv", ("今日放送", version) if version else ())
    return _document(resolved, width=width, body=_sheet(hero, body, footer, stamp="TODAY"))


# ---------------------------------------------------------------------------
# 3. 条目详情卡（跨源聚合）
# ---------------------------------------------------------------------------


def build_subject_card(
    theme: Theme | str,
    match: MatchResult,
    *,
    width: int = CARD_WIDTH,
    cover: str = "",
    next_air: str = "",
    watch_links: Sequence[tuple[str, str]] = (),
    summary_override: str = "",
    staff: Sequence[tuple[str, str]] = (),
    cast: Sequence[tuple[str, str]] = (),
    cast_hint: str = "",
    version: str = "",
) -> str:
    """跨源聚合详情卡：Bangumi 打底，長門番堂补制作与声优，其余源补观看入口。

    「staff」「cast」 是调用方从 Bangumi 的 「infobox」/「characters」 备好的兜底数据。
    長門番堂只有当季页面，年番、旧番、以及该站临时挂掉时都拿不到阵容 ——
    此前这两栏就直接空着，看起来像插件坏了。有兜底之后卡片永远是满的，
    而長門番堂在线时仍然优先用它（人工整理过，岗位名统一）。
    """

    resolved = theme if isinstance(theme, Theme) else resolve_theme(theme)
    subject = match.subject
    season = match.season
    chips: list[str] = []
    stats: list[str] = []
    if subject:
        stats.append(_stat(subject.score_label, "SCORE", accent=True))
        if subject.rank:
            stats.append(_stat(f"#{subject.rank}", "RANK"))
        if subject.doing:
            stats.append(_stat(subject.doing, "WATCHING"))
        chips.append(_chip(subject.type_label, variant="solid"))
        if subject.weekday_label:
            chips.append(_chip(subject.weekday_label))
        if subject.air_date:
            chips.append(_chip(f"首播 {subject.air_date}", variant="mono"))
        if subject.eps:
            chips.append(_chip(f"{subject.eps} 话", variant="mono"))
    if next_air:
        chips.append(_chip(next_air, variant="solid"))

    title = match.title
    alt = ""
    if subject and subject.alt_name:
        alt = subject.alt_name
    elif season and season.title_jp and season.title_jp != title:
        alt = season.title_jp

    hero = _hero(
        eyebrow="SUBJECT",
        title=clip(title, 42),
        sub=clip(alt, 60),
        chips=f'<div class="chips">{"".join(chips)}</div>' if chips else "",
        stats=stats,
        small=text_width(title) > 34,
    )

    blocks: list[str] = []
    intro = '<div class="tile plain">' + _thumb(cover, title, size="lg")
    facts: list[tuple[str, str]] = []
    if season:
        facts.append(("题材", " / ".join(season.genres[:6])))
        facts.append(("首播", season.broadcast))
    if subject:
        facts.append(("放送", f"{subject.weekday_label} {subject.air_date}".strip()))
        if subject.eps:
            facts.append(("话数", f"{subject.eps} 话"))
        facts.append(("Bangumi", subject.url or f"https://bgm.tv/subject/{subject.id}"))
    if match.data_item and match.data_item.official_site:
        facts.append(("官网", match.data_item.official_site))
    elif subject and subject.infobox.get("官方网站"):
        facts.append(("官网", subject.infobox["官方网站"]))
    if match.moegirl:
        facts.append(("萌娘百科", match.moegirl.url))
    intro += '<div class="tile-main">' + _kv(_dedupe_facts(facts))
    summary = summary_override or (subject.summary if subject else "")
    if summary:
        blurbs = paragraphs(summary, 3)
        joined = "".join(f'<div class="para">{esc(clip(part, 320))}</div>' for part in blurbs)
        intro += f'<div style="margin-top:12px">{joined}</div>'
    intro += "</div></div>"
    blocks.append(_block("条目信息", intro, hint=f"bgm {subject.id}" if subject else ""))

    season_staff = (
        [
            ("原作", season.staff_of("原作")),
            ("导演", season.staff_of("导演", "監督", "监督")),
            ("动画制作", season.studio),
            ("系列构成", season.staff_of("系列构成", "シリーズ構成", "脚本")),
            ("人物设定", season.staff_of("人物设定", "角色设计", "キャラクターデザイン")),
            ("音乐", season.staff_of("音乐", "音楽")),
        ]
        if season
        else []
    )
    staff_rows = _merge_staff(season_staff, staff)
    if staff_rows:
        blocks.append(
            _block(
                "制作阵容",
                _kv(staff_rows),
                hint="長門番堂 / bgm" if season else "bgm infobox",
            )
        )

    cast_pairs = list(season.cast) if season and season.cast else list(cast)
    if cast_pairs:
        cast_rows = [
            (str(index), clip(role, 30), clip(voice, 30), "")
            for index, (role, voice) in enumerate(cast_pairs[:8], start=1)
        ]
        hint = f"{len(season.cast)} 位" if season and season.cast else cast_hint
        blocks.append(_block("主要声优", _rows(cast_rows), hint=hint))

    if subject and subject.tags:
        blocks.append(_block("标签", _chips(subject.tags[:14]), hint="按热度"))

    if watch_links:
        blocks.append(_block("在线观看", _links(list(watch_links)[:8]), hint="正版优先"))

    body = f'<div class="body">{"".join(blocks)}</div>'
    footer = _footer(
        "番剧中枢",
        f"置信度 {match.confidence:.0%}" if match.confidence else "跨源聚合",
        (version,) if version else (),
    )
    return _document(resolved, width=width, body=_sheet(hero, body, footer, stamp="SUBJECT"))


def build_search_card(
    theme: Theme | str,
    keyword: str,
    subjects: Sequence[Subject],
    *,
    width: int = CARD_WIDTH,
    covers: dict[int, str] | None = None,
    version: str = "",
) -> str:
    """搜索结果卡。单列瓦片，保证长标题不折断成两行还看不清。"""

    resolved = theme if isinstance(theme, Theme) else resolve_theme(theme)
    covers = covers or {}
    tiles = []
    for index, subject in enumerate(subjects, start=1):
        meta = [
            f"<span><i>评分</i> <b>{esc(subject.score_label)}</b></span>",
            f"<span><i>类型</i> {esc(subject.type_label)}</span>",
        ]
        if subject.air_date:
            meta.append(f"<span><i>首播</i> {esc(subject.air_date)}</span>")
        if subject.eps:
            meta.append(f"<span><i>话数</i> {subject.eps}</span>")
        meta.append(f"<span><i>ID</i> {subject.id}</span>")
        note = clip(flatten(subject.summary), 120)
        tiles.append(
            '<div class="tile">'
            + _thumb(covers.get(subject.id, ""), subject.display_name, rank=str(index))
            + '<div class="tile-main">'
            + f'<div class="tile-title">{esc(clip(subject.display_name, 40))}</div>'
            + (
                f'<div class="tile-alt">{esc(clip(subject.alt_name, 46))}</div>'
                if subject.alt_name
                else ""
            )
            + f'<div class="tile-meta">{"".join(meta)}</div>'
            + (f'<div class="tile-note">{esc(note)}</div>' if note else "")
            + "</div></div>"
        )
    body = (
        f'<div class="body"><div class="grid c1">{"".join(tiles)}</div></div>'
        if tiles
        else f'<div class="body">{_empty("没有找到匹配的条目，换个关键词试试")}</div>'
    )
    hero = _hero(
        eyebrow="SEARCH",
        title=clip(keyword, 34) or "搜索结果",
        sub=f"在 Bangumi 找到 {len(subjects)} 条结果，发送 /bgm <ID> 看详情",
        stats=(_stat(len(subjects), "HITS", accent=True),),
    )
    footer = _footer("番剧中枢", "数据来源 bgm.tv", ("搜索", version) if version else ())
    return _document(resolved, width=width, body=_sheet(hero, body, footer, stamp="SEARCH"))


# ---------------------------------------------------------------------------
# 4. 分集卡
# ---------------------------------------------------------------------------


def build_episode_card(
    theme: Theme | str,
    subject: Subject,
    episodes: Sequence[Episode],
    *,
    width: int = CARD_WIDTH,
    cover: str = "",
    next_air: str = "",
    highlight: float | None = None,
    version: str = "",
) -> str:
    """分集/放送时间卡。「highlight」 是下一集的 sort，会被标成强调色。"""

    resolved = theme if isinstance(theme, Theme) else resolve_theme(theme)
    rows = []
    for episode in episodes[:16]:
        tail = "即将放送" if highlight is not None and episode.sort == highlight else ""
        rows.append(
            (
                f"{episode.sort:g}",
                clip(episode.display_name, 42),
                episode.airdate or "待定",
                tail,
            )
        )
    chips = [_chip(subject.type_label, variant="solid")]
    if subject.weekday_label:
        chips.append(_chip(subject.weekday_label))
    if next_air:
        chips.append(_chip(next_air, variant="solid"))
    hero = _hero(
        eyebrow="EPISODES",
        title=clip(subject.display_name, 40),
        sub=clip(subject.alt_name, 56),
        chips=f'<div class="chips">{"".join(chips)}</div>',
        stats=(
            _stat(subject.score_label, "SCORE", accent=True),
            _stat(subject.eps or len(episodes), "EPS"),
        ),
        small=text_width(subject.display_name) > 32,
    )
    intro = ""
    if cover:
        intro = _block(
            "封面",
            '<div class="tile plain">' + _thumb(cover, subject.display_name, size="lg") + "</div>",
        )
    body = (
        '<div class="body">'
        + intro
        + _block(
            "分集列表",
            _rows(rows) or _empty("这个条目暂时没有分集数据"),
            hint=f"共 {len(episodes)} 集",
        )
        + "</div>"
    )
    footer = _footer("番剧中枢", "数据来源 bgm.tv", ("分集", version) if version else ())
    return _document(resolved, width=width, body=_sheet(hero, body, footer, stamp="EPISODE"))


# ---------------------------------------------------------------------------
# 5. 追番清单卡
# ---------------------------------------------------------------------------


def build_watchlist_card(
    theme: Theme | str,
    items: Sequence[WatchItem],
    *,
    width: int = CARD_WIDTH,
    covers: dict[int, str] | None = None,
    owner: str = "",
    airing: dict[int, str] | None = None,
    version: str = "",
) -> str:
    """追番进度卡。每部番一个进度条，「airing」 提供放送倒计时文案。"""

    resolved = theme if isinstance(theme, Theme) else resolve_theme(theme)
    covers = covers or {}
    airing = airing or {}
    watching = sum(1 for item in items if item.status == "watching")
    done = sum(1 for item in items if item.status == "finished")
    tiles = []
    for item in items[:24]:
        chips = [_chip(item.status_label, variant="solid" if item.status == "watching" else "")]
        countdown = airing.get(item.subject_id, "")
        if countdown:
            chips.append(_chip(countdown, variant="mono"))
        if item.score:
            chips.append(_chip(f"评分 {item.score:.1f}", variant="ghost"))
        tiles.append(
            '<div class="tile">'
            + _thumb(covers.get(item.subject_id, ""), item.title)
            + '<div class="tile-main">'
            + f'<div class="tile-title">{esc(clip(item.title, 34))}</div>'
            + f'<div class="chips">{"".join(chips)}</div>'
            + _meter(item.percent, "进度", item.progress_label)
            + (f'<div class="tile-note">{esc(clip(item.note, 60))}</div>' if item.note else "")
            + "</div></div>"
        )
    body = (
        f'<div class="body"><div class="grid c2">{"".join(tiles)}</div></div>'
        if tiles
        else f'<div class="body">{_empty("追番表还是空的，发送 /追番 <名称> 加第一部")}</div>'
    )
    hero = _hero(
        eyebrow="WATCHLIST",
        title=owner or "我的追番",
        sub="进度可用 /看到 <名称> <集数> 更新，写 +1 表示往前推一集",
        stats=(
            _stat(len(items), "TOTAL", accent=True),
            _stat(watching, "WATCHING"),
            _stat(done, "DONE"),
        ),
    )
    footer = _footer(
        "番剧中枢", "追番数据保存在本地 SQLite", ("追番表", version) if version else ()
    )
    return _document(resolved, width=width, body=_sheet(hero, body, footer, stamp="WATCH"))


# ---------------------------------------------------------------------------
# 6. RSS / 更新通知卡
# ---------------------------------------------------------------------------


def build_feed_card(
    theme: Theme | str,
    source: str,
    items: Sequence[FeedItem],
    *,
    width: int = CARD_WIDTH,
    subtitle: str = "",
    cover: str = "",
    persona_text: str = "",
    version: str = "",
) -> str:
    """RSS 更新卡。「persona_text」 是人格转述，会作为卡片开头的一段话出现。"""

    resolved = theme if isinstance(theme, Theme) else resolve_theme(theme)
    rows = []
    for index, item in enumerate(items[:12], start=1):
        note = " \u00b7 ".join(part for part in (item.published, item.size) if part)
        rows.append((str(index), clip(item.title, 66), note, ""))
    blocks = []
    if persona_text:
        blocks.append(
            _block(
                "播报",
                f'<div class="para"><em>{esc(persona_text)}</em></div>',
                hint="由人格生成",
            )
        )
    if cover:
        blocks.append(
            _block(
                "封面",
                '<div class="tile plain">' + _thumb(cover, source, size="lg") + "</div>",
            )
        )
    blocks.append(
        _block("更新条目", _rows(rows) or _empty("这次没有新条目"), hint=f"{len(items)} 条")
    )
    hero = _hero(
        eyebrow="FEED UPDATE",
        title=clip(source, 34) or "订阅更新",
        sub=subtitle or "来自你订阅的 RSS 源",
        stats=(_stat(len(items), "NEW", accent=True),),
    )
    body = f'<div class="body">{"".join(blocks)}</div>'
    footer = _footer("番剧中枢", "订阅可用 /sub_list 管理", ("RSS", version) if version else ())
    return _document(resolved, width=width, body=_sheet(hero, body, footer, stamp="FEED"))


def build_picker_card(
    theme: Theme | str,
    *,
    title: str,
    options: Sequence[tuple[str, str, str, str]],
    subtitle: str = "",
    cover: str = "",
    hint: str = "",
    excludes: Sequence[str] = (),
    width: int = CARD_WIDTH,
    version: str = "",
) -> str:
    """选源卡：把候选字幕组编号列出来，等用户回一个数字。

    「options」 每项为 「(序号, 组名, 补充说明, 标记)」，「标记」 是简繁 / 画质 / 片源
    这类差异 —— 用户在这一步看清差异，才不会订完才发现被同一集刷四遍。

    这张卡是一次性的中间过程，选完由 main.py 撤回，所以不放 footer 链接。
    """
    resolved = theme if isinstance(theme, Theme) else resolve_theme(theme)
    rows = [
        (idx, clip(name, 40), clip(note, 60), clip(tag, 24)) for idx, name, note, tag in options
    ]
    blocks = []
    if cover:
        blocks.append(
            _block("封面", '<div class="tile plain">' + _thumb(cover, title, size="lg") + "</div>")
        )
    blocks.append(
        _block(
            "可选源",
            _rows(rows) or _empty("这部番在 Mikan 上还没有字幕组发布"),
            hint=f"{len(rows)} 个",
        )
    )
    if excludes:
        blocks.append(
            _block("已生效的排除项", _chips(excludes, variant="ghost", limit=12), hint="全局")
        )
    blocks.append(
        _block(
            "怎么选",
            f'<div class="para">{esc(hint or "直接回复序号即可，比如 1")}</div>',
        )
    )
    hero = _hero(
        eyebrow="PICK A SOURCE",
        title=clip(title, 34) or "挑一个更新源",
        sub=subtitle or "回复序号完成订阅",
        stats=(_stat(len(rows), "SOURCES", accent=True),),
        small=text_width(title) > 30,
    )
    body = f'<div class="body">{"".join(blocks)}</div>'
    footer = _footer("番剧中枢", "选完这条列表会自动撤回", (version,) if version else ())
    return _document(resolved, width=width, body=_sheet(hero, body, footer, stamp="PICK"))


def build_notice_card(
    theme: Theme | str,
    *,
    eyebrow: str,
    title: str,
    lines: Sequence[str],
    subtitle: str = "",
    persona_text: str = "",
    cover: str = "",
    chips: Sequence[str] = (),
    link: str = "",
    width: int = CARD_WIDTH,
    stamp: str = "NOTICE",
    version: str = "",
) -> str:
    """通用通知卡。Webhook 事件、系统提示、长文回复都走这一张。"""

    resolved = theme if isinstance(theme, Theme) else resolve_theme(theme)
    blocks = []
    if persona_text:
        blocks.append(
            _block(
                "播报", f'<div class="para"><em>{esc(persona_text)}</em></div>', hint="由人格生成"
            )
        )
    if cover:
        blocks.append(
            _block("封面", '<div class="tile plain">' + _thumb(cover, title, size="lg") + "</div>")
        )
    detail = "".join(f'<div class="para">{esc(line)}</div>' for line in lines if str(line).strip())
    if detail:
        blocks.append(_block("详情", detail))
    if link:
        blocks.append(_block("链接", _links([("打开", link)])))
    hero = _hero(
        eyebrow=eyebrow,
        title=clip(title, 40),
        sub=subtitle,
        chips=_chips(chips, variant="ghost"),
        small=text_width(title) > 32,
    )
    body = f'<div class="body">{"".join(blocks) or _empty("没有更多内容")}</div>'
    footer = _footer("番剧中枢", "Bangumi Nexus", (version,) if version else ())
    return _document(resolved, width=width, body=_sheet(hero, body, footer, stamp=stamp))


# ---------------------------------------------------------------------------
# 7. 抽番 / 推荐卡
# ---------------------------------------------------------------------------


def build_gacha_card(
    theme: Theme | str,
    match: MatchResult,
    *,
    width: int = 760,
    cover: str = "",
    reason: str = "",
    pool_size: int = 0,
    watch_links: Sequence[tuple[str, str]] = (),
    version: str = "",
) -> str:
    """抽番卡。信息比详情卡少、封面比详情卡大，主打「一眼决定看不看」。"""

    resolved = theme if isinstance(theme, Theme) else resolve_theme(theme)
    subject = match.subject
    season = match.season
    chips = []
    if season:
        chips.extend(_chip(genre, variant="ghost") for genre in season.genres[:5])
    elif subject:
        chips.extend(_chip(tag, variant="ghost") for tag in subject.tags[:5])
    facts: list[tuple[str, str]] = []
    if season:
        facts.append(("动画制作", season.studio))
        facts.append(("导演", season.staff_of("导演", "監督", "监督")))
        facts.append(("首播", season.broadcast))
    if subject:
        facts.append(("评分", subject.score_label))
        facts.append(("话数", str(subject.eps) if subject.eps else ""))
        facts.append(("放送", subject.weekday_label))
    hero = _hero(
        eyebrow="GACHA",
        title=clip(match.title, 34),
        sub=reason or "从当季新番里随机抽到的一部",
        chips=f'<div class="chips">{"".join(chips)}</div>' if chips else "",
        stats=(_stat(pool_size or "\u2014", "POOL", accent=True),),
        small=True,
    )
    intro = (
        '<div class="tile stack">'
        + _thumb(cover, match.title, size="wide")
        + _kv([(key, value) for key, value in facts if str(value or "").strip()])
        + "</div>"
    )
    blocks = [_block("这一部", intro)]
    summary = clip(flatten(subject.summary if subject else ""), 260)
    if summary:
        blocks.append(_block("简介", f'<div class="para">{esc(summary)}</div>'))
    if watch_links:
        blocks.append(_block("去哪看", _links(list(watch_links)[:4])))
    body = f'<div class="body">{"".join(blocks)}</div>'
    footer = _footer("番剧中枢", "再抽一次就再发一遍指令", (version,) if version else ())
    return _document(resolved, width=width, body=_sheet(hero, body, footer, stamp="GACHA"))


def build_recommend_card(
    theme: Theme | str,
    items: Sequence[AgeItem],
    *,
    width: int = CARD_WIDTH,
    covers: dict[str, str] | None = None,
    version: str = "",
) -> str:
    """AGE 动漫推荐位卡。三列封面墙。"""

    resolved = theme if isinstance(theme, Theme) else resolve_theme(theme)
    covers = covers or {}
    tiles = []
    for item in items[:18]:
        tiles.append(
            '<div class="tile stack">'
            + _thumb(covers.get(item.url, "") or item.cover, item.title, size="wide")
            + f'<div class="tile-title">{esc(clip(item.title, 26))}</div>'
            + (
                f'<div class="tile-alt">{esc(clip(item.progress, 24))}</div>'
                if item.progress
                else ""
            )
            + "</div>"
        )
    body = (
        f'<div class="body"><div class="grid c3">{"".join(tiles)}</div></div>'
        if tiles
        else f'<div class="body">{_empty("推荐位暂时没有数据")}</div>'
    )
    hero = _hero(
        eyebrow="RECOMMEND",
        title="热门更新推荐",
        sub="来自 AGE 动漫推荐位",
        stats=(_stat(len(items), "TITLES", accent=True),),
    )
    footer = _footer("番剧中枢", "数据来源 agedm.io", (version,) if version else ())
    return _document(resolved, width=width, body=_sheet(hero, body, footer, stamp="PICKS"))


# ---------------------------------------------------------------------------
# 8. 季度总览卡
# ---------------------------------------------------------------------------


def build_season_card(
    theme: Theme | str,
    season_label: str,
    entries: Sequence[SeasonEntry],
    *,
    width: int = 1180,
    limit: int = 36,
    version: str = "",
) -> str:
    """季度新番总览。三列紧凑瓦片，突出制作组与首播时间。"""

    resolved = theme if isinstance(theme, Theme) else resolve_theme(theme)
    tiles = []
    for entry in entries[:limit]:
        meta = []
        if entry.studio:
            meta.append(f"<span><i>制作</i> {esc(clip(entry.studio, 22))}</span>")
        director = entry.staff_of("导演", "監督", "监督")
        if director:
            meta.append(f"<span><i>导演</i> {esc(clip(director, 18))}</span>")
        if entry.broadcast:
            meta.append(f"<span><i>首播</i> {esc(clip(entry.broadcast, 18))}</span>")
        tiles.append(
            '<div class="tile stack">'
            + f'<div class="tile-title">{esc(clip(entry.display_name, 28))}</div>'
            + (
                f'<div class="tile-alt">{esc(clip(entry.title_jp, 34))}</div>'
                if entry.title_jp and entry.title_jp != entry.display_name
                else ""
            )
            + (f'<div class="tile-meta">{"".join(meta)}</div>' if meta else "")
            + _chips(entry.genres[:4], variant="ghost")
            + "</div>"
        )
    counted = len(entries)
    body = (
        f'<div class="body"><div class="grid c3">{"".join(tiles)}</div></div>'
        if tiles
        else f'<div class="body">{_empty("这一季暂时没有抓到数据")}</div>'
    )
    hero = _hero(
        eyebrow="SEASON",
        title=f"{season_label} 新番总览",
        sub="题材、制作组与首播时间来自長門番堂",
        stats=(
            _stat(counted, "TITLES", accent=True),
            _stat(min(counted, limit), "SHOWN"),
        ),
    )
    footer = _footer("番剧中枢", "数据来源 yuc.wiki", (version,) if version else ())
    return _document(resolved, width=width, body=_sheet(hero, body, footer, stamp="SEASON"))


# ---------------------------------------------------------------------------
# 9. 诊断卡
# ---------------------------------------------------------------------------


def build_diagnose_card(
    theme: Theme | str,
    results: Sequence[tuple[str, bool, str, float]],
    *,
    width: int = CARD_WIDTH,
    version: str = "",
) -> str:
    """数据源健康检查卡。每项为 (名称, 是否成功, 说明, 耗时秒)。"""

    resolved = theme if isinstance(theme, Theme) else resolve_theme(theme)
    rows = []
    for name, ok, note, elapsed in results:
        mark = "\u2713" if ok else "\u2717"
        rows.append((mark, name, clip(note, 80), f"{elapsed * 1000:.0f} ms"))
    passed = sum(1 for _n, ok, _note, _e in results if ok)
    hero = _hero(
        eyebrow="DIAGNOSTICS",
        title="数据源健康检查",
        sub="失败通常是网络或站点结构变动，可配合代理重试",
        stats=(_stat(f"{passed}/{len(results)}", "PASSED", accent=True),),
    )
    body = f'<div class="body">{_block("逐源结果", _rows(rows) or _empty("没有可检查的源"))}</div>'
    footer = _footer("番剧中枢", "/番剧诊断", (version,) if version else ())
    return _document(resolved, width=width, body=_sheet(hero, body, footer, stamp="CHECK"))


def build_anirss_card(
    theme: Theme | str,
    *,
    status: Mapping[str, object],
    entries: Sequence[tuple[str, str, str]] = (),
    width: int = CARD_WIDTH,
    version: str = "",
) -> str:
    """ani-rss 同步状态卡。

    「entries」 每项是 (标题, 一行简述, 尾标)，尾标一般放进度或「已完结」。
    这张卡同时服务 「/anirss」 和同步结果通知，所以状态字段全部通过 「status」
    映射传进来，而不是让模板层去碰服务对象。
    """

    resolved = theme if isinstance(theme, Theme) else resolve_theme(theme)
    configured = bool(status.get("configured"))
    reachable = bool(status.get("ok"))
    base = str(status.get("base") or "未配置")
    total = _as_display_int(status.get("total"))
    active = _as_display_int(status.get("active"))
    synced = _as_display_int(status.get("synced"))
    mark = "已连通" if reachable else ("已配置未连通" if configured else "未配置")
    chips = _chips(
        (
            mark,
            f"鉴权 {_AUTH_LABEL.get(str(status.get('auth') or ''), '未设置')}",
            f"间隔 {_as_display_int(status.get('interval'))} 分钟"
            if status.get("interval")
            else "自动同步已关",
        )
    )
    hero = _hero(
        eyebrow="ANI-RSS SYNC",
        title="ani-rss 同步",
        sub=clip(base, 72),
        chips=chips,
        stats=(
            _stat(f"{active}/{total}", "启用/总数", accent=True),
            _stat(synced, "已入库"),
        ),
    )
    facts = _kv(
        [
            ("上次同步", str(status.get("last_at_label") or "还没同步过")),
            ("同步方向", str(status.get("direction") or "")),
            ("推送会话", str(status.get("targets_label") or "未指定")),
            ("提示", str(status.get("note") or "")),
        ]
    )
    rows = [(str(index), title, note, tail) for index, (title, note, tail) in enumerate(entries, 1)]
    listing = _rows(rows) or _empty(
        "没读到订阅 —— 确认 ani-rss 已启动，端口和密钥填对了"
        if configured
        else "先在配置里填 ani-rss 地址和密钥"
    )
    body = (
        '<div class="body">'
        + _block("连接", facts)
        + _block("ani-rss 里的订阅", listing, hint=f"共 {total} 条")
        + "</div>"
    )
    footer = _footer("番剧中枢", "/anirss", (version,) if version else ())
    return _document(
        resolved,
        width=width,
        body=_sheet(hero, body, footer, stamp="SYNC" if reachable else "SETUP"),
    )


_AUTH_LABEL = {"api_key": "API Key", "password": "账号密码", "none": "未设置"}


def _as_display_int(value: object) -> int:
    """卡片里只想显示整数，字符串/None/浮点全部收敛掉。"""

    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


__all__ = [
    "CARD_WIDTH",
    "HELP_CARD_WIDTH",
    "HELP_COLUMNS",
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
    "clip",
    "esc",
    "flatten",
    "pack_columns",
    "paragraphs",
    "text_width",
]
