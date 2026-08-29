"""pytest 公共装置。

单测直接 import 插件包，因此需要把插件根目录塞进「sys.path」，
否则 pytest 从仓库根启动时找不到「nexus」顶层包。
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_PACKAGE = PLUGIN_ROOT.name

# 两条路径都要加：
# 「插件根」让 「from nexus import ...」 这种子包直连生效；
# 「插件根的上一级」让插件目录本身能当包导入，这是 「main.py」 唯一可行的导入姿势。
for _entry in (str(PLUGIN_ROOT), str(PLUGIN_ROOT.parent)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

FIXTURES = PLUGIN_ROOT / "tests" / "fixtures"


def plugin_module(name: str) -> ModuleType:
    """以「插件包.子模块」的形式导入插件模块。

    「main.py」 里写的是相对导入（from .nexus import ...），
    直接 「import main」 会抛 「attempted relative import with no known parent package」，
    必须按 AstrBot 真实的加载方式（插件目录即包）导入才能复现线上行为。
    """

    return importlib.import_module(f"{PLUGIN_PACKAGE}.{name}")


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    """离线 HTML / JSON 样本目录，避免单测联网。"""

    return FIXTURES


@pytest.fixture(scope="session")
def read_fixture(fixtures_dir: Path):
    """按文件名读取 fixture 文本，统一 UTF-8，避免 Windows 默认 GBK 解码失败。"""

    def _read(name: str) -> str:
        return (fixtures_dir / name).read_text(encoding="utf-8")

    return _read
