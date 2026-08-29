"""业务服务层。

服务层做决策：搜什么、怎么合并、什么时候推、推给谁、失败怎么退避。它依赖数据源
适配器与持久层，但同样不认识 AstrBot 的 「Star」 —— 需要发消息时通过注入的回调完成，
于是这一层可以脱离机器人环境单测。

Copyright (C) 2026 Whereis-Alice and AstrBot Plugin Authors.
Licensed under the GNU Affero General Public License v3.0 or later.
"""
