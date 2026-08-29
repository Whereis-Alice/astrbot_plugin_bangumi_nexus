"""插件的 Web 侧：Dashboard 管理面板接口 + 独立 Webhook 监听。

拆成独立子包的理由：这一层是唯一需要关心 HTTP 语义（状态码、请求头、
JSON 编解码）的地方。业务服务只吃 「Deps」、吐 「Reply」/「dict」，
换掉 Web 框架不应该波及它们。

Copyright (C) 2026 Whereis-Alice and AstrBot Plugin Authors.
Licensed under the GNU Affero General Public License v3.0 or later.
"""

from __future__ import annotations

from .api import NexusService, NexusWebApi, NexusWebError, Wiring
from .listener import WebhookListener

__all__ = [
    "NexusService",
    "NexusWebApi",
    "NexusWebError",
    "WebhookListener",
    "Wiring",
]
