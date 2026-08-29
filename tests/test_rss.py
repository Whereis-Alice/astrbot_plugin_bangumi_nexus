"""RSS 订阅地址构造与 Feed 解析单测。

订阅是插件里最容易「静默失效」的一环：地址拼错不会报错，
只会永远抓不到内容，所以这里把每种简写前缀的展开结果都锁住。
"""

from __future__ import annotations

from urllib.parse import quote

import pytest

from nexus.sources import rss

MIKAN = "https://mikanani.me"
RSSHUB = "https://rsshub.app"


class TestFeedBuilders:
    """各站点 Feed 地址模板。"""

    def test_mikan_bangumi(self) -> None:
        assert (
            rss.mikan_bangumi_feed("https://mikanani.me/", 3141)
            == "https://mikanani.me/RSS/Bangumi?bangumiId=3141"
        )

    def test_mikan_search_quotes_keyword(self) -> None:
        keyword = "葬送的芙莉莲"
        assert rss.mikan_search_feed(MIKAN, keyword) == (
            f"{MIKAN}/RSS/Search?searchstr={quote(keyword, safe='')}"
        )

    def test_mikan_classic(self) -> None:
        assert rss.mikan_classic_feed(MIKAN) == f"{MIKAN}/RSS/Classic"

    def test_dmhy(self) -> None:
        keyword = "迷宫饭"
        assert rss.dmhy_feed(keyword) == (
            f"https://share.dmhy.org/topics/rss/rss.xml?keyword={quote(keyword, safe='')}"
        )

    def test_rsshub_path(self) -> None:
        assert (
            rss.rsshub_feed(RSSHUB, "/bangumi/tv/calendar/today")
            == "https://rsshub.app/bangumi/tv/calendar/today"
        )

    def test_rsshub_passthrough(self) -> None:
        """已经是完整 URL 时原样返回，方便用户直接贴自建实例地址。"""

        assert rss.rsshub_feed(RSSHUB, "https://x.com/y") == "https://x.com/y"


class TestNormalizeFeedUrl:
    """简写前缀展开：这是用户实际输入的主要形态。"""

    def test_rsshub_prefix(self) -> None:
        assert rss.normalize_feed_url(
            "rsshub:/a/b", rsshub_base=RSSHUB, mikan_base=MIKAN
        ) == rss.rsshub_feed(RSSHUB, "/a/b")

    def test_mikan_numeric_goes_single_show(self) -> None:
        assert rss.normalize_feed_url(
            "mikan:3141", rsshub_base=RSSHUB, mikan_base=MIKAN
        ) == rss.mikan_bangumi_feed(MIKAN, "3141")

    def test_mikan_keyword_goes_search(self) -> None:
        assert rss.normalize_feed_url(
            "mikan:芙莉莲", rsshub_base=RSSHUB, mikan_base=MIKAN
        ) == rss.mikan_search_feed(MIKAN, "芙莉莲")

    def test_dmhy_prefix(self) -> None:
        assert rss.normalize_feed_url(
            "dmhy:关键词", rsshub_base=RSSHUB, mikan_base=MIKAN
        ) == rss.dmhy_feed("关键词")

    def test_bare_path_defaults_to_rsshub(self) -> None:
        assert rss.normalize_feed_url(
            "/a/b", rsshub_base=RSSHUB, mikan_base=MIKAN
        ) == rss.rsshub_feed(RSSHUB, "/a/b")

    def test_bare_host_gets_https(self) -> None:
        assert (
            rss.normalize_feed_url("example.com/x", rsshub_base=RSSHUB, mikan_base=MIKAN)
            == "https://example.com/x"
        )

    @pytest.mark.parametrize("value", ["http://x", "https://x/y"])
    def test_absolute_untouched(self, value: str) -> None:
        assert rss.normalize_feed_url(value, rsshub_base=RSSHUB, mikan_base=MIKAN) == value

    def test_empty(self) -> None:
        assert rss.normalize_feed_url("", rsshub_base=RSSHUB, mikan_base=MIKAN) == ""


RSS_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Mikan Project</title>
    <item>
      <title>[字幕组] 葬送的芙莉莲 [12][1080p]</title>
      <link>https://mikanani.me/Home/Episode/aaa</link>
      <pubDate>Tue, 01 Jul 2026 13:00:00 GMT</pubDate>
    </item>
    <item>
      <title>[字幕组] 葬送的芙莉莲 [11][1080p]</title>
      <link>https://mikanani.me/Home/Episode/bbb</link>
      <pubDate>Tue, 24 Jun 2026 13:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""

ATOM_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>RSSHub</title>
  <entry>
    <title>迷宫饭 第 5 话</title>
    <link href="https://example.com/e/5"/>
    <updated>2026-07-02T10:00:00Z</updated>
  </entry>
</feed>
"""


class TestParseFeed:
    """解析：RSS 2.0 与 Atom 都要走通，且尊重「limit」。"""

    def test_parse_rss(self) -> None:
        items = rss.parse_feed(RSS_SAMPLE)
        assert len(items) == 2
        assert items[0].title.endswith("[12][1080p]")
        assert items[0].link == "https://mikanani.me/Home/Episode/aaa"

    def test_parse_atom(self) -> None:
        items = rss.parse_feed(ATOM_SAMPLE)
        assert len(items) == 1
        assert items[0].title == "迷宫饭 第 5 话"
        assert items[0].link == "https://example.com/e/5"

    def test_limit(self) -> None:
        assert len(rss.parse_feed(RSS_SAMPLE, limit=1)) == 1

    def test_garbage_is_empty(self) -> None:
        assert rss.parse_feed("not xml at all") == []
        assert rss.parse_feed("") == []
