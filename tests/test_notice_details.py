"""通知资料补全不能改集数、混入配角、拖住推送或在文字回退时丢失。"""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bs4 import BeautifulSoup

from nexus.activity import ActivityLog
from nexus.config import NexusConfig, load_config
from nexus.models import CharacterProfile, FeedItem, NoticeDetails, Notification, Subject
from nexus.render import RenderedCard, build_feed_card, build_notice_card
from nexus.services.notice_details import load_notice_details, snippet
from nexus.services.notifier import Notifier
from nexus.sources.bangumi import BangumiSource


def deps_for(limit=3):
    return SimpleNamespace(
        conf=NexusConfig(
            notify_character_limit=limit, persona_reply_enabled=False, show_watch_text=False
        ),
        activity=ActivityLog(),
        matcher=SimpleNamespace(
            notification_subject=AsyncMock(
                return_value=Subject(
                    1,
                    "测试动画",
                    summary="作品简介" * 100,
                    infobox={
                        "导演": "监督甲",
                        "动画制作": "工作室乙",
                        "官方网站": "https://example.org",
                    },
                )
            )
        ),
        hub=SimpleNamespace(
            bangumi=SimpleNamespace(
                main_characters=AsyncMock(
                    return_value=(CharacterProfile(7, "主角甲", "人物简介" * 100, "声优甲"),)
                )
            )
        ),
        context=SimpleNamespace(send_message=AsyncMock(return_value=True)),
        store=SimpleNamespace(get_pref=AsyncMock(return_value="")),
        engine=SimpleNamespace(
            render=AsyncMock(return_value=RenderedCard(image_url="https://example.org/card.png"))
        ),
    )


def event(kind="download_complete"):
    return Notification(kind, "测试动画", lines=("第 3 集下载完成",), payload={"subject_id": 1})


@pytest.mark.parametrize(
    "raw,expected",
    [
        ({}, 3),
        ({"notify_character_limit": 0}, 0),
        ({"notify_character_limit": 99}, 6),
        ({"notify_character_limit": -1}, 0),
        ({"notify_character_limit": "2"}, 2),
        ({"notify_character_limit": "bad"}, 3),
    ],
)
def test_limit_config(raw, expected):
    assert load_config(raw).notify_character_limit == expected


async def test_bounded_details_and_core_staff():
    deps = deps_for()
    details = await load_notice_details(deps, event())
    assert len(details.summary) == 220
    assert len(details.characters[0].summary) == 90
    assert details.staff == (("导演", "监督甲"), ("动画制作", "工作室乙"))
    assert "http" not in details.plain_text()
    deps.hub.bangumi.main_characters.assert_awaited_once_with(1, limit=3)


async def test_zero_does_not_request_characters_but_keeps_staff():
    deps = deps_for(0)
    details = await load_notice_details(deps, event())
    assert details.summary and details.staff and not details.characters
    deps.hub.bangumi.main_characters.assert_not_awaited()


async def test_character_failure_preserves_subject():
    deps = deps_for()
    deps.hub.bangumi.main_characters.side_effect = TimeoutError
    details = await load_notice_details(deps, event())
    assert details.summary and details.staff and not details.characters


async def test_subject_failure_is_optional():
    deps = deps_for()
    deps.matcher.notification_subject.side_effect = TimeoutError
    assert await load_notice_details(deps, event()) == NoticeDetails()


@pytest.mark.parametrize("kind", ["rss_update", "test", "daily_digest", "anirss_sync"])
async def test_unbound_general_notifications_are_not_guessed(kind):
    deps = deps_for()
    assert await load_notice_details(deps, Notification(kind, "资讯")) == NoticeDetails()
    deps.matcher.notification_subject.assert_not_awaited()


