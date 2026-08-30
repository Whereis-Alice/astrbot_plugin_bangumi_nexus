"""卡片构造器与服务层调用点的签名一致性，外加今日放送卡的两栏结构。

为什么单独锁这一条：v1.1.0 给 「today」/「digest」 加了「放送钟点」和「长期连载」两栏，
服务层已经按新口径传 「times=」「long_running=」，模板那边却漏改，
结果 「/今日新番」 和每日播报每次都抛 「TypeError: unexpected keyword argument」。
当时的测试全在测纯函数和数据源，没有任何一条走到「服务层怎么调模板」这条缝上，
于是这个必现崩溃一路发到线上。这里用 AST 把所有调用点的关键字参数跟真实签名对一遍,
以后再有人改模板签名忘了改调用方（或反过来），测试会直接红。
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from nexus import render
from nexus.models import CalendarDay, Subject
from nexus.render.template import build_today_card
from nexus.services.search import _sort_note

PACKAGE_ROOT = Path(render.__file__).resolve().parent.parent


def _card_builders() -> dict[str, inspect.Signature]:
    """「render」 包导出的全部卡片构造器 → 它们的签名。"""

    found = {}
    for name in dir(render):
        if not (name.startswith("build_") and name.endswith("_card")):
            continue
        found[name] = inspect.signature(getattr(render, name))
    return found


def _call_sites() -> list[tuple[str, str, int, set[str]]]:
    """扫全包源码，收集每个 「build_*_card(...)」 调用点用到的关键字参数名。

    只看关键字：位置参数的个数错了 Python 自己会在导入期或首次调用时炸得很明显，
    而拼错、过时的关键字参数只有真正走到那一行才会炸 —— 正是这次翻车的形态。
    """

    sites = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        if "tests" in path.parts or path.name.startswith("test_"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
            if not (name.startswith("build_") and name.endswith("_card")):
                continue
            keywords = {kw.arg for kw in node.keywords if kw.arg}
            sites.append((name, path.name, node.lineno, keywords))
    return sites


class TestCardCallSignatures:
    """调用点与构造器签名必须对得上，否则就是一个必现的运行期崩溃。"""

    def test_every_keyword_exists_on_the_builder(self) -> None:
        builders = _card_builders()
        sites = _call_sites()
        assert sites, "一个调用点都没扫到，说明 AST 扫描本身坏了"
        problems = []
        for name, filename, lineno, keywords in sites:
            signature = builders.get(name)
            if signature is None:
                problems.append(f"{filename}:{lineno} 调了未导出的 {name}")
                continue
            unknown = keywords - set(signature.parameters)
            if unknown:
                problems.append(f"{filename}:{lineno} {name} 不认识 {sorted(unknown)}")
        assert not problems, "；".join(problems)

    def test_builders_are_all_covered_by_exports(self) -> None:
        """构造器必须从 「render」 包导出，否则上面那条扫描会有盲区。"""

        assert len(_card_builders()) >= 10


def _subject(subject_id: int, name: str, *, score: float = 0.0) -> Subject:
    return Subject(id=subject_id, name=name, name_cn=name, score=score)


class TestTodayCard:
    """今日放送卡的两栏结构。这些信息抓到了却没进 HTML 就等于没抓。"""

    @staticmethod
    def _day() -> CalendarDay:
        return CalendarDay(
            weekday=3,
            label="周三",
            items=(_subject(1, "甲番", score=8.1), _subject(2, "乙番", score=7.4)),
        )

    def test_air_clock_reaches_the_html(self) -> None:
        """钟点是这张卡的主信息，掉了用户就得再问一次「几点播」。"""

        html = build_today_card("sakura", self._day(), times={1: "深夜 01:30", 2: "22:00"})
        assert "深夜 01:30" in html
        assert "22:00" in html

    def test_long_running_gets_its_own_block(self) -> None:
        """年番单独一栏：混进主列表会让「今天共 N 部」这个数字失真。"""

        extra = _subject(99, "某年番")
        html = build_today_card(
            "sakura",
            self._day(),
            times={99: "18:00"},
            long_running=[extra],
        )
        assert "长期连载" in html
        assert "某年番" in html
        # 主栏统计只数当季那两部，不能被补进来的年番顶高。
        assert '<b class="accent">2</b>' in html
        assert "LONG RUN" in html

    def test_long_running_tiles_are_unnumbered(self) -> None:
        """补番不参与当季排名，带上序号会像是评分排到了前面。"""

        html = build_today_card("sakura", self._day(), long_running=[_subject(99, "某年番")])
        assert html.count('class="rank"') == 2

    def test_empty_day_still_shows_long_running(self) -> None:
        """当季那栏空了也不该把年番一起吞掉，否则用户以为今天没番。"""

        blank = CalendarDay(weekday=3, label="周三", items=())
        html = build_today_card("sakura", blank, long_running=[_subject(99, "某年番")])
        assert "某年番" in html
        assert "今天没有查到放送记录" not in html

    def test_order_note_is_honest(self) -> None:
        """副标题会跟着播报的排序配置走，不能一直硬写「按评分」。"""

        assert _sort_note("doing", "desc") == "按在看人数从高到低排列"
        assert _sort_note("name", "asc") == "按名称从低到高排列"
        assert _sort_note("nonsense", "desc") == "按评分从高到低排列"
        html = build_today_card("sakura", self._day(), order_note="按在看人数从高到低排列")
        assert "按在看人数从高到低排列" in html
