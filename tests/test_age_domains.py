"""AGE 动漫域名发现的回归锁。

AGE 官方 README 自己写着「每 2~3 个月换域名」，且用 「~~删除线~~」 标注阵亡域名。
把死域名排在前面会让每次刷新白试五六轮超时，所以必须剔干净。
"""

from __future__ import annotations

from nexus.sources.age import parse_domains, parse_recommend

NOTICE = """## AGE动漫 · 最新网址
> 最新域名：[https://www.agedm.io](https://www.agedm.io?ref=github)
>
> ~~弃用域名：[https://www.agedm.vip](https://www.agedm.vip?ref=github)（已阵亡）~~
>
> ~~弃用域名：[https://www.agefans.la](https://www.agefans.la?ref=github)（已阵亡）~~

## AGE动漫 · 易记域名
> [http://www.age.tv](http://www.age.tv?ref=github)
>
> [http://agefans.com](http://agefans.com?ref=github)

## 百度贴吧
> [https://tieba.baidu.com/f?kw=age](https://tieba.baidu.com/f?kw=age)
"""


def test_剔掉删除线域名() -> None:
    """阵亡域名一个都不能留，否则刷新时白等一串超时。"""
    domains = parse_domains(NOTICE)
    assert "https://www.agedm.vip" not in domains
    assert "https://www.agefans.la" not in domains


def test_保留活域名且最新的在最前() -> None:
    """顺序即优先级，官方公告里最新域名排第一。"""
    domains = parse_domains(NOTICE)
    assert domains[0] == "https://www.agedm.io"
    assert "https://www.age.tv" in domains
    assert "https://www.agefans.com" in domains


def test_不抓非_age_域名() -> None:
    """贴吧链接不是镜像站，混进去只会拖慢探测。"""
    assert not any("tieba" in domain for domain in parse_domains(NOTICE))


def test_空输入不炸() -> None:
    assert parse_domains("") == ()
    assert parse_recommend("") == ()


def test_封面取_data_original_而不是懒加载占位图() -> None:
    """「src」 是灰色占位图，上游插件就是这么把封面全抓成灰块的。"""
    html = (
        '<div class="video_item">'
        '<div class="video_item-title"><a href="/detail/123">测试番剧</a></div>'
        '<img src="/static/placeholder.png" data-original="/covers/1.jpg">'
        '<span class="video_item--info">更新至第 5 集</span>'
        "</div>"
    )
    items = parse_recommend(html, "https://www.agedm.io")
    assert len(items) == 1
    assert items[0].cover == "https://www.agedm.io/covers/1.jpg"
    assert items[0].url == "https://www.agedm.io/detail/123"
    assert items[0].progress == "更新至第 5 集"
