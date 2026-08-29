"""进程内活动日志：一个有界环形缓冲。

WebUI 的「概览」页需要知道「插件刚才干了什么」，但把这些写进 AstrBot 主日志会
把日志刷爆，写进数据库又太重。折中方案是一个固定容量的内存环，重启即丢 ——
它只是运维观察窗，不是审计记录。

Copyright (C) 2026 Whereis-Alice and AstrBot Plugin Authors.
Licensed under the GNU Affero General Public License v3.0 or later.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from .constants import MAX_ACTIVITY_ENTRIES

LEVELS = ("debug", "info", "warn", "error")


@dataclass(frozen=True)
class Entry:
    """一条活动记录。"""

    at: float
    level: str
    scope: str
    message: str

    def payload(self) -> dict[str, Any]:
        return {
            "at": self.at,
            "time": time.strftime("%m-%d %H:%M:%S", time.localtime(self.at)),
            "level": self.level,
            "scope": self.scope,
            "message": self.message,
        }


class ActivityLog:
    """线程安全性不做保证 —— 只在事件循环里用，够了。"""

    def __init__(self, capacity: int = MAX_ACTIVITY_ENTRIES) -> None:
        self._entries: deque[Entry] = deque(maxlen=max(16, capacity))
        self._counters: dict[str, int] = {}

    def add(self, scope: str, message: str, *, level: str = "info") -> Entry:
        entry = Entry(
            at=time.time(),
            level=level if level in LEVELS else "info",
            scope=scope,
            message=str(message)[:400],
        )
        self._entries.append(entry)
        self._counters[entry.level] = self._counters.get(entry.level, 0) + 1
        return entry

    def info(self, scope: str, message: str) -> Entry:
        return self.add(scope, message, level="info")

    def warn(self, scope: str, message: str) -> Entry:
        return self.add(scope, message, level="warn")

    def error(self, scope: str, message: str) -> Entry:
        return self.add(scope, message, level="error")

    def recent(self, limit: int = 80, *, level: str | None = None) -> list[dict[str, Any]]:
        items = list(self._entries)
        if level in LEVELS:
            wanted = LEVELS.index(level)
            items = [item for item in items if LEVELS.index(item.level) >= wanted]
        return [item.payload() for item in reversed(items[-max(1, limit) :])]

    def counters(self) -> dict[str, int]:
        return dict(self._counters)

    def clear(self) -> None:
        self._entries.clear()
        self._counters.clear()

    def __len__(self) -> int:
        return len(self._entries)
