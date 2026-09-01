"""排除项匹配的回归锁：双语豁免与短缩写边界。

这套判断只有两个函数，但它决定「这一集到底推不推」，错一个方向的代价都很大：
放宽了会推一堆用户明确说不要的版本，收紧了会让整集悄悄消失（用户看不到任何提示，
只会以为字幕组没出）。所以这里全部用真实的字幕组标题做样本，
每条 case 都写清「为什么锁这条」。
"""

from __future__ import annotations

from nexus.dedup import release_tags
from nexus.excludes import blocked_by, expand_excludes, is_dual_language
from nexus.models import Subscription
from nexus.services import base


class TestDualLanguageSurvives:
    """双语单文件不该被单语排除项误杀。

    「[喵萌][…][简繁日内封]」 这种一个文件里同时封了简繁日三条字幕的发布，
    在几个大组里占了很大比例。用户勾「不要繁体」 的本意是躲纯繁版，
    如果把双语单文件也丢掉，等于这一集根本收不到。
    """

    def test_简繁日内封不被繁体拦(self) -> None:
        title = "[喵萌奶茶屋][葬送的芙莉莲][05][1080p][简繁日内封]"
        assert blocked_by(title, expand_excludes(["繁体"])) == ""

    def test_chs_cht_不被繁体拦(self) -> None:
        title = "[桜都字幕组][某科学的超电磁炮][12][1080p][CHS&CHT]"
        assert blocked_by(title, expand_excludes(["繁体"])) == ""

    def test_下划线写法也算双语(self) -> None:
        """NC-Raws 惯用 「CHS_CHT」，桜都用 「CHS&CHT」，写法不同但意思一样。"""

        title = "[NC-Raws] 药师少女的独语 - 08 [B-Global][1080p][CHS_CHT]"
        assert is_dual_language(title) is True

    def test_简体侧同样豁免(self) -> None:
        """反向也要成立：勾「不要简体」 的人拿到双语单文件，也照样能挑繁体轨。"""

        title = "[LoliHouse][迷宫饭][18][WebRip 1080p][简繁日内封]"
        assert blocked_by(title, expand_excludes(["简体"])) == ""


class TestSingleLanguageStillBlocked:
    """纯单语版本必须照常拦住，否则豁免就变成了「排除项失效」。"""

    def test_纯繁日被拦(self) -> None:
        title = "[ANi] 我推的孩子 - 11 [1080P][Baha][WEB-DL][AAC AVC][CHT]"
        assert blocked_by(title, expand_excludes(["繁体"])) == "CHT"

    def test_繁日双语不算双语单文件(self) -> None:
        """「繁日」 是繁体+日文，不含简体，所以它是单语（中文侧只有繁体）版本。"""

        title = "[天月搬运组][某组作品][03][1080p][繁日双语]"
        assert is_dual_language(title) is False
        assert blocked_by(title, expand_excludes(["繁体"])) == "繁日"

    def test_勾简繁时双语单文件被拦(self) -> None:
        """真想连双语单文件一起躲，勾「简繁」 那组 —— 这是豁免的唯一出口。"""

        title = "[喵萌奶茶屋][葬送的芙莉莲][05][1080p][简繁日内封]"
        assert blocked_by(title, expand_excludes(["简繁"])) == "简繁"


