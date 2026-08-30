"""服务层公共依赖与回复载体。

服务层刻意不直接碰 AstrBot 的 「event」/「Star」 API：所有服务只吃 「Deps」、吐 「Reply」，
由 main.py 负责把 「Reply」 翻译成消息链。这样服务层能在 pytest 里裸跑，
也不会被 SDK 版本变动波及。
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..activity import ActivityLog
from ..config import NexusConfig
from ..constants import COVER_HERO_EDGE, COVER_THUMB_EDGE
from ..http import HttpClient
from ..render import CardEngine, CardRequest, RenderedCard, plain_lines
from ..sources.hub import SourceHub
from ..store import Store
from .matcher import Matcher

PREF_THEME = "card_theme"
PREF_RENDERER = "card_renderer"
PREF_TEMPLATE = "search_template"
PREF_DAILY = "daily_digest"
PREF_TARGET = "push_target"


@dataclass(slots=True)
class Deps:
    """所有服务共享的一组句柄。

    「config」 存的是取值函数而不是快照：AstrBot 的配置可以在 WebUI 里热改，
    服务每次用时重新取，才不会拿到过期值。
    """

    star: Any
    context: Any
    http: HttpClient
    hub: SourceHub
    store: Store
    engine: CardEngine
    matcher: Matcher
    activity: ActivityLog
    config: Callable[[], NexusConfig]

    @property
    def conf(self) -> NexusConfig:
        return self.config()


@dataclass(slots=True)
class Reply:
    """服务层统一回复载体。

    「card」 为空表示只发文字；两者都有时 main.py 会先发卡片图，
    文字作为图渲染彻底失败时的兜底。

    「caption」 是跟在卡片图后面一起发的补充文本。卡片是图，图里的链接点不动，
    所以「在线观看」这类需要用户点击的内容必须另外给一段纯文本。
    它不参与「图渲染失败就退回文字」那套逻辑 —— 文字兜底本身已经带上了。
    """

    text: str = ""
    card: CardRequest | None = None
    caption: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def plain(cls, text: str) -> Reply:
        return cls(text=text)

    @property
    def empty(self) -> bool:
        return not self.text and self.card is None


def make_card(
    html: str,
    *,
    plain: str,
    title: str = "",
    eyebrow: str = "",
    subtitle: str = "",
    chips: Sequence[str] = (),
    theme: str = "",
    width: int = 0,
) -> CardRequest:
    """把 HTML + 纯文本包成渲染请求；栅格兜底的字段一起带上。"""
    return CardRequest(
        html=html,
        plain=plain,
        title=title,
        eyebrow=eyebrow,
        subtitle=subtitle,
        chips=tuple(str(chip) for chip in chips if chip),
        theme=theme,
        width=width,
    )


async def style_for(deps: Deps, umo: str) -> tuple[str, str]:
    """取会话级的卡片风格（主题、渲染器），没设过就用全局配置。

    群和群的口味不一样，所以主题/渲染器允许每个会话自己覆盖，
    这也是 「/sub_profile」 的存在意义。
    """
    conf = deps.conf
    theme = conf.card_theme
    renderer = conf.card_renderer
    if not umo:
        return theme, renderer
    try:
        chosen = await deps.store.get_pref(umo, PREF_THEME)
        if chosen:
            theme = chosen
        picked = await deps.store.get_pref(umo, PREF_RENDERER)
        if picked:
            renderer = picked
    except Exception:  # noqa: BLE001 - 偏好读不出来不该拖垮回复
        pass
    return theme, renderer


async def template_for(deps: Deps, umo: str) -> str:
    """搜索结果版式：1 详情卡 / 2 紧凑卡 / 3 纯文本（继承自上游 「/bgm模板」）。"""
    if not umo:
        return "1"
    try:
        value = await deps.store.get_pref(umo, PREF_TEMPLATE)
    except Exception:  # noqa: BLE001
        return "1"
    return value if value in {"1", "2", "3"} else "1"


async def cover_uri(deps: Deps, url: str, *, max_edge: int = COVER_HERO_EDGE) -> str:
    """封面转 base64 data URI。

    远端渲染服务不一定能直连 bgm 的图床（也不一定有代理），
    所以图片一律由插件自己下好、内联进 HTML；同时按 「max_edge」 瘦身，
    否则内联体积会把渲染服务撑爆。
    """
    if not url:
        return ""
    try:
        return await deps.http.data_uri(url, max_edge=max_edge)
    except Exception:  # noqa: BLE001
        return ""


async def cover_map(
    deps: Deps,
    pairs: Iterable[tuple[Any, str]],
    *,
    max_edge: int = COVER_THUMB_EDGE,
) -> dict[Any, str]:
    """批量取封面，返回 「{业务键: data_uri}」，失败的键直接缺席。

    「data_uris」 返回的是以**原始 URL**为键的字典，所以这里必须按 URL 反查，
    不能拿它跟 「wanted」 做 zip —— 那样拿到的是 URL 本身，
    卡片里就会出现一堆远端地址，渲染服务取不到图，整卡退化成首字占位块。
    """
    wanted = [(key, url) for key, url in pairs if url]
    if not wanted:
        return {}
    uris = await deps.http.data_uris([url for _, url in wanted], max_edge=max_edge)
    return {key: uris[url] for key, url in wanted if uris.get(url)}


async def llm_text(
    deps: Deps,
    prompt: str,
    *,
    provider_id: str = "",
    system_prompt: str = "",
    umo: str = "",
    limit: int = 0,
) -> str:
    """调一次 LLM 拿纯文本；任何异常都吞掉并返回空串。

    人格转述、日文简介翻译都走这里 —— 它们全是「有则更好」的增强，
    绝不能因为模型不可用就让主功能失败。
    """
    generate = getattr(deps.context, "llm_generate", None)
    if generate is None or not prompt.strip():
        return ""
    chosen = provider_id
    if not chosen:
        getter = getattr(deps.context, "get_current_chat_provider_id", None)
        if getter is not None:
            try:
                # 该方法在 4.25 是 async，早期版本是同步；两种都兜住。
                candidate = getter(umo)
                if inspect.isawaitable(candidate):
                    candidate = await candidate
                chosen = str(candidate or "")
            except Exception:  # noqa: BLE001
                chosen = ""
    try:
        response = await generate(
            chat_provider_id=chosen or None,
            prompt=prompt,
            system_prompt=system_prompt or None,
        )
    except Exception as error:  # noqa: BLE001
        deps.activity.warn("llm", f"生成失败：{error}")
        return ""
    text = str(getattr(response, "completion_text", "") or "").strip()
    if limit and len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def looks_japanese(text: str) -> bool:
    """粗判是否含平假名/片假名，用来决定要不要翻译简介。"""
    return any("\u3040" <= ch <= "\u30ff" for ch in text)


def is_long_reply(text: str) -> bool:
    """上游 「astrbot_plugin_bangumi」 的判定：超过 30 字或含换行就该转图。"""
    return len(text) > 30 or "\n" in text


def numeric(text: str) -> int:
    """纯数字串转 int，否则 0 —— 用于「关键词还是条目 ID」的分流。"""
    stripped = text.strip()
    return int(stripped) if stripped.isdigit() else 0


def joined(*groups: Iterable[str]) -> str:
    return plain_lines(*groups)


def describe(card: RenderedCard) -> str:
    """给活动日志用的一行渲染结果摘要。"""
    where = card.backend
    if card.notes:
        where += " (" + "; ".join(card.notes) + ")"
    return f"{where} {card.elapsed:.2f}s"


def pref_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "on", "true", "yes", "开", "开启"}


def parse_switch(text: str) -> bool | None:
    """解析「开/关」这类开关参数，无法识别时返回 None。"""
    token = text.strip().lower()
    if token in {"开", "开启", "on", "true", "1", "启用"}:
        return True
    if token in {"关", "关闭", "off", "false", "0", "停用", "禁用"}:
        return False
    return None


def mapping_get(source: Mapping[str, Any] | Any, key: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(key, default)
    return getattr(source, key, default)
