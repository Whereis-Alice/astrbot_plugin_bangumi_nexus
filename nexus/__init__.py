"""番剧中枢的 Star 插件包。

模块分层（自下而上，只允许下层被上层引用）::

    constants / models / titles / activity   纯数据与纯函数，无 IO
    config                                   把 AstrBotConfig 收敛成不可变快照
    http / store                             共享 HTTP 客户端与 SQLite 持久层
    sources/*                                每个数据源一个适配器，只负责「抓 + 解析」
    services/*                               业务编排：搜索、匹配、追番、订阅、调度、通知
    render/*                                 主题 / HTML 模板 / Pillow 兜底 / 渲染引擎
    web/*                                    Dashboard 页面的后端接口

Copyright (C) 2026 Whereis-Alice and AstrBot Plugin Authors.
Licensed under the GNU Affero General Public License v3.0 or later.
"""
