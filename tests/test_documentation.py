"""首页保持简洁，完整参考迁到专题页后仍要可达、可复制且不遗漏功能。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

from nexus import catalog

ROOT = Path(__file__).resolve().parents[1]
DOCS = sorted((ROOT / "docs").glob("*.md"))
PAGES = [ROOT / "README.md", *DOCS]


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def without_fences(text: str) -> str:
    """代码示例里的路径和链接不是文档导航，不参与死链检查。"""
    return re.sub(r"(?ms)^```[^\n]*\n.*?^```[^\n]*$", "", text)


def anchors(text: str) -> set[str]:
    """这些文档的普通标题采用 GitHub 的小写连字符锚点，同名标题加序号。"""
    used: set[str] = set()
    for title in re.findall(r"^#{1,6} (.+)$", without_fences(text), flags=re.M):
        slug = re.sub(r"[^\w -]", "", title.lower()).replace(" ", "-")
        key, index = slug, 0
        while key in used:
            index += 1
            key = f"{slug}-{index}"
        used.add(key)
    return used


def test_首页只做入口不承载版本和参数表() -> None:
    text = read(ROOT / "README.md")
    assert len(text.splitlines()) <= 120, "完整教程和参考表请移到 docs"
    assert not re.search(r"\bv\d+\.\d+", text)
    assert not re.search(r"^## .*?(版本|修复|更新记录)", text, flags=re.M)
    assert "| 配置项 |" not in text
    assert "CHANGELOG.md" in text


@pytest.mark.parametrize("path", DOCS, ids=lambda path: path.name)
def test_专题页有首页入口且能返回(path: Path) -> None:
    assert f"docs/{path.name}" in read(ROOT / "README.md")
    assert "(../README.md)" in read(path)


@pytest.mark.parametrize("path", PAGES, ids=lambda path: path.name)
def test_本地图片链接与章节锚点存在(path: Path) -> None:
    text = without_fences(read(path))
    links = re.findall(r"!?\[[^\]\n]*\]\(([^)\n]+)\)", text)
    links += re.findall(r'(?:src|href)="([^"]+)"', text)
    for link in links:
        url = urlsplit(link)
        if url.scheme or url.netloc:
            continue
        target = (path.parent / unquote(url.path)).resolve() if url.path else path
        assert target.is_relative_to(ROOT), f"{path.name}: 跳出仓库的路径 {link}"
        assert target.is_file(), f"{path.name}: 无效链接 {link}"
        if url.fragment and target.suffix == ".md":
            assert unquote(url.fragment) in anchors(read(target)), (
                f"{path.name}: 无效章节锚点 {link}"
            )


@pytest.mark.parametrize("path", PAGES, ids=lambda path: path.name)
def test_表格列数一致避免参数竖线破坏排版(path: Path) -> None:
    columns = None
    for line in without_fences(read(path)).splitlines():
        if not line.startswith("|"):
            columns = None
            continue
        count = len(re.split(r"(?<!\\)\|", line)) - 2
        if columns is None:
            columns = count
        assert count == columns, f"{path.name}: 表格列数不一致 {line}"


def test_指令参考完整且没有遗留指令() -> None:
    text = read(ROOT / "docs/commands.md")
    names = re.findall(r"^\| `/([^\s`]+)", text, flags=re.M)
    assert len(names) == len(set(names)), "指令参考有重复行"
    assert set(names) == {cmd.name for cmd in catalog.all_commands()}
    for command in catalog.all_commands():
        for alias in command.aliases:
            assert f"`/{alias}`" in text, f"缺少别名：{alias}"


def test_配置参考完整且没有遗留配置() -> None:
    schema = json.loads(read(ROOT / "_conf_schema.json"))
    text = read(ROOT / "docs/configuration.md")
    rows = re.findall(r"^\| `([^`]+)` \| (?:string|text|int|float|bool|list) \|", text, flags=re.M)
    assert len(rows) == len(set(rows)), "配置参考有重复行"
    assert set(rows) == set(schema)


def test_webhook模板仍能作为json复制() -> None:
    text = read(ROOT / "docs/webhook.md")
    blocks = re.findall(r"```json\n(.*?)\n```", text, flags=re.S)
    assert len(blocks) == 1
    body = json.loads(blocks[0])
    for key in ("episode", "currentEpisodeNumber", "totalEpisodeNumber", "message"):
        assert body[key] == "${" + key + "}"
    assert body["url"] == "${bgmUrl}"
