"""「/追番」 顺手订阅：从「打印三条裸 URL」改成「回复序号」。

为什么单独锁这一组：上游 「astrbot_plugin_bangumi」 在加完追番之后，
会把 Mikan / 动漫花园 / RSSHub 三条完整 RSS 地址原样打印出来，让用户自己
复制粘贴到 「/sub」。实机表现是灾难：长 URL 在聊天窗里被折行、点不动，
而且这段文字把整条回复顶过转图阈值，最后变成一张全是乱码般长链接的文字图。

这里改成跟选源同一套序号流程 —— 提示文本里只有序号和源名，地址留在会话里。
一旦有人把 「offer_from_match」 改回吐 URL，或者忘了开会话（用户回 1 没反应），
下面的断言就会当场炸。
"""

from __future__ import annotations

import pytest

from nexus.config import NexusConfig
from nexus.models import DataItem, MatchResult, SiteRef, Subject
from nexus.services.picker import PickRegistry
from nexus.services.subscriptions import SubscriptionService


class _FakeDeps:
    """只带 「conf」 和 「picker」 的极简依赖。

    「offer_from_match」 全程不碰网络、不碰数据库，用真 「Deps」 反而要把
    HTTP 池和 SQLite 都拉起来，测试会变慢且脆。
    """

    def __init__(self) -> None:
        self.conf = NexusConfig()
        self.picker = PickRegistry()


@pytest.fixture
def service() -> SubscriptionService:
    return SubscriptionService(_FakeDeps())


@pytest.fixture
def match() -> MatchResult:
    """带 bgm 条目 + bangumi-data 登记的完整匹配，三条源都能凑出来。"""

    return MatchResult(
        subject=Subject(
            id=611077,
            name="名探偵プリキュア！",
            name_cn="名侦探光之美少女",
            image="https://img/1.jpg",
        ),
        data_item=DataItem(
            title="名探偵プリキュア！",
            titles=("名探偵プリキュア！",),
            sites=(SiteRef(site="mikan", id="3883"),),
        ),
        mikan_id="3883",
        mikan_rss="https://mikanani.me/RSS/Bangumi?bangumiId=3883",
    )


class TestOfferFromMatch:
    @pytest.mark.asyncio
    async def test_提示里不出现裸_url(
        self, service: SubscriptionService, match: MatchResult
    ) -> None:
        """这是整条改动的核心诉求：聊天里不该再出现可折行的长链接。"""

        text = await service.offer_from_match("session:1", match)
        assert "http" not in text
        assert "1. " in text

    @pytest.mark.asyncio
    async def test_每个候选源都编了号(
        self, service: SubscriptionService, match: MatchResult
    ) -> None:
        """序号必须从 1 连续排，否则用户回的数字对不上 「PickOption.index」。"""

        text = await service.offer_from_match("session:1", match)
        expected = len(service.suggest(match))
        assert expected >= 2
        for index in range(1, expected + 1):
            assert f"{index}. " in text

    @pytest.mark.asyncio
    async def test_开了会话且能被数字回复命中(
        self, service: SubscriptionService, match: MatchResult
    ) -> None:
        """光打印列表不开会话，是最容易漏的一步 —— 用户回 1 会毫无反应。"""

        await service.offer_from_match("session:1", match)
        hit = service._deps.picker.resolve("session:1", "1")
        assert hit is not None
        session, option = hit
        assert session.kind == "watch"
        assert session.name == "名侦探光之美少女"
        assert session.subject_id == 611077
        assert option.url.startswith("http")

    @pytest.mark.asyncio
    async def test_没有可用源时不开会话(self, service: SubscriptionService) -> None:
        """空匹配下必须返回空串：否则卡片后面会跟一行「回复序号」的假提示。"""

        text = await service.offer_from_match("session:2", MatchResult())
        assert text == ""
        assert service._deps.picker.get("session:2") is None

    @pytest.mark.asyncio
    async def test_提示里给出手动挑字幕组的出路(
        self, service: SubscriptionService, match: MatchResult
    ) -> None:
        """三条源都是「整番混流」，想只订一个组得走 「/sub」，这条出路必须写明。"""

        text = await service.offer_from_match("session:1", match)
        assert "/sub 名侦探光之美少女" in text
