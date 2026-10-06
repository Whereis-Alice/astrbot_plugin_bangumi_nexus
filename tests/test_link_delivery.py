"""网址必须可点：覆盖主动推送、图片回退以及跨季入口误配。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bs4 import BeautifulSoup
from conftest import plugin_module

from nexus.activity import ActivityLog
from nexus.config import NexusConfig
from nexus.links import append_caption, link_caption
from nexus.models import (
    Anime1Entry,
    DataItem,
    FeedItem,
    MatchResult,
    Notification,
    SiteRef,
    Subject,
)
from nexus.render import (
    CardEngine,
    CardRequest,
    RenderedCard,
    build_gacha_card,
    build_notice_card,
    build_subject_card,
)
from nexus.services.base import Reply
from nexus.services.matcher import Matcher
from nexus.services.notifier import Notifier

BGM = "https://bgm.tv/subject/400602"
WATCH = "https://anime1.me/?cat=1833"
SUBJECT = Subject(id=400602, name="葬送的芙莉莲", air_date="2023-09-29", url=BGM)


def make_matcher():
    item = DataItem(
        title=SUBJECT.name, titles=(SUBJECT.name,), sites=(SiteRef("bangumi", "400602", BGM),)
    )
    data = SimpleNamespace(
        month=AsyncMock(),
        by_bangumi_id=AsyncMock(return_value=item),
        by_title=AsyncMock(return_value=(None, 0)),
        watch_links=lambda item: [("Bangumi", BGM)] if item else [],
    )
    hub = SimpleNamespace(
        bangumi_data=data,
        bangumi=SimpleNamespace(
            subject=AsyncMock(return_value=SUBJECT), search=AsyncMock(return_value=[SUBJECT])
        ),
        anime1=SimpleNamespace(match=AsyncMock(return_value=Anime1Entry(1833, SUBJECT.name))),
        age=SimpleNamespace(match=AsyncMock(return_value=None)),
    )
    return Matcher(hub), hub


def make_notifier(*, enabled=True, image=True):
    deps = SimpleNamespace(
        conf=NexusConfig(show_watch_text=enabled, persona_reply_enabled=False),
        activity=ActivityLog(),
        context=SimpleNamespace(send_message=AsyncMock(return_value=True)),
        matcher=SimpleNamespace(
            notification_links=AsyncMock(return_value=(("Bangumi 条目", BGM), ("anime1", WATCH)))
        ),
        store=SimpleNamespace(get_pref=AsyncMock(return_value="")),
        engine=SimpleNamespace(
            render=AsyncMock(
                return_value=RenderedCard(
                    text="下载完成", image_url="https://example.com/card.png" if image else ""
                )
            )
        ),
    )
    return Notifier(deps), deps


def notice():
    return Notification(
        "download_complete",
        SUBJECT.name,
        lines=("第 1 集下载完成",),
        link=BGM,
        payload={"subject_id": SUBJECT.id},
    )


def chain_text(chain):
    return "\n".join(c.text for c in chain.chain if hasattr(c, "text"))


def test_条目链接不冒充在线观看且去重():
    text = link_caption(
        [("Bangumi 条目", BGM), ("Bangumi", BGM), ("anime1", WATCH), ("官网", "javascript:bad")]
    )
    assert text.startswith("▶ 在线观看\nanime1")
    assert "相关链接\nBangumi 条目" in text
    assert text.count(BGM) == 1
    assert "javascript" not in text


def test_卡片正文不含链接区或条目网址():
    match = MatchResult(
        subject=SUBJECT,
        data_item=DataItem(SUBJECT.name, (SUBJECT.name,), official_site="https://frieren.example/"),
    )
    cards = [
        build_subject_card("sakura", match),
        build_gacha_card("sakura", match),
        build_notice_card("sakura", eyebrow="TEST", title="下载完成", lines=("第 1 集",)),
    ]
    for html in cards:
        text = BeautifulSoup(html, "html.parser").get_text()
        assert BGM not in text and "https://frieren.example/" not in text
        assert 'class="links"' not in html
        assert "在线观看" not in text and "去哪看" not in text


@pytest.mark.parametrize("image", [True, False])
async def test_下载通知图片或文本均带可点击入口(image):
    notifier, deps = make_notifier(image=image)
    chain = await notifier.build_chain(notice(), "group")
    text = chain_text(chain)
    assert text.count(BGM) == 1 and text.count(WATCH) == 1
    deps.matcher.notification_links.assert_awaited_once_with(SUBJECT.id, SUBJECT.name)


async def test_关闭链接后不查入口():
    notifier, deps = make_notifier(enabled=False)
    text = chain_text(await notifier.build_chain(notice(), "group"))
    assert WATCH not in text and BGM not in text
    deps.matcher.notification_links.assert_not_awaited()


async def test_入口失败不影响卡片和原链接():
    notifier, deps = make_notifier()
    deps.matcher.notification_links.side_effect = TimeoutError
    text = chain_text(await notifier.build_chain(notice(), "group"))
    assert BGM in text


async def test_渲染彻底失败仍有事实及链接():
    notifier, _ = make_notifier()
    notifier._render = AsyncMock(return_value=None)
    notifier._persona_line = AsyncMock(return_value="下载好啦")
    text = chain_text(await notifier.build_chain(notice(), "group", persona=True))
    assert "第 1 集下载完成" in text and BGM in text and WATCH in text


async def test_多个群只查一次观看入口():
    notifier, deps = make_notifier()
    assert await notifier.dispatch(notice(), ["group-a", "group-b"]) == 2
    deps.matcher.notification_links.assert_awaited_once()
    assert deps.context.send_message.await_count == 2


async def test_宽rss只发资源页不乱猜作品():
    notifier, deps = make_notifier()
    item = FeedItem(uid="1", title="站点更新", link="https://example.com/news")
    event = Notification("rss_update", "每日资讯", link=item.link, payload={"feed_items": [item]})
    text = chain_text(await notifier.build_chain(event, "group"))
    assert text.count(item.link) == 1
    deps.matcher.notification_links.assert_not_awaited()


@pytest.mark.parametrize("image", [True, False])
async def test_send_reply保留caption且文本回退不重复(image):
    notifier, deps = make_notifier(image=image)
    caption = link_caption([("anime1", WATCH)])
    plain = append_caption("今日播报", caption)
    deps.engine.render.return_value.text = plain
    await notifier.send_reply(
        Reply(text=plain, caption=caption, card=CardRequest("html", plain)), "group"
    )
    assert chain_text(deps.context.send_message.call_args.args[1]).count(WATCH) == 1


async def test_旧番先加载首播月份再按id取数据():
    matcher, hub = make_matcher()
    hit, _ = await matcher._resolve_data_item(SUBJECT, (SUBJECT.name,))
    hub.bangumi_data.month.assert_awaited_once_with(2023, 9)
    assert hit.bangumi_id == "400602"


async def test_标题相似也不能带入不同bangumi条目():
    matcher, hub = make_matcher()
    hub.bangumi_data.by_bangumi_id.return_value = None
    hub.bangumi_data.by_title.return_value = (
        DataItem(SUBJECT.name, (SUBJECT.name,), sites=(SiteRef("bangumi", "515759"),)),
        0.95,
    )
    assert await matcher._resolve_data_item(SUBJECT, (SUBJECT.name,)) == (None, 0.0)


def test_旧番结束后不循环生成下一集倒计时():
    matcher, _ = make_matcher()
    match = MatchResult(
        subject=SUBJECT,
        data_item=DataItem(
            SUBJECT.name,
            (SUBJECT.name,),
            end="2024-03-22T14:00:00Z",
            broadcast="R/2023-09-29T14:00:00Z/P7D",
        ),
    )
    assert matcher.next_air_label(match) == "已完结"


async def test_慢站被取消但快站结果保留且缓存(monkeypatch):
    matcher, hub = make_matcher()
    cancelled = asyncio.Event()

    async def slow(*names):
        try:
            await asyncio.sleep(30)
        finally:
            cancelled.set()

    hub.age.match.side_effect = slow
    monkeypatch.setattr("nexus.services.matcher.LINK_TIMEOUT", 0.02)
    links = await matcher.notification_links(SUBJECT.id, SUBJECT.name)
    assert ("anime1", WATCH) in links and cancelled.is_set()
    assert await matcher.notification_links(SUBJECT.id, SUBJECT.name) == links
    hub.bangumi.subject.assert_awaited_once()


async def test_第一季不接续作anime1入口():
    matcher, hub = make_matcher()
    hub.anime1.match.return_value = Anime1Entry(2000, SUBJECT.name + " 第二季")
    links = await matcher.notification_links(SUBJECT.id, SUBJECT.name)
    assert not any("cat=" in url for _, url in links)


async def test_通知资料与链接共用条目查询():
    matcher, hub = make_matcher()
    assert await matcher.notification_subject(SUBJECT.id, SUBJECT.name) is SUBJECT
    await matcher.notification_links(SUBJECT.id, SUBJECT.name)
    hub.bangumi.subject.assert_awaited_once()


async def test_没有id时不把续作当首季资料():
    matcher, hub = make_matcher()
    hub.bangumi.search.return_value = [Subject(515759, SUBJECT.name + " 第二季")]
    assert await matcher.notification_subject(0, SUBJECT.name) is None
    assert await matcher.notification_links(0, SUBJECT.name) == ()
    hub.bangumi.search.assert_awaited_once()


async def test_t2i只渲染正文但文本回退保留网址(tmp_path):
    star = SimpleNamespace(text_to_image=AsyncMock(return_value="https://example.com/card.png"))
    engine = CardEngine(star, tmp_path)
    plain = append_caption("第 1 集下载完成", link_caption([("anime1", WATCH)]))
    request = CardRequest("", plain)
    result = await engine.render(request, NexusConfig(card_renderer="t2i"))
    assert WATCH not in star.text_to_image.call_args.args[0]
    assert WATCH not in str(request.raster_card())
    assert WATCH in result.text


async def test_含链接长文本不会被入口再次转图():
    main = plugin_module("main")
    plugin = main.BangumiNexusPlugin.__new__(main.BangumiNexusPlugin)
    plugin._text_image = AsyncMock()
    text = "介绍" * 120 + "\n" + WATCH
    components = await plugin._compose(Reply.plain(text), conf=NexusConfig())
    assert components[0].text == text
    plugin._text_image.assert_not_awaited()
