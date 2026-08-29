"""指令目录与实际注册的一致性检查。

「catalog.py」 是帮助卡、WebUI 指令页、README 表格的唯一数据源。
一旦它和 「main.py」 里真正注册的指令对不上，
用户就会照着帮助卡打出一条不存在的指令，所以这里做双向核对。
"""

from __future__ import annotations

import re
from pathlib import Path

from nexus import catalog

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
MAIN_SOURCE = (PLUGIN_ROOT / "main.py").read_text(encoding="utf-8")

REGISTERED = set(re.findall(r'filter\.command\(\s*"([^"]+)"', MAIN_SOURCE))
REGISTERED_GROUPS = set(re.findall(r'filter\.command_group\(\s*"([^"]+)"', MAIN_SOURCE))


class TestCounts:
    def test_totals(self) -> None:
        assert catalog.command_count() == len(catalog.all_commands())
        assert catalog.category_count() == len(catalog.CATEGORIES)

    def test_alias_count_matches_sum(self) -> None:
        total = sum(len(cmd.aliases) for cmd in catalog.all_commands())
        assert catalog.alias_count() == total


class TestUniqueness:
    def test_command_names_unique(self) -> None:
        names = [cmd.name for cmd in catalog.all_commands()]
        assert len(names) == len(set(names))

    def test_aliases_never_collide_with_names(self) -> None:
        names = {cmd.name for cmd in catalog.all_commands()}
        for cmd in catalog.all_commands():
            assert not (set(cmd.aliases) & names), cmd.name

    def test_aliases_unique_across_commands(self) -> None:
        seen: dict[str, str] = {}
        for cmd in catalog.all_commands():
            for alias in cmd.aliases:
                assert alias not in seen, f"{alias} 同时属于 {seen.get(alias)} 与 {cmd.name}"
                seen[alias] = cmd.name

    def test_category_keys_unique(self) -> None:
        keys = [cat.key for cat in catalog.CATEGORIES]
        assert len(keys) == len(set(keys))


class TestContent:
    def test_every_command_documented(self) -> None:
        for cmd in catalog.all_commands():
            assert cmd.usage, cmd.name
            assert cmd.summary, cmd.name

    def test_usage_starts_with_command_name(self) -> None:
        for cmd in catalog.all_commands():
            assert cmd.usage.startswith(cmd.name), cmd.name

    def test_every_category_has_commands(self) -> None:
        for cat in catalog.CATEGORIES:
            assert cat.commands, cat.key
            assert cat.title and cat.blurb and cat.icon, cat.key


class TestSyncWithMain:
    """双向核对：目录里写的指令都真的注册了，注册的指令也都写进了目录。"""

    def test_catalog_commands_are_registered(self) -> None:
        missing = [
            cmd.name
            for cmd in catalog.all_commands()
            if cmd.name not in REGISTERED and cmd.name not in REGISTERED_GROUPS
        ]
        assert missing == []

    def test_registered_commands_are_documented(self) -> None:
        documented = {cmd.name for cmd in catalog.all_commands()}
        undocumented = sorted((REGISTERED | REGISTERED_GROUPS) - documented)
        assert undocumented == []

    def test_aliases_are_declared_in_main(self) -> None:
        """帮助卡上写的别名必须真的出现在 main.py 的 alias 集合里。"""

        for cmd in catalog.all_commands():
            for alias in cmd.aliases:
                assert f'"{alias}"' in MAIN_SOURCE, f"{cmd.name} 的别名 {alias} 未注册"
