"""跨季度完结番复活回归：不猜周数，日期证据与正常年番都要保住。"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from nexus.activity import ActivityLog
from nexus.config import NexusConfig
from nexus.http import FetchError
from nexus.models import CalendarDay, DataItem, Episode, Subject
from nexus.services import airing
from nexus.services.airing import AiringFilter, episode_status, metadata_status
from nexus.services.search import SearchService, _long_running
from nexus.sources.bangumi_data import MONTH_CACHE_SECONDS, BangumiDataSource
from nexus.titles import JST, parse_broadcast

TODAY = date(2026, 10, 2)
NOW = datetime(2026, 10, 2, 8, 30, tzinfo=JST)


@pytest.fixture(autouse=True)
def freeze_search_date(monkeypatch: pytest.MonkeyPatch) -> None:
    """服务层测试固定在用户报告当天，避免真实日期改变边界预期。"""
    monkeypatch.setattr(
        "nexus.services.search.AiringFilter", lambda deps: AiringFilter(deps, now=NOW)
    )


def subject(sid: int = 1, **fields: Any) -> Subject:
    return Subject(id=sid, name=f"番剧{sid}", **fields)


def episodes(total: int, final: date, *, offset: int = 0) -> list[Episode]:
    return [
        Episode(
            id=n, sort=n + offset, ep=n, airdate=(final - timedelta(weeks=total - n)).isoformat()
        )
        for n in range(1, total + 1)
    ]


def data_item(sid: int = 1, **fields: Any) -> DataItem:
    return DataItem(title=f"番剧{sid}", titles=(f"番剧{sid}",), **fields)


@pytest.mark.parametrize("field", ["播放结束", "放送结束", "放送終了"])
def test_已有完结日期优先于空结束月表(field: str) -> None:
    item = subject(infobox={field: "2026年9月25日"})
    assert metadata_status(item, data_item(begin="2026-07-03", end=""), TODAY) == "ended"


@pytest.mark.parametrize("day, expected", [("2026-10-09", "upcoming"), ("2026-10-02", "unknown")])
def test_未到首播日不能出现在今天(day: str, expected: str) -> None:
    assert metadata_status(subject(air_date=day), None, TODAY) == expected


def test_日期到月份不猜具体开播或完结日() -> None:
    assert (
        metadata_status(subject(air_date="2026-10", infobox={"播放结束": "2026年9月"}), None, TODAY)
        == "unknown"
    )


def test_有完整连续编号分集也能判定完结() -> None:
    assert (
        episode_status(
            subject(total_episodes=12), episodes(12, date(2026, 9, 25), offset=24), None, TODAY
        )
        == "ended"
    )


def test_最终话当天保留明天移除() -> None:
    item, eps = subject(total_episodes=12), episodes(12, TODAY)
    assert episode_status(item, eps, None, TODAY) == "airing"
    assert episode_status(item, eps, None, TODAY + timedelta(days=1)) == "ended"


def test_深夜档末集按卡片归组日保留() -> None:
    data = data_item(broadcast="R/2026-07-02T16:28:00Z/P7D")
    item = subject(total_episodes=12, infobox={"播放结束": "2026年10月1日"})
    assert metadata_status(item, data, TODAY) == "airing"
    assert episode_status(item, episodes(12, date(2026, 10, 1)), data, TODAY) == "airing"


def test_分集缺页或只给录入条数不能判完结() -> None:
    eps = episodes(12, date(2026, 9, 1))
    assert episode_status(subject(total_episodes=24, eps=12), eps, None, TODAY) == "unknown"
    assert episode_status(subject(eps=12), eps, None, TODAY) == "unknown"
    assert episode_status(subject(total_episodes=12), eps[1:], None, TODAY) == "unknown"


def test_上周播过但缺少下一集日期不能直接当作本周仍播() -> None:
    eps = episodes(12, date(2026, 9, 25))
    assert episode_status(subject(total_episodes=13), eps, None, TODAY) == "unknown"


def test_仍在更新的51集年番不会因跨季消失() -> None:
    assert (
        episode_status(subject(total_episodes=51), episodes(51, date(2027, 1, 24)), None, TODAY)
        == "airing"
    )


def deps_for(items: list[Subject], eps: dict[int, list[Episode]] | None = None) -> Any:
    by_id = {item.id: item for item in items}
    bgm = SimpleNamespace(
        subject=AsyncMock(side_effect=lambda sid: by_id.get(sid)),
        episodes=AsyncMock(side_effect=lambda sid, **kw: (eps or {}).get(sid, [])),
        calendar=AsyncMock(return_value=[CalendarDay(5, "星期五", tuple(items))]),
    )
    data = SimpleNamespace(
        cached_by_bangumi_id=lambda sid: None,
        warm=AsyncMock(return_value=0),
        long_running=AsyncMock(return_value=()),
    )
    return SimpleNamespace(
        hub=SimpleNamespace(bangumi=bgm, bangumi_data=data),
        activity=ActivityLog(),
        conf=NexusConfig(),
        store=SimpleNamespace(get_pref=AsyncMock(return_value="")),
        http=SimpleNamespace(data_uris=AsyncMock(return_value={})),
    )


async def test_主日历和补充栏均排除完结条目() -> None:
    ended = subject(1, infobox={"播放结束": "2026年9月25日"})
    running = subject(2, total_episodes=51)
    deps = deps_for([ended, running], {2: episodes(51, date(2027, 1, 24))})
    checker = AiringFilter(deps, now=NOW)
    assert await checker.filter([ended, running]) == (running,)
    assert await checker.filter([ended, running], supplemental=True) == (running,)


async def test_接口失败主栏保留候选补充栏不凭空确认() -> None:
    item = subject()
    deps = deps_for([item])
    deps.hub.bangumi.subject.side_effect = FetchError("unavailable")
    deps.hub.bangumi.episodes.side_effect = FetchError("unavailable")
    checker = AiringFilter(deps, now=NOW)
    assert await checker.filter([item]) == (item,)
    assert await checker.filter([item], supplemental=True) == ()


async def test_核验超时取消任务且保留已完成的判定(monkeypatch: pytest.MonkeyPatch) -> None:
    deps = deps_for([subject(1), subject(2)])
    cancelled = asyncio.Event()

    async def fetch(sid: int) -> Subject:
        if sid == 2:
            try:
                await asyncio.sleep(60)
            finally:
                cancelled.set()
        return subject(sid, infobox={"播放结束": "2026年9月25日"})

    deps.hub.bangumi.subject.side_effect = fetch
    monkeypatch.setattr(airing, "CHECK_TIMEOUT", 0.03)
    assert await AiringFilter(deps, now=NOW).filter([subject(1), subject(2)]) == (subject(2),)
    assert cancelled.is_set()


@pytest.mark.parametrize("method", ["today", "digest"])
async def test_主栏全被过滤仍显示在播年番(method: str, monkeypatch: pytest.MonkeyPatch) -> None:
    ended, ongoing = subject(1, infobox={"播放结束": "2026年9月25日"}), subject(2)
    deps = deps_for([ended, ongoing])
    deps.hub.bangumi.calendar.return_value = [CalendarDay(5, "星期五", (ended,))]
    monkeypatch.setattr(
        "nexus.services.search._long_running", AsyncMock(return_value=((ongoing, "22:00"),))
    )
    reply = await getattr(SearchService(deps), method)("group", weekday=5)
    assert ongoing.name in reply.text
    assert ended.name not in reply.text
    assert reply.card is not None


async def test_先排掉旧番再取展示上限(monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus.models import SiteRef

    old = subject(1, infobox={"播放结束": "2026年9月25日"})
    live = subject(2, infobox={"播放结束": "2027年1月24日"})
    deps = deps_for([old, live])
    slot = parse_broadcast("R/2026-07-03T14:00:00Z/P7D")
    deps.hub.bangumi_data.long_running.return_value = tuple(
        (data_item(item.id, sites=(SiteRef(site="bangumi", id=str(item.id)),)), slot)
        for item in [old, live]
    )
    pairs = await _long_running(deps, weekday=5, days=[], limit=1)
    assert [item.id for item, _ in pairs] == [2]


class Test月表刷新:
    async def test_十月冷启动也读取二月年番(self) -> None:
        http = SimpleNamespace(fetch_json=AsyncMock(return_value=[]))
        source = BangumiDataSource(http)
        await source.airing(now=NOW)
        urls = [call.args[0] for call in http.fetch_json.await_args_list]
        assert any(url.endswith("/2026/02.json") for url in urls)

    async def test_刷新能替换完结日期并移除旧别名(self) -> None:
        http = SimpleNamespace(
            fetch_json=AsyncMock(
                return_value=[
                    {
                        "title": "旧名",
                        "begin": "2026-07-03",
                        "end": "",
                        "sites": [{"site": "bangumi", "id": "1"}],
                    }
                ]
            )
        )
        source = BangumiDataSource(http)
        await source.month(2026, 7)
        await source.month(2026, 7)
        assert http.fetch_json.await_count == 1
        http.fetch_json.return_value = [
            {
                "title": "新名",
                "begin": "2026-07-03",
                "end": "2026-09-25",
                "sites": [{"site": "bangumi", "id": "1"}],
            }
        ]
        source._month_fetched[(2026, 7)] -= MONTH_CACHE_SECONDS + 1
        await asyncio.gather(source.month(2026, 7), source.month(2026, 7))
        assert http.fetch_json.await_count == 2
        assert source.cached_by_bangumi_id(1).end == "2026-09-25"
        assert all(item.title != "旧名" for item in source._alias_index.values())

    async def test_失败不把空结果永久缓存(self) -> None:
        http = SimpleNamespace(fetch_json=AsyncMock(side_effect=FetchError("down")))
        source = BangumiDataSource(http)
        assert await source.month(2026, 7) == ()
        assert (2026, 7) not in source._by_month
        http.fetch_json.side_effect = None
        http.fetch_json.return_value = [{"title": "恢复", "begin": "2026-07-03"}]
        assert len(await source.month(2026, 7)) == 1

    async def test_失败保留旧数据但不推迟下次刷新(self) -> None:
        http = SimpleNamespace(fetch_json=AsyncMock(return_value=[{"title": "原条目"}]))
        source = BangumiDataSource(http)
        old = await source.month(2026, 7)
        source._month_fetched[(2026, 7)] -= MONTH_CACHE_SECONDS + 1
        timestamp = source._month_fetched[(2026, 7)]
        http.fetch_json.side_effect = FetchError("down")
        assert await source.month(2026, 7) == old
        assert source._month_fetched[(2026, 7)] == timestamp
