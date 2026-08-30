"""指令清单：帮助卡、WebUI「指令表」与 README 共用的唯一事实来源。

把指令元数据从 「main.py」 的装饰器里抽出来单独放一份，是为了让三处展示不会各自
漂移 —— 帮助卡渲染、Dashboard 页面、文档生成都读这里。纯数据，无 IO。

Copyright (C) 2026 Whereis-Alice and AstrBot Plugin Authors.
Licensed under the GNU Affero General Public License v3.0 or later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Command:
    """一条面向用户的指令。

    「usage」 不带指令前缀（前缀在渲染时拼上去），因为同一份数据要服务于前缀各异的
    多个部署。
    """

    name: str
    usage: str
    summary: str
    aliases: tuple[str, ...] = ()
    admin: bool = False
    origin: str = ""

    def payload(self, prefix: str = "/") -> dict[str, Any]:
        return {
            "name": self.name,
            "usage": f"{prefix}{self.usage}",
            "summary": self.summary,
            "aliases": list(self.aliases),
            "admin": self.admin,
            "origin": self.origin,
        }


@dataclass(frozen=True)
class Category:
    """一组同类指令。「icon」 是单个 emoji，卡片与 WebUI 都直接画它。"""

    key: str
    title: str
    blurb: str
    icon: str
    commands: tuple[Command, ...] = field(default_factory=tuple)

    def payload(self, prefix: str = "/") -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "blurb": self.blurb,
            "icon": self.icon,
            "commands": [command.payload(prefix) for command in self.commands],
        }


SEARCH = Category(
    key="search",
    title="查番",
    blurb="从八个源里把一部番的全部信息拼齐",
    icon="\U0001f50d",
    commands=(
        Command(
            "bgm",
            "bgm <关键词|条目ID|help> [数量]",
            "在 Bangumi 搜索条目；传纯数字按条目 ID 直接开卡。",
            origin="astrbot_plugin_bangumi",
        ),
        Command(
            "bgm番剧",
            "bgm番剧 <关键词>",
            "只搜 TV 动画，排除剧场版与三次元。",
            aliases=("动漫", "动画", "番", "动画片"),
            origin="astrbot_plugin_bangumi",
        ),
        Command(
            "bgm剧场版",
            "bgm剧场版 <关键词>",
            "只搜剧场版 / 电影。",
            aliases=("电影", "劇場版"),
            origin="astrbot_plugin_bangumi",
        ),
        Command(
            "bgm漫画",
            "bgm漫画 <关键词>",
            "搜漫画与轻小说条目。",
            aliases=("漫画",),
            origin="astrbot_plugin_bangumi",
        ),
        Command(
            "查番",
            "查番 <名称>",
            "跨源聚合卡：评分、放送倒计时、制作组、声优、正版与在线观看入口一次给全。",
            origin="astrbot_plugin_anime_gacha",
        ),
        Command(
            "放送时间",
            "放送时间 <名称|条目ID>",
            "下一集什么时候播、还差几天，放送时刻按日本时间。",
            origin="astrbot_plugin_bangumi",
        ),
        Command(
            "在线观看",
            "在线观看 <名称>",
            "汇总正版平台、anime1、AGE 动漫与官网链接。",
        ),
        Command(
            "萌娘百科",
            "萌娘百科 <关键词>",
            "取萌娘百科词条摘要，作品和角色都能查。",
            origin="astrbot_plugin_anime_gacha",
        ),
    ),
)

CALENDAR = Category(
    key="calendar",
    title="日历",
    blurb="今天播什么、这一季有什么",
    icon="\U0001f4c5",
    commands=(
        Command(
            "calendar",
            "calendar",
            "整周每日放送总览卡。",
            aliases=("每日放送",),
            origin="astrbot_plugin_bangumi",
        ),
        Command(
            "today",
            "today",
            "今天放送的番：封面、放送钟点、评分，外加今天也在播的年番。",
            aliases=("今日放送", "今日新番"),
            origin="astrbot_plugin_bangumi",
        ),
        Command(
            "季度新番",
            "季度新番 [202607]",
            "整季新番表：题材、制作组、首播时间，数据来自長門番堂。",
        ),
        Command(
            "新番",
            "新番 today|push|status",
            "指令组：today 看今日、push 立即播报、status 查播报状态（后两者限管理员）。",
            aliases=("bangumi",),
            origin="astrbot_plugin_bangumi_calendar",
        ),
    ),
)

WATCH = Category(
    key="watch",
    title="追番",
    blurb="自己的番自己记，进度和放送提醒都在",
    icon="\U0001f4d6",
    commands=(
        Command(
            "追番",
            "追番 <名称|条目ID>",
            "加入追番表，并顺手推荐可一键订阅的 Mikan 资源源。",
            origin="astrbot_plugin_bangumi",
        ),
        Command(
            "弃坑",
            "弃坑 <名称>",
            "把一部番标记为已弃坑（记录保留，可再 /追番 复活）。",
            origin="astrbot_plugin_bangumi",
        ),
        Command(
            "追番列表",
            "追番列表",
            "追番进度卡：进度条、放送倒计时、评分一览。",
            aliases=("我的追番",),
        ),
        Command(
            "看到",
            "看到 <名称> <集数>",
            "更新观看进度；集数写 +1 表示往前推一集。",
        ),
    ),
)

SUBSCRIBE = Category(
    key="subscribe",
    title="订阅推送",
    blurb="RSS 进来，卡片出去，人格负责说话",
    icon="\U0001f4e1",
    commands=(
        Command(
            "sub",
            "sub <名称> [RSS地址|mikan:番剧ID|rsshub:路径|dmhy:关键词]",
            "订阅一个源。只写番名会先列出 Mikan 上的字幕组，回复序号即订阅一个组。",
            origin="astrbot_plugin_rsshub",
        ),
        Command("unsub", "unsub <名称>", "退订一个源。", origin="astrbot_plugin_rsshub"),
        Command(
            "sub_list",
            "sub_list",
            "当前会话的订阅清单卡。",
            aliases=("订阅列表",),
            origin="astrbot_plugin_rsshub",
        ),
        Command(
            "sub_exclude",
            "sub_exclude [list|add <词>|del <词>|clear|preset|apply]",
            "全局排除项：命中的发布直接丢掉。新订阅自动套用，apply 可刷到已有订阅。",
            aliases=("排除词",),
        ),
        Command(
            "sub_test",
            "sub_test <名称|RSS地址>",
            "立刻抓一次并把最新几条发出来，不写去重库，用来验证源是否可用。",
            origin="astrbot_plugin_rsshub",
        ),
        Command(
            "sub_stop",
            "sub_stop",
            "暂停当前会话的全部推送（订阅保留）。",
            aliases=("rss_stop", "停止RSS", "停止推送"),
            origin="astrbot_plugin_rsshub",
        ),
        Command(
            "sub_status",
            "sub_status",
            "轮询任务状态：下次抓取时间、成功失败计数。",
            aliases=("推送状态", "任务状态"),
            origin="astrbot_plugin_rsshub",
        ),
        Command(
            "sub_state",
            "sub_state <名称>",
            "单个订阅的详情：上次抓取、最新条目、最近错误。",
            aliases=("订阅状态",),
            origin="astrbot_plugin_rsshub",
        ),
        Command(
            "activate_subs",
            "activate_subs",
            "恢复当前会话的全部订阅。",
            origin="astrbot_plugin_rsshub",
        ),
        Command(
            "deactivate_subs",
            "deactivate_subs",
            "停用当前会话的全部订阅。",
            origin="astrbot_plugin_rsshub",
        ),
        Command(
            "unsub_all",
            "unsub_all",
            "清空当前会话的订阅，需要二次确认。",
            origin="astrbot_plugin_rsshub",
        ),
        Command(
            "sub_export",
            "sub_export",
            "导出订阅与追番表为 JSON 文本，可直接贴给别的群。",
            origin="astrbot_plugin_rsshub",
        ),
        Command(
            "sub_import",
            "sub_import <JSON>",
            "导入 /sub_export 产出的 JSON，重名条目自动跳过。",
            origin="astrbot_plugin_rsshub",
        ),
        Command(
            "sub_profile",
            "sub_profile get|set <主题|渲染器>",
            "按会话覆盖卡片主题与渲染方式，不影响别的群。",
            origin="astrbot_plugin_rsshub",
        ),
        Command(
            "sub_session",
            "sub_session get|set [会话ID]",
            "把本会话的推送改投到另一个会话（例如群里订阅、私聊收）。",
            origin="astrbot_plugin_rsshub",
        ),
        Command(
            "日历订阅",
            "日历订阅 开|关",
            "让每日新番播报也发到当前会话，无需管理员改配置。",
        ),
        Command(
            "rsshelp",
            "rsshelp",
            "订阅相关的详细说明卡（含常用 RSSHub 路径示例）。",
            aliases=("RSS帮助",),
            origin="astrbot_plugin_rsshub",
        ),
    ),
)

FUN = Category(
    key="fun",
    title="娱乐",
    blurb="不知道看什么的时候交给运气",
    icon="\U0001f3b0",
    commands=(
        Command(
            "抽番",
            "抽番 [题材]",
            "从当季新番里随机抽一部，可按题材过滤（如「抽番 恋爱」）。",
            aliases=("随机番剧",),
            origin="astrbot_plugin_anime_gacha",
        ),
        Command(
            "番剧推荐",
            "番剧推荐",
            "AGE 动漫推荐位的热门更新。",
        ),
    ),
)

MAINTAIN = Category(
    key="maintain",
    title="数据与维护",
    blurb="数据源是活的，出问题先看这里",
    icon="\U0001f6e0",
    commands=(
        Command(
            "番剧中枢",
            "番剧中枢 [主题名]",
            "帮助总览卡；带主题名可临时换配色预览。",
            aliases=("番剧帮助",),
        ),
        Command(
            "番剧诊断",
            "番剧诊断",
            "逐个数据源做健康检查，给出耗时与失败原因。",
            admin=True,
        ),
        Command(
            "anime_update",
            "anime_update",
            "立刻刷新 anime1.me 在线观看索引。",
            origin="astrbot_plugin_anime1_list",
        ),
        Command(
            "检查番剧数据",
            "检查番剧数据",
            "查看各数据源的缓存条目数与最后刷新时间。",
            origin="astrbot_plugin_anime_gacha",
        ),
        Command(
            "更新番剧数据",
            "更新番剧数据 [202607]",
            "重新抓取指定季度的番剧数据，省略则用当季。",
            admin=True,
            origin="astrbot_plugin_anime_gacha",
        ),
        Command(
            "bgm模板",
            "bgm模板 1|2|3",
            "切换搜索结果卡的版式（1 详情 / 2 紧凑 / 3 纯文本）。",
            origin="astrbot_plugin_bangumi",
        ),
    ),
)

CATEGORIES: tuple[Category, ...] = (SEARCH, CALENDAR, WATCH, SUBSCRIBE, FUN, MAINTAIN)
CATEGORY_BY_KEY = {category.key: category for category in CATEGORIES}


def all_commands() -> tuple[Command, ...]:
    return tuple(command for category in CATEGORIES for command in category.commands)


def command_count() -> int:
    return len(all_commands())


def category_count() -> int:
    return len(CATEGORIES)


def alias_count() -> int:
    return sum(len(command.aliases) for command in all_commands())


def find(name: str) -> Command | None:
    """按名字或别名找指令，忽略前导斜杠与大小写。"""

    token = str(name or "").strip().lstrip("/").lower()
    if not token:
        return None
    for command in all_commands():
        if command.name.lower() == token:
            return command
        if any(alias.lower() == token for alias in command.aliases):
            return command
    return None


def payload(prefix: str = "/") -> list[dict[str, Any]]:
    """WebUI「指令表」直接吃这个结构。"""

    return [category.payload(prefix) for category in CATEGORIES]


__all__ = [
    "CATEGORIES",
    "CATEGORY_BY_KEY",
    "Category",
    "Command",
    "alias_count",
    "all_commands",
    "category_count",
    "command_count",
    "find",
    "payload",
]
