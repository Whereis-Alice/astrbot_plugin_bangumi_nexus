"""同集归并的回归锁。

这些标题全是从 Mikan / ANi / 喵萌的真实 feed 里抄来的。归并做错的代价不对称：
少归并只是多推一条，错归并是**静默丢消息**，用户永远等不到那一集。
所以下面每条断言都对应一个真实踩过或差点踩到的坑，别因为「看起来啰嗦」删掉。
"""

from __future__ import annotations

import pytest

from nexus.constants import EPISODE_PREFER_DEFAULT
from nexus.dedup import (
    dedupe_releases,
    episode_number,
    normalize_prefer,
    prefer_score,
    release_tags,
    revision,
    series_key,
)

# ANi 的 Kirara Fantasia：同一集四个版本（Baha / ABEMA × 简体 / 繁体 × 画质）。
KIRARA = (
    "[ANi] Kirara Fantasia - 01 [720P][Baha][WEB-DL][AAC AVC][CHT][MP4]",
    "[ANi] Kirara Fantasia - 01 [1080P][Baha][WEB-DL][AAC AVC][CHT][MP4]",
    "[ANi] Kirara Fantasia - 01 [1080P][ABEMA][WEB-DL][AAC AVC][CHT][MP4]",
    "[ANi] Kirara Fantasia - 01 [720P][Baha][WEB-DL][AAC AVC][CHS][MP4]",
)

# 喵萌奶茶屋：作品名也写在括号里，同一周会发好几部不同的番。
NEKOMOE = (
    "[喵萌奶茶屋]★10月新番★[名探偵プリキュア][22][1080p][简日双语][招募翻译]",
    "[喵萌奶茶屋]★10月新番★[名探偵プリキュア][22][1080p][繁日双语][招募翻译]",
    "[喵萌奶茶屋]★10月新番★[葬送のフリーレン][22][1080p][简日双语][招募翻译]",
)


class Test集数识别:
    """认不出集数就会退化成「原样保留」，是整条链路的第一道门。"""

    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            ("[ANi] 名探偵プリキュア - 22 [1080P][Baha][WEB-DL]", "22"),
            ("[喵萌奶茶屋][名探偵プリキュア][05][1080p][简日双语]", "05"),
            ("[Sakurato] Some Show [第01话][AVC-8bit 1080p][CHS]", "01"),
            ("[Group] Some Show S01E07 [WebRip]", "07"),
            ("[Lilith-Raws] Show / EP03 [Baha][WEB-DL]", "03"),
            ("[Nekomoe kissaten][Show][13v2][1080p][JPTC]", "13"),
            ("[Group][Show][24END][1080p][CHS]", "24"),
            ("[Group] Show - 1 [1080p]", "01"),
        ],
    )
    def test_常见写法都能认出来(self, title: str, expected: str) -> None:
        assert episode_number(title) == expected

    def test_年份与画质不会被当成集数(self) -> None:
        """位数放开到四位就会把 「[2026]」「1080」 认成集数，历史上真踩过。"""

        assert episode_number("[Group][Show Movie][2026][2160p][BDRip]") == ""

    def test_剧场版与合集认不出集数(self) -> None:
        """认不出才是对的：让它们走「原样保留」，绝不能互相顶掉。"""

        assert episode_number("[Nekomoe kissaten][Some Movie][Movie][1080p][BDRip]") == ""
        assert episode_number("[Group][Show][Fin][BDBox][1080p][合集]") == ""

    def test_修订号(self) -> None:
        assert revision("[Nekomoe kissaten][Show][13v2][1080p]") == 2
        assert revision("[Nekomoe kissaten][Show][13][1080p]") == 1


class Test分组键:
    """分组键错一点点，就会把两部番并成一部。"""

    def test_同一集的不同版本共用一个键(self) -> None:
        keys = {series_key(title) for title in KIRARA}
        assert len(keys) == 1

    def test_同组不同番不能共用一个键(self) -> None:
        """喵萌把作品名也放在括号里。早期实现一律抠光括号，键只剩 「10月新番」，
        同一周的两部番互相顶掉 —— 这是本模块最贵的一课。"""

        assert series_key(NEKOMOE[0]) != series_key(NEKOMOE[2])

    def test_保留字幕组名所以跨组不归并(self) -> None:
        """有意的取舍：跨组该由用户在选源那一步收敛，这里猜不得。"""

        ani = series_key("[ANi] 名探偵プリキュア - 22 [1080P][Baha][WEB-DL][AAC AVC][CHT][MP4]")
        nekomoe = series_key("[喵萌奶茶屋][名探偵プリキュア][22][1080p][简日双语]")
        assert ani != nekomoe


