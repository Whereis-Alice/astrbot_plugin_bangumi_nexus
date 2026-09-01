"""排除项的纯逻辑：勾选名展开成关键词、判断一条标题是否该被丢掉。

为什么单独一个模块：这套判断有三个调用点 —— 轮询时的会话/全局两层
（「services.base」）、每条订阅自己的黑名单（「models.Subscription.matches」）、
以及 WebUI 的合并预览（「web.api」）。之前 「Subscription.matches」 里自己写了一份
「word in title」，于是双语保护和 「CR 」 的边界只在其中一条路上生效，另一条静默漏过。
这里只依赖 「constants」，不碰配置、数据库和网络，可以直接喂真实标题做回归测试。
"""

from __future__ import annotations

from collections.abc import Iterable

from .constants import DUAL_LANGUAGE_MARKERS, EXCLUDE_PRESET_BY_NAME, LANGUAGE_ONLY_WORDS

__all__ = ["blocked_by", "expand_excludes", "is_dual_language"]


def expand_excludes(values: Iterable[str]) -> tuple[str, ...]:
    """把用户勾的排除项展开成真正用于过滤的关键词。

    输入既接受预设名（「繁体」「720p」），也接受任意自定义词。预设名会展开成
    一组同义写法 —— 同一件事字幕组能写出 「繁体」「繁日」「CHT」「BIG5」 四种，
    只存字面量等于没过滤。展开后去重并保持勾选顺序，便于在 WebUI 里回显。
    """
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        name = str(raw or "").strip()
        if not name:
            continue
        # 预设展开出来的词原样保留，绝不再 strip 一次：「CR 」 尾部那个空格
        # 就是它的边界，抹掉之后 「Secret」「Sacred」 里的 「cr」 也会命中。
        # 只有用户手打的词需要去掉两头空白（上面已经 strip 过）。
        for word in EXCLUDE_PRESET_BY_NAME.get(name, (name,)):
            key = word.lower()
            if key.strip() and key not in seen:
                seen.add(key)
                result.append(word)
    return tuple(result)


def is_dual_language(title: str) -> bool:
    """这条发布是不是「一个文件里同时装了简体和繁体」。"""

    text = (title or "").lower()
    return any(marker in text for marker in DUAL_LANGUAGE_MARKERS)


def blocked_by(title: str, words: Iterable[str]) -> str:
    """标题是否命中排除词，命中就返回那个词（便于日志说明原因），否则空串。

    两条不显然的规则：

    * 词一律**不再 strip**。预设里的 「CR 」「[ASS」 靠自带的空格与方括号划边界，
      去掉就会在 「Secret」「class」 这类普通单词里误命中。
    * 「简体」/「繁体」 这类单语词遇到双语单文件（「简繁日内封」「CHS&CHT」）时
      不算命中 —— 那种发布本来就带用户想要的那一路字幕，丢掉等于整集收不到。
      真想连双语单文件一起躲，勾 「简繁」 那组预设。
    """

    text = (title or "").lower()
    dual = is_dual_language(title)
    for word in words:
        needle = str(word or "").lower()
        if not needle.strip() or needle not in text:
            continue
        if dual and needle in LANGUAGE_ONLY_WORDS:
            continue
        return str(word)
    return ""