async def test_multi_target_lookup_once_even_with_links_off():
    deps = deps_for()
    assert await Notifier(deps).dispatch(event(), ["group-a", "group-b"]) == 2
    deps.matcher.notification_subject.assert_awaited_once()
    deps.hub.bangumi.main_characters.assert_awaited_once()
    assert deps.engine.render.await_count == 2


@pytest.mark.parametrize("kind", ["rss_update", "download_complete"])
async def test_notice_and_feed_include_details_and_text_fallback(kind):
    deps = deps_for()
    notification = event(kind)
    if kind == "rss_update":
        notification = replace(
            notification, payload={"subject_id": 1, "feed_items": [FeedItem("1", "第三集")]}
        )

    async def render(request, conf):
        assert "主角甲" in request.html and "工作室乙" in request.html
        raster_text = "\n".join(
            line for section in request.raster_card().sections for line in section.lines
        )
        assert "主角甲" in raster_text and "人物简介" in raster_text
        return RenderedCard(text=request.plain)

    deps.engine.render.side_effect = render
    chain = await Notifier(deps).build_chain(notification, "group")
    text = "".join(part.text for part in chain.chain if hasattr(part, "text"))
    assert "第 3 集下载完成" in text and "主角甲" in text and "监督甲" in text


async def test_total_render_failure_keeps_details():
    deps = deps_for()
    deps.engine.render.side_effect = RuntimeError("renderer offline")
    chain = await Notifier(deps).build_chain(event(), "group")
    text = "".join(part.text for part in chain.chain if hasattr(part, "text"))
    assert "主角甲" in text and "第 3 集下载完成" in text


def test_snippet_strips_markup_links_and_only_uses_first_paragraph():
    assert (
        snippet("<b>勇者</b>[b]小队[/b] https://example.org\n后面的剧情", 90, first_paragraph=True)
        == "勇者小队"
    )
    assert snippet("<script>alert(1)</script>介绍", 90) == "介绍"


def test_templates_escape_details_and_omit_empty_blocks():
    details = NoticeDetails(
        summary="<简介>", characters=(CharacterProfile(1, "<主角>", "<介绍>", "<声优>"),)
    )
    cards = [
        build_notice_card(
            "sakura", eyebrow="TEST", title="测试", lines=("本集详情",), details=details
        ),
        build_feed_card("sakura", "测试", [FeedItem("1", "本集详情")], details=details),
    ]
    for card in cards:
        assert "&lt;主角&gt;" in card and "<主角>" not in card
        text = BeautifulSoup(card, "html.parser").get_text()
        assert text.index("本集详情") < text.index("作品简介") < text.index("主角介绍")
        assert "制作信息" not in text


async def test_character_filter_before_limit_dedup_ids_not_voices():
    rows = [
        {"id": 9, "name": "路人", "relation": "配角"},
        {"id": 1, "name": "原名甲", "relation": "主角", "actors": [{"name": "相同声优"}]},
        {"id": 1, "name": "重复甲", "relation": "主角"},
        {"id": 2, "name": "原名乙", "relation": "主角", "actors": [{"name": "相同声优"}]},
        {"id": 3, "name": "第三位", "relation": "主角"},
    ]

    async def fetch(url, **kwargs):
        if "/subjects/" in url:
            return rows
        if url.endswith("/2"):
            raise TimeoutError
        return {"summary": "角色介绍", "infobox": [{"key": "简体中文名", "value": "中文甲"}]}

    http = SimpleNamespace(fetch_json=AsyncMock(side_effect=fetch))
    result = await BangumiSource(http).main_characters(1, limit=2)
    assert [c.name for c in result] == ["中文甲", "原名乙"]
    assert result[0].summary == "角色介绍" and not result[1].summary
    assert all(c.voice == "相同声优" for c in result)
    assert http.fetch_json.await_count == 3


async def test_source_zero_makes_no_request():
    http = SimpleNamespace(fetch_json=AsyncMock())
    assert await BangumiSource(http).main_characters(1, limit=0) == ()
    http.fetch_json.assert_not_awaited()