class TestShortAbbrevBoundary:
    """短缩写靠自带边界防误杀，展开与匹配两侧都不能把边界 strip 掉。"""

    def test_cr_预设保留尾空格(self) -> None:
        assert "CR " in expand_excludes(["CR"])

    def test_secret_不被cr拦(self) -> None:
        """裸 「cr」 会命中 「Se-cr-et」；这是上线后真实发生过的误杀。"""

        title = "[Nekomoe kissaten][Secret Society][05][1080p][JPSC]"
        assert blocked_by(title, expand_excludes(["CR"])) == ""

    def test_sacred_不被cr拦(self) -> None:
        title = "[Sakurato][Sacred Blacksmith][02][1080p][AVC AAC]"
        assert blocked_by(title, expand_excludes(["CR"])) == ""

    def test_真正的cr源被拦(self) -> None:
        for title, hit in (
            ("[ANi] Show - 03 [1080P][CR][WEB-DL]", "[CR]"),
            ("[Group] Show - 03 (Crunchyroll 1080p)", "Crunchyroll"),
            ("[Group] Show - 03 CR WEB-DL 1080p", "CR "),
        ):
            assert blocked_by(title, expand_excludes(["CR"])) == hit

    def test_nc_raws_不被生肉拦(self) -> None:
        """「Raw」 是 「NC-Raws」「Lilith-Raws」 两个大组的名字，裸词会打死它们全部发布。"""

        title = "[NC-Raws] 药师少女的独语 - 08 [B-Global][1080p][CHS_CHT]"
        assert blocked_by(title, expand_excludes(["生肉"])) == ""

    def test_真正的生肉被拦(self) -> None:
        title = "[Group] Show - 03 [1080p][RAW]"
        assert blocked_by(title, expand_excludes(["生肉"])) == "[RAW]"

    def test_简体的gb必须带左括号(self) -> None:
        """裸 「GB」 会命中 「1GB」「Gundam Battle」，「GB]」 会命中体积标注 「[2.1GB]」。

        所以简体预设只收 「[GB」 —— 「[GB]」「[GB_JP]」 都被它覆盖，而体积标注
        不会以 「[GB」 开头，两边正好分开。
        """

        words = expand_excludes(["简体"])
        assert "[GB" in words
        assert "GB" not in words
        assert "GB]" not in words
        assert blocked_by("[Group] Show - 03 [1080p][2.1GB]", words) == ""
        assert blocked_by("[Group] Show - 03 [1080p][GB_JP]", words) == "[GB"


class TestSubscriptionMatches:
    """单条订阅自己的黑名单必须与全局/会话两层同一套规则。

    这两条路曾经分家：「Subscription.matches」 里是一句 「word in title」，
    于是双语豁免和 「CR 」 边界只在轮询那条路上生效，
    per-subscription 黑名单静默误杀，用户完全无从察觉。
    """

    def _sub(self, excludes: tuple[str, ...]) -> Subscription:
        return Subscription(id=1, umo="u", name="n", url="http://x", excludes=excludes)

    def test_双语豁免同样生效(self) -> None:
        sub = self._sub(expand_excludes(["繁体"]))
        assert sub.matches("[喵萌奶茶屋][葬送的芙莉莲][05][1080p][简繁日内封]") is True

    def test_纯繁体照常拦住(self) -> None:
        sub = self._sub(expand_excludes(["繁体"]))
        assert sub.matches("[ANi] Show - 11 [1080P][Baha][CHT]") is False

    def test_关键词白名单不受影响(self) -> None:
        sub = self._sub(())
        sub.keywords = ("芙莉莲",)
        assert sub.matches("[Group][迷宫饭][18][1080p]") is False
        assert sub.matches("[Group][葬送的芙莉莲][05][1080p]") is True


class TestFacadeReexport:
    """「services.base」 仍要转出这两个名字：订阅服务与 WebUI 都从门面取用。"""

    def test_同一个函数(self) -> None:
        assert base.blocked_by is blocked_by
        assert base.expand_excludes is expand_excludes


class TestReleaseTagBoundary:
    """选源标记 / 同集归并打分用的 「RELEASE_TAG_RULES」 也不能被体积标注骗到。

    这张表和排除项那张表用途不同（贴标签 vs 命中即丢），但踩的是同一个坑：
    「gb]」 会命中 「[2.1GB]」，于是一条纯繁体发布被标成简体，
    在同集归并里拿着「简体」的高分压掉真正的简体版 —— 用户会以为优先顺序失灵。
    """

    def test_体积标注不被标成简体(self) -> None:
        tags = release_tags("[Group] Show - 03 [1080p][2.1GB][CHT]")
        assert "简体" not in tags
        assert "繁体" in tags

    def test_真正的简体标记还在(self) -> None:
        assert "简体" in release_tags("[Group] Show - 03 [1080p][GB_JP]")
        assert "简体" in release_tags("[Group] Show - 03 [1080p][简日双语]")
