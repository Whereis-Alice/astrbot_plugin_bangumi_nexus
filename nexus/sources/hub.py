"""数据源集合：一次构造，处处注入。

服务层只需要拿到一个 「SourceHub」，就能访问全部九个数据源，而不必各自 new 一遍、
各自持有 HTTP 客户端。配置变更时由 「reconfigure」 统一刷新。

Copyright (C) 2026 Whereis-Alice and AstrBot Plugin Authors.
Licensed under the GNU Affero General Public License v3.0 or later.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..http import HttpClient
from .age import AgeSource
from .anime1 import Anime1Source
from .bangumi import BangumiSource
from .bangumi_data import BangumiDataSource
from .mikan import MikanSource
from .moegirl import MoegirlSource
from .rss import RssSource
from .yuc import YucSource


@dataclass
class SourceHub:
    """所有数据源适配器的容器。"""

    http: HttpClient
    bangumi: BangumiSource
    bangumi_data: BangumiDataSource
    anime1: Anime1Source
    yuc: YucSource
    age: AgeSource
    moegirl: MoegirlSource
    mikan: MikanSource
    rss: RssSource

    @classmethod
    def build(cls, http: HttpClient, *, bangumi_token: str = "") -> SourceHub:
        return cls(
            http=http,
            bangumi=BangumiSource(http, access_token=bangumi_token),
            bangumi_data=BangumiDataSource(http),
            anime1=Anime1Source(http),
            yuc=YucSource(http),
            age=AgeSource(http),
            moegirl=MoegirlSource(http),
            mikan=MikanSource(http),
            rss=RssSource(http),
        )

    def set_bangumi_token(self, token: str) -> None:
        """token 变更不需要重建整个 hub，只换掉 Bangumi 客户端。"""

        self.bangumi = BangumiSource(self.http, access_token=token or "")

    def stats(self) -> dict[str, Any]:
        return {
            "http": self.http.stats(),
            "bangumi_data": self.bangumi_data.stats(),
            "anime1": self.anime1.stats(),
            "yuc": self.yuc.stats(),
            "age": self.age.stats(),
        }