class Test优选打分:
    """权重要拉开量级，否则第一优先会被后面几项之和反超。"""

    def test_第一优先压得住后面全部之和(self) -> None:
        prefer = ("简体", "1080p", "Baha", "MKV", "外挂")
        chs = prefer_score("[Group][Show][01][720p][CHS][MP4]", prefer)
        others = prefer_score("[Group][Show][01][1080p][Baha][MKV][外挂][CHT]", prefer)
        assert chs > others

    def test_非法项被洗掉且顺序不变(self) -> None:
        assert normalize_prefer(["baha", "不存在的标记", "1080P", "Baha"]) == ("Baha", "1080p")

    def test_空清单也能接受(self) -> None:
        """全非法等价于「不表态」，调用方会回落到默认顺序。"""

        assert normalize_prefer(["???", ""]) == ()
        assert normalize_prefer(None) == ()

    def test_标记识别覆盖片源与语言(self) -> None:
        tags = release_tags("[ANi] Show - 01 [1080P][Baha][WEB-DL][AAC AVC][CHT][MP4]")
        assert "Baha" in tags
        assert "繁体" in tags
        assert "1080p" in tags


class Test归并:
    """真实批次的端到端行为。"""

    def test_多片源多画质只留一条(self) -> None:
        outcome = dedupe_releases(KIRARA)
        assert len(outcome.kept) == 1
        assert outcome.merged == 3

    def test_默认顺序下简体压过画质(self) -> None:
        """默认 「简体」 在 「1080p」 前面，所以 720p 简体应当胜出 —— 这不是 bug，
        而是「先要看得懂，再要清晰」的取舍，改默认值时会被这条拦住。"""

        outcome = dedupe_releases(KIRARA, prefer=EPISODE_PREFER_DEFAULT)
        assert "CHS" in outcome.kept[0]

    def test_改优选顺序能改结果(self) -> None:
        outcome = dedupe_releases(KIRARA, prefer=("1080p", "繁体"))
        assert "1080P" in outcome.kept[0]

    def test_修订版顶掉原版(self) -> None:
        items = (
            "[ANi] 名探偵プリキュア - 22 [1080P][Baha][WEB-DL][AAC AVC][CHT][MP4]",
            "[ANi] 名探偵プリキュア - 22v2 [1080P][Baha][WEB-DL][AAC AVC][CHT][MP4]",
        )
        outcome = dedupe_releases(items)
        assert outcome.kept == (items[1],)
        assert outcome.dropped == (items[0],)

    def test_同组不同番都留下(self) -> None:
        outcome = dedupe_releases(NEKOMOE)
        assert len(outcome.kept) == 2
        assert outcome.merged == 1

    def test_认不出集数的原样保留(self) -> None:
        items = (
            "[Nekomoe kissaten][Some Movie][Movie][1080p][BDRip]",
            "[Nekomoe kissaten][Another Movie][Movie][1080p][BDRip]",
            "[Group] 招募翻译 校对 时轴 长期有效",
        )
        outcome = dedupe_releases(items)
        assert outcome.kept == items
        assert outcome.merged == 0

    def test_落选条目会被完整交回(self) -> None:
        """落选的必须一起写进已推送历史，否则下一轮又被当成新条目重新参选。"""

        outcome = dedupe_releases(KIRARA)
        assert set(outcome.kept) | set(outcome.dropped) == set(KIRARA)
        assert not set(outcome.kept) & set(outcome.dropped)

    def test_保持原始顺序(self) -> None:
        """RSS 按时间倒序，归并后不该把顺序打乱，否则通知里的先后会错。"""

        items = (
            "[Group][A 番][03][1080p][CHS]",
            "[Group][B 番][03][1080p][CHS]",
            "[Group][C 番][03][1080p][CHS]",
        )
        assert dedupe_releases(items).kept == items

    def test_支持自定义取标题(self) -> None:
        """轮询里喂的是 「FeedItem」，不是裸字符串。"""

        class Row:
            def __init__(self, name: str) -> None:
                self.name = name

        rows = [Row(title) for title in KIRARA]
        outcome = dedupe_releases(rows, title_of=lambda row: row.name)
        assert len(outcome.kept) == 1

    def test_空输入不报错(self) -> None:
        outcome = dedupe_releases(())
        assert outcome.kept == ()
        assert outcome.merged == 0
