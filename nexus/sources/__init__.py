"""数据源适配层。

每个模块只负责一件事：把某个站点的原始响应翻译成 「nexus.models」 里的类型。
它们不认识 AstrBot，也不做业务决策（排序、合并、推送），因此可以脱离插件单测。

Copyright (C) 2026 Whereis-Alice and AstrBot Plugin Authors.
Licensed under the GNU Affero General Public License v3.0 or later.
"""
