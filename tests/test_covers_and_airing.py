"""封面瘦身与「长期连载」补番的单测。

这两块都属于「上一版明明有数据、卡片上却什么都不显示」的坑，
所以逐条锁住：一条锁封面字典的键值对应关系（错了整卡退化成占位块），
一条锁深夜档的星期归组（错了同一部番会在两个星期里各出现一次）。
"""

from __future__ import annotations

import asyncio
import io
from datetime import UTC, datetime

import pytest

from nexus import images
from nexus.models import DataItem, SiteRef
from nexus.services import base as service_base
from nexus.sources.bangumi_data import BangumiDataSource
from nexus.titles import parse_broadcast


class TestBgmCoverSize:
    """bgm 图床的尺寸档位改写。改错会直接 404，宁可不改。"""

    def test_large_to_common(self) -> None:
        url = "https://lain.bgm.tv/pic/cover/l/9c/8f/1234_abcd.jpg"
        assert images.bgm_cover_size(url) == ("https://lain.bgm.tv/pic/cover/c/9c/8f/1234_abcd.jpg")

    def test_strips_server_resize_prefix(self) -> None:
        """带 「/r/400/」 的链接要把这层去掉，否则等于缩两次。"""

        url = "https://lain.bgm.tv/r/400/pic/cover/l/9c/8f/1234_abcd.jpg"
        assert images.bgm_cover_size(url, "m") == (
            "https://lain.bgm.tv/pic/cover/m/9c/8f/1234_abcd.jpg"
        )

    def test_unknown_host_untouched(self) -> None:
        url = "https://example.com/poster.png"
        assert images.bgm_cover_size(url) == url

    def test_unknown_size_untouched(self) -> None:
        url = "https://lain.bgm.tv/pic/cover/l/9c/8f/1234_abcd.jpg"
        assert images.bgm_cover_size(url, "xl") == url


class TestShrink:
    """降采样。压不动的时候必须原样返回，不能把卡片搞没图。"""

    @staticmethod
    def _png(size: tuple[int, int]) -> bytes:
        pillow = pytest.importorskip("PIL.Image")
        buffer = io.BytesIO()
        pillow.new("RGB", size, (200, 120, 160)).save(buffer, format="PNG")
        return buffer.getvalue()

    def test_downscales_large_image(self) -> None:
        payload = self._png((1200, 1600))
        shrunk, mime = images.shrink(payload, max_edge=320)
        assert mime == "image/jpeg"
        assert len(shrunk) < len(payload)

    def test_small_image_untouched(self) -> None:
        payload = self._png((80, 80))
        shrunk, mime = images.shrink(payload, max_edge=320)
        assert shrunk is payload
        assert mime == ""

    def test_zero_edge_is_noop(self) -> None:
        payload = self._png((1200, 1600))
        assert images.shrink(payload, max_edge=0) == (payload, "")

    def test_garbage_falls_back(self) -> None:
        assert images.shrink(b"not an image", max_edge=320) == (b"not an image", "")


class _FakeHttp:
    """只实现 「data_uris」 的假客户端，签名与真实现一致（按原始 URL 建键）。"""

    def __init__(self, table: dict[str, str]) -> None:
        self.table = table
        self.max_edge = -1

    async def data_uris(self, urls: list[str], *, max_edge: int = 0) -> dict[str, str]:
        self.max_edge = max_edge
        return {url: self.table[url] for url in urls if url in self.table}


class TestCoverMap:
    """封面字典。「data_uris」 返回的是「URL -> data URI」，不能拿它跟入参 zip。"""

    def test_maps_business_key_to_data_uri(self) -> None:
        http = _FakeHttp(
            {
                "https://img/a.jpg": "data:image/jpeg;base64,AAA",
                "https://img/b.jpg": "data:image/jpeg;base64,BBB",
            }
        )
        deps = type("Deps", (), {"http": http})()
        result = asyncio.run(
            service_base.cover_map(
                deps,
                ((1, "https://img/a.jpg"), (2, "https://img/b.jpg")),
            )
        )
        assert result == {
            1: "data:image/jpeg;base64,AAA",
            2: "data:image/jpeg;base64,BBB",
        }

    def test_missing_url_absent_and_order_independent(self) -> None:
        """抓失败的键直接缺席，且不能因为少一个就把后面的值串位。"""

        http = _FakeHttp({"https://img/b.jpg": "data:image/jpeg;base64,BBB"})
        deps = type("Deps", (), {"http": http})()
        result = asyncio.run(
            service_base.cover_map(
                deps,
                ((1, "https://img/a.jpg"), (2, "https://img/b.jpg")),
            )
        )
        assert result == {2: "data:image/jpeg;base64,BBB"}

    def test_passes_thumb_edge(self) -> None:
        """瓦片封面必须带尺寸上限，否则十二张内联能把渲染服务撑爆。"""

        http = _FakeHttp({})
        deps = type("Deps", (), {"http": http})()
        asyncio.run(service_base.cover_map(deps, ((1, "https://img/a.jpg"),)))
        assert http.max_edge > 0


