"""i18n 词条与文档计数的一致性锁。

AstrBot 面板按「_conf_schema.json」的键去 i18n 里取 description / hint，
少一条就会在面板上露出裸键名；多一条则说明配置项已删但翻译忘了清。
i18n 简介中的指令条数需要跟随指令目录，避免文案漂移，
所以这里统一以「catalog.command_count()」为唯一真源反查文案。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from nexus import catalog

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
I18N_DIR = PLUGIN_ROOT / ".astrbot-plugin" / "i18n"
LOCALES = ("zh-CN", "en-US")


def _load(name: str) -> dict:
    """读一份 i18n 文件；BOM 容错，避免 Windows 编辑器写坏后测试报解码错。"""

    return json.loads((I18N_DIR / f"{name}.json").read_text(encoding="utf-8-sig"))


def _schema() -> dict:
    """配置模板本身，就是 i18n 该覆盖的键集合。"""

    return json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8-sig"))


class TestConfigCoverage:
    """每个配置项都要有中英词条，且不能有多余词条。"""

    @pytest.mark.parametrize("locale", LOCALES)
    def test_没有缺失的配置词条(self, locale: str) -> None:
        missing = sorted(set(_schema()) - set(_load(locale)["config"]))
        assert missing == [], f"{locale} 缺少词条：{missing}"

    @pytest.mark.parametrize("locale", LOCALES)
    def test_没有多余的配置词条(self, locale: str) -> None:
        orphan = sorted(set(_load(locale)["config"]) - set(_schema()))
        assert orphan == [], f"{locale} 有已删配置的残留词条：{orphan}"

    @pytest.mark.parametrize("locale", LOCALES)
    def test_词条都带描述(self, locale: str) -> None:
        blank = [
            key
            for key, item in _load(locale)["config"].items()
            if not str(item.get("description", "")).strip()
        ]
        assert blank == [], f"{locale} 这些词条没写 description：{blank}"

    @pytest.mark.parametrize("locale", LOCALES)
    def test_选项标签数量与模板对齐(self, locale: str) -> None:
        """带 options 的配置项如果给了 labels，个数必须一一对应，否则面板会错位。"""

        schema = _schema()
        bad = []
        for key, item in _load(locale)["config"].items():
            labels = item.get("labels")
            options = schema.get(key, {}).get("options")
            if labels is None or options is None:
                continue
            if len(labels) != len(options):
                bad.append(f"{key}({len(labels)}!={len(options)})")
        assert bad == [], f"{locale} 标签与选项数量不一致：{bad}"

    @pytest.mark.parametrize("locale", LOCALES)
    def test_必备段落齐全(self, locale: str) -> None:
        doc = _load(locale)
        assert set(doc) >= {"metadata", "config", "pages"}
        assert doc["metadata"]["display_name"].strip()
        assert "nexus" in doc["pages"]


class TestCommandCount:
    """指令条数只认「catalog」，文案跟着它走。"""

    def test_i18n_简介写的条数是真的(self) -> None:
        count = catalog.command_count()
        for locale in LOCALES:
            short = _load(locale)["metadata"]["short_desc"]
            numbers = {int(n) for n in re.findall(r"\d+", short)}
            assert count in numbers, f"{locale} 简介里的指令条数与 catalog 的 {count} 不符：{short}"
