"""ani-rss 客户端的协议锁。

这份测试值钱的地方不是「代码跑得通」，而是**把从上游源码里核实到的协议细节钉住**：
「/api」 前缀、「weekLabel」 的 1=周日 偏移、包封里的 「code」 才是真状态码、
403 只重登一次。这些都是看不见的约定，一旦被「顺手简化」掉，
表现是同步静默错位（星期全差一天、失败当成功），不是报错，很难查。

Copyright (C) 2026 Whereis-Alice and AstrBot Plugin Authors.
Licensed under the GNU Affero General Public License v3.0 or later.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from nexus.http import FetchError
from nexus.sources.anirss import (
    AniRssError,
    AniRssSource,
    iso_weekday,
    normalize_base,
    parse_entry,
    parse_snapshot,
    subject_id_of,
    unwrap_payload,
)


class FakeResponse:
    """只实现 「status_code」 + 「json()」 的假响应。"""

    def __init__(self, payload: Any, status: int = 200, *, broken: bool = False) -> None:
        self.status_code = status
        self._payload = payload
        self._broken = broken

    def json(self) -> Any:
        if self._broken:
            raise ValueError("not json")
        return self._payload


class FakeHttp:
    """记录每次请求的假 HttpClient。

    「replies」 按调用顺序出队；不够时重复最后一个 —— 这样「重试后成功」
    这种两步场景只需要写两个元素。
    """

    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies) or [FakeResponse({"code": 200, "data": {}})]
        self.calls: list[dict[str, Any]] = []

    async def request(self, method: str, url: str, **kwargs: Any) -> Any:
        self.calls.append({"method": method, "url": url, **kwargs})
        reply = self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]
        if isinstance(reply, Exception):
            raise reply
        return reply


def _source(http: FakeHttp, **kwargs: Any) -> AniRssSource:
    options: dict[str, Any] = {"base": "10.0.0.5", "api_key": "secret"}
    options.update(kwargs)
    return AniRssSource(cast(Any, http), **options)


ANI = {
    "id": "a1",
    "title": "名侦探光之美少女",
    "jpTitle": "キミとアイドルプリキュア",
    "url": "https://mikanani.me/RSS/Bangumi?bangumiId=3345",
    "bgmUrl": "https://bgm.tv/subject/523174",
    "image": "https://lain.bgm.tv/pic/cover/l/aa.jpg",
    "cover": "/opt/ani-rss/cover/aa.jpg",
    "season": 1,
    "subgroup": "桜都字幕组",
    "totalEpisodeNumber": 50,
    "currentEpisodeNumber": 22,
    "score": 7.6,
    "enable": True,
    "completed": False,
    "ova": False,
    "weekLabel": "1",
    "match": ["1080P"],
    "exclude": ["繁体"],
    "standbyRssList": ["https://example.com/backup.xml"],
}


class Test地址归一:
    """用户会用五种写法填同一个地址，全都要能用。"""

    @pytest.mark.parametrize(
        ("raw", "expect"),
        [
            ("192.168.1.8", "http://192.168.1.8:7789"),
            ("192.168.1.8:7789", "http://192.168.1.8:7789"),
            ("http://nas:7789/", "http://nas:7789"),
            ("https://ani.example.com", "https://ani.example.com:7789"),
            ("  ", ""),
        ],
    )
    def test_补协议与端口(self, raw: str, expect: str) -> None:
        assert normalize_base(raw) == expect

    def test_不动带路径的反代地址(self) -> None:
        """套了反代且带子路径时，端口补在主机段上，路径原样留着。"""

        assert normalize_base("http://nas/ani") == "http://nas:7789/ani"

    def test_ipv6_字面量不被切坏(self) -> None:
        assert normalize_base("http://[::1]:7789") == "http://[::1]:7789"


class Test字段解析:
    def test_取到插件真正要用的字段(self) -> None:
        entry = parse_entry(ANI)
        assert entry is not None
        assert entry.ani_id == "a1"
        assert entry.display_title == "名侦探光之美少女"
        assert entry.total == 50
        assert entry.progress == 22
        assert entry.subject_id == 523174
        assert entry.subgroup == "桜都字幕组"
        assert entry.match == ("1080P",)
        assert entry.standby == ("https://example.com/backup.xml",)

    def test_封面只认网络地址(self) -> None:
        """「cover」 是 ani-rss 主机上的本地路径，机器人拿到只会渲染出破图。"""

        entry = parse_entry(ANI)
        assert entry is not None
        assert entry.cover == "https://lain.bgm.tv/pic/cover/l/aa.jpg"

    def test_没有标题的条目被丢掉(self) -> None:
        assert parse_entry({"id": "x", "totalEpisodeNumber": 12}) is None

    def test_非字典输入不炸(self) -> None:
        assert parse_entry(["nope"]) is None

    def test_只有日文名也认(self) -> None:
        entry = parse_entry({"id": "b", "jpTitle": "ぼっち・ざ・ろっく！"})
        assert entry is not None
        assert entry.display_title == "ぼっち・ざ・ろっく！"

    def test_一行简述带进度与字幕组(self) -> None:
        entry = parse_entry(ANI)
        assert entry is not None
        assert entry.summary() == "名侦探光之美少女 · 22/50 · 桜都字幕组"


class Test星期偏移:
    """「weekLabel」 是 1=周日，直接当 isoweekday 用会让整张表差一天。"""

    @pytest.mark.parametrize(
        ("label", "iso"),
        [("1", 7), ("2", 1), ("3", 2), ("4", 3), ("5", 4), ("6", 5), ("7", 6)],
    )
    def test_映射到_isoweekday(self, label: str, iso: int) -> None:
        assert iso_weekday(label) == iso

    def test_认不出给零(self) -> None:
        assert iso_weekday("") == 0
        assert iso_weekday(None) == 0
        assert iso_weekday("周三") == 0

    def test_条目上的星期跟着桶走(self) -> None:
        snapshot = parse_snapshot(
            {"weekList": [{"weekLabel": "4", "items": [dict(ANI, weekLabel="")]}]}
        )
        assert snapshot.entries[0].weekday == 3


class Test快照解析:
    def test_展平按周分桶的列表(self) -> None:
        snapshot = parse_snapshot(
            {
                "releaseDateList": ["2026-09-01", "2026-09-02"],
                "weekList": [
                    {"weekLabel": "1", "items": [ANI]},
                    {"weekLabel": "3", "items": [dict(ANI, id="a2", title="蔚蓝档案")]},
                ],
                "total": 2,
            }
        )
        assert [entry.ani_id for entry in snapshot.entries] == ["a1", "a2"]
        assert snapshot.total == 2
        assert snapshot.release_dates == ("2026-09-01", "2026-09-02")

    def test_同一_ani_id_只留一条(self) -> None:
        snapshot = parse_snapshot(
            {"weekList": [{"weekLabel": "1", "items": [ANI]}, {"weekLabel": "2", "items": [ANI]}]}
        )
        assert len(snapshot.entries) == 1

    def test_停用的不算在追(self) -> None:
        snapshot = parse_snapshot(
            {
                "weekList": [
                    {"weekLabel": "1", "items": [ANI, dict(ANI, id="a3", enable=False)]},
                ]
            }
        )
        assert len(snapshot.entries) == 2
        assert [entry.ani_id for entry in snapshot.active] == ["a1"]

    def test_结构不对时给空快照(self) -> None:
        assert parse_snapshot(None).entries == ()
        assert parse_snapshot({"weekList": "oops"}).entries == ()

    def test_total_缺失时按条数兜底(self) -> None:
        snapshot = parse_snapshot({"weekList": [{"weekLabel": "1", "items": [ANI]}]})
        assert snapshot.total == 1


class Test条目ID:
    @pytest.mark.parametrize(
        ("url", "expect"),
        [
            ("https://bgm.tv/subject/302286", 302286),
            ("https://bangumi.tv/subject/302286/", 302286),
            ("https://bgm.tv/", 0),
            ("", 0),
        ],
    )
    def test_从_bgm_地址解析(self, url: str, expect: int) -> None:
        assert subject_id_of(url) == expect


class Test鉴权:
    async def test_api_key_走请求头且不登录(self) -> None:
        http = FakeHttp(FakeResponse({"code": 200, "data": {"weekList": [], "total": 0}}))
        await _source(http).list_ani()
        assert len(http.calls) == 1
        assert http.calls[0]["url"] == "http://10.0.0.5:7789/api/listAni"
        assert http.calls[0]["headers"]["api-key"] == "secret"

    async def test_账号密码先登录再带_token(self) -> None:
        http = FakeHttp(
            FakeResponse({"code": 200, "data": "tok-123"}),
            FakeResponse({"code": 200, "data": {"weekList": [], "total": 0}}),
        )
        source = _source(http, api_key="", username="u", password="p")
        await source.list_ani()
        assert [call["url"] for call in http.calls] == [
            "http://10.0.0.5:7789/api/login",
            "http://10.0.0.5:7789/api/listAni",
        ]
        assert http.calls[1]["headers"]["Authorization"] == "tok-123"

    async def test_token_只登录一次(self) -> None:
        """ani-rss 登录接口会随机 sleep 0.5~5 秒，每轮都登录会白等。"""

        http = FakeHttp(
            FakeResponse({"code": 200, "data": "tok-123"}),
            FakeResponse({"code": 200, "data": {"weekList": [], "total": 0}}),
        )
        source = _source(http, api_key="", username="u", password="p")
        await source.list_ani()
        await source.list_ani()
        assert [call["url"].rsplit("/", 1)[-1] for call in http.calls] == [
            "login",
            "listAni",
            "listAni",
        ]

    async def test_403_清缓存重登一次(self) -> None:
        http = FakeHttp(
            FakeResponse({"code": 200, "data": "old"}),
            FakeResponse({"code": 403, "data": None}, 403),
            FakeResponse({"code": 200, "data": "new"}),
            FakeResponse({"code": 200, "data": {"weekList": [], "total": 0}}),
        )
        source = _source(http, api_key="", username="u", password="p")
        await source.list_ani()
        assert [call["url"].rsplit("/", 1)[-1] for call in http.calls] == [
            "login",
            "listAni",
            "login",
            "listAni",
        ]

    async def test_重登之后依旧_403_就报错(self) -> None:
        http = FakeHttp(
            FakeResponse({"code": 200, "data": "tok"}),
            FakeResponse({"code": 403, "data": None}, 403),
            FakeResponse({"code": 200, "data": "tok"}),
            FakeResponse({"code": 403, "data": None}, 403),
        )
        source = _source(http, api_key="", username="u", password="p")
        with pytest.raises(AniRssError) as caught:
            await source.list_ani()
        assert caught.value.status == 403

    async def test_api_key_模式不做重登(self) -> None:
        """API Key 不会过期，403 一定是 key 写错了，重试只是白打一次。"""

        http = FakeHttp(FakeResponse({"code": 403, "message": "无权限"}, 403))
        with pytest.raises(AniRssError):
            await _source(http).list_ani()
        assert len(http.calls) == 1

    def test_缺凭据算没配好(self) -> None:
        http = FakeHttp()
        assert _source(http, api_key="").configured is False
        assert _source(http, api_key="", username="u").configured is False
        assert _source(http, api_key="", username="u", password="p").configured is True
        assert _source(http, base="").configured is False

    def test_describe_不回显密钥(self) -> None:
        payload = _source(FakeHttp(), api_key="super-secret").describe()
        assert "super-secret" not in str(payload)
        assert payload["auth"] == "api_key"


class Test错误文案:
    async def test_404_指出端口填错(self) -> None:
        http = FakeHttp(FakeResponse(None, 404))
        with pytest.raises(AniRssError) as caught:
            await _source(http).list_ani()
        assert "7789" in caught.value.message

    async def test_包封里的错误码也算失败(self) -> None:
        """HTTP 200 但 「code: 500」 —— 只看 status_code 会把失败当成功。"""

        http = FakeHttp(FakeResponse({"code": 500, "message": "数据库锁了"}, 200))
        with pytest.raises(AniRssError) as caught:
            await _source(http).list_ani()
        assert "数据库锁了" in caught.value.message

    async def test_非_json_响应给可读提示(self) -> None:
        http = FakeHttp(FakeResponse(None, 200, broken=True))
        with pytest.raises(AniRssError) as caught:
            await _source(http).list_ani()
        assert "JSON" in caught.value.message

    async def test_连不上时不外泄底层异常类型(self) -> None:
        http = FakeHttp(FetchError("connect timeout"))
        with pytest.raises(AniRssError) as caught:
            await _source(http).list_ani()
        assert "连不上" in caught.value.message

    async def test_没填地址直接拒(self) -> None:
        http = FakeHttp()
        with pytest.raises(AniRssError):
            await _source(http, base="").list_ani()
        assert http.calls == []


class Test请求姿势:
    async def test_不走缓存也不重试(self) -> None:
        """局域网自建服务，点了同步就该看到最新结果；重试交给用户手动。"""

        http = FakeHttp(FakeResponse({"code": 200, "data": {"weekList": []}}))
        await _source(http).list_ani()
        assert http.calls[0]["retries"] == 0
        assert http.calls[0]["expect_status"] is False

    async def test_关闭校验时放行自签证书(self) -> None:
        http = FakeHttp(FakeResponse({"code": 200, "data": {"weekList": []}}))
        await _source(http, verify_tls=False).list_ani()
        assert http.calls[0]["insecure"] is True

    async def test_ping_回报条目数(self) -> None:
        http = FakeHttp(
            FakeResponse({"code": 200, "data": {"weekList": [{"weekLabel": "1", "items": [ANI]}]}})
        )
        payload = await _source(http).ping()
        assert payload["ok"] is True
        assert payload["entries"] == 1
        assert payload["active"] == 1

    async def test_refresh_all_打的是重扫端点(self) -> None:
        http = FakeHttp(FakeResponse({"code": 200, "data": True}))
        assert await _source(http).refresh_all() is True
        assert http.calls[0]["url"].endswith("/api/refreshAll")


class Test剥离包封:
    """离线导入时用户存下来的可能是整个包封，也可能只是里层的 「data」。"""

    def test_整份包封剥成_data(self) -> None:
        payload = {"code": 200, "message": "", "data": {"weekList": [], "total": 0}, "t": 1}
        assert unwrap_payload(payload) == {"weekList": [], "total": 0}

    def test_已经是_data_就原样返回(self) -> None:
        """判据是「有 weekList 就是 data 本身」—— 否则会把它当包封再剥一层剥成 None。"""

        data = {"weekList": [{"weekLabel": "1", "items": [ANI]}], "total": 1}
        assert unwrap_payload(data) is data

    def test_失败响应直接报出原因(self) -> None:
        """存下来的可能是一份 401：这时候「没有条目」远不如原始 message 有用。"""

        with pytest.raises(AniRssError) as caught:
            unwrap_payload({"code": 401, "message": "api-key 不正确", "data": None})
        assert "api-key 不正确" in caught.value.message
        assert caught.value.status == 401

    def test_没有_message_的失败也给出错误码(self) -> None:
        with pytest.raises(AniRssError) as caught:
            unwrap_payload({"code": 500, "data": None})
        assert "500" in caught.value.message

    def test_非字典原样放过(self) -> None:
        """交给 「parse_snapshot」 去给出空快照，不在这里多编一种错法。"""

        assert unwrap_payload([1, 2]) == [1, 2]

    def test_既没有_data_也没有_weeklist_时原样返回(self) -> None:
        assert unwrap_payload({"foo": 1}) == {"foo": 1}

    def test_包封剥完能直接解析(self) -> None:
        payload = {"code": 200, "data": {"weekList": [{"weekLabel": "1", "items": [ANI]}]}}
        snapshot = parse_snapshot(unwrap_payload(payload))
        assert [entry.title for entry in snapshot.entries] == ["名侦探光之美少女"]