class TestBroadcastSlot:
    """放送时刻与星期归组。"""

    def test_late_night_keeps_japan_clock(self) -> None:
        """深夜档必须跟归组同口径：既然归到日本的周一，钟点也得写日本的 01:30。

        锁这条是因为以前它按 Bot 本地时区写成 「24:30」，而归组用日本日期，
        两边差一天 —— 卡片上就会出现「周一那栏写着周日深夜的时刻」。
        """

        slot = parse_broadcast("R/2026-03-01T16:30:00Z/P7D")
        assert slot is not None
        assert slot.slot_label == "深夜 01:30"
        assert slot.jst_time == "01:30"

    def test_daytime_keeps_plain_clock(self) -> None:
        """白天档不加前缀，直接给日本当地钟点。"""

        slot = parse_broadcast("R/2026-02-01T23:30:00Z/P7D")
        assert slot is not None
        assert slot.slot_label == "08:30"

    def test_air_weekday_follows_japan(self) -> None:
        """归组必须用日本当地日期，跟 Bangumi 的 「air_weekday」 一个口径。"""

        slot = parse_broadcast("R/2026-03-01T16:30:00Z/P7D")
        assert slot is not None
        assert slot.air_weekday == 1
        # 「label」 的星期必须跟 「air_weekday」 指的是同一天，不能各说各话。
        assert slot.label().startswith("周一")

    def test_rejects_garbage(self) -> None:
        assert parse_broadcast("每周日晚八点") is None


def _item(
    title: str,
    *,
    begin: str,
    end: str = "",
    broadcast: str,
    bangumi_id: str = "",
    type_: str = "tv",
) -> DataItem:
    return DataItem(
        title=title,
        titles=(title,),
        type=type_,
        begin=begin,
        end=end,
        broadcast=broadcast,
        sites=(SiteRef(site="bangumi", id=bangumi_id),) if bangumi_id else (),
    )


class TestIsAiring:
    """「正在放送」判定。放宽一点会捞出僵尸条目，收紧一点会漏掉年番。"""

    NOW = datetime(2026, 8, 30, 4, 0, tzinfo=UTC)

    @property
    def source(self) -> BangumiDataSource:
        return BangumiDataSource(http=None)  # type: ignore[arg-type]

    def test_running_year_long_show(self) -> None:
        item = _item("年番", begin="2026-02-01T08:30:00Z", broadcast="R/2026-02-01T08:30:00Z/P7D")
        assert self.source.is_airing(item, self.NOW) is True

    def test_finished_show_excluded(self) -> None:
        item = _item(
            "已完结",
            begin="2026-01-05T15:00:00Z",
            end="2026-03-30T15:00:00Z",
            broadcast="R/2026-01-05T15:00:00Z/P7D",
        )
        assert self.source.is_airing(item, self.NOW) is False

    def test_not_started_excluded(self) -> None:
        item = _item("下季", begin="2026-10-01T15:00:00Z", broadcast="R/2026-10-01T15:00:00Z/P7D")
        assert self.source.is_airing(item, self.NOW) is False

    def test_stale_open_ended_excluded(self) -> None:
        """end 留空又开播于两年前的条目大概率是没人维护，不该混进今日放送。"""

        item = _item("僵尸", begin="2020-04-01T15:00:00Z", broadcast="R/2020-04-01T15:00:00Z/P7D")
        assert self.source.is_airing(item, self.NOW) is False

    def test_movie_excluded(self) -> None:
        """剧场版的 「begin」 是上映日，放进放送表只是噪音。"""

        item = _item(
            "剧场版",
            begin="2026-08-01T00:00:00Z",
            broadcast="R/2026-08-01T00:00:00Z/P7D",
            type_="movie",
        )
        assert self.source.is_airing(item, self.NOW) is False


class TestLongRunning:
    """长期连载栏：按星期筛，且要排掉每日放送已经收录过的条目。"""

    NOW = datetime(2026, 8, 30, 4, 0, tzinfo=UTC)

    def _source(self) -> BangumiDataSource:
        source = BangumiDataSource(http=None)  # type: ignore[arg-type]
        items = (
            # 日本时间周日 17:30 开播的年番
            _item(
                "周日年番",
                begin="2026-02-01T08:30:00Z",
                broadcast="R/2026-02-01T08:30:00Z/P7D",
                bangumi_id="1001",
            ),
            _item(
                "周一年番",
                begin="2026-02-02T08:30:00Z",
                broadcast="R/2026-02-02T08:30:00Z/P7D",
                bangumi_id="1002",
            ),
        )
        source._by_month[(2026, 2)] = items
        source._index(items)
        return source

    async def _warm_noop(self, *, span: int = 2) -> int:
        return 0

    def test_filters_by_weekday(self, monkeypatch: pytest.MonkeyPatch) -> None:
        source = self._source()
        monkeypatch.setattr(source, "warm", self._warm_noop)
        found = asyncio.run(source.long_running(weekday=7, now=self.NOW))
        assert [item.title for item, _ in found] == ["周日年番"]

    def test_excludes_calendar_hits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """已经出现在每日放送里的条目不能再出现在长期连载栏，否则一部番显示两次。"""

        source = self._source()
        monkeypatch.setattr(source, "warm", self._warm_noop)
        found = asyncio.run(source.long_running(weekday=7, exclude=["1001"], now=self.NOW))
        assert found == ()

    def test_cached_lookup_does_not_fetch(self) -> None:
        source = self._source()
        assert source.cached_by_bangumi_id(1001) is not None
        assert source.cached_by_bangumi_id(9999) is None
