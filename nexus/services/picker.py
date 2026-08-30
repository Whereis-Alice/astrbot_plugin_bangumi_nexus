"""选源会话：让用户回复序号挑一个字幕组，而不是一次订下全部。

为什么要有这一层：Mikan 的关键词搜索源会把所有字幕组、所有语言、所有画质的
发布一起收下，一集番能推七八条。上游插件全都这么做，于是「订阅」几乎等于「刷屏」。
正确姿势是先把候选列出来，让用户挑一个固定源。

这一步必须是有状态的（列表发出去 → 等用户回一个数字 → 才真正落库），
所以这里放一个只存在内存里的会话表：

- **不落库**：选源意图活不过一次对话，重启后残留的会话只会让人困惑；
- **带过期**：「PICK_SESSION_SECONDS」 之后自动失效，免得跟下一次选源串台；
- **一个会话只留一个**：同一个群里重新发起选源会顶掉上一次，符合直觉。

「message_ids」 记的是列表消息本身的 id，选完之后交给 main.py 撤回 ——
选源列表是一次性的中间过程，留在聊天记录里只是噪音。

Copyright (C) 2026 Whereis-Alice and AstrBot Plugin Authors.
Licensed under the GNU Affero General Public License v3.0 or later.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field

from ..constants import PICK_SESSION_SECONDS

#: 打在 「Reply.notes」 上的标记：main.py 见到它就走「发送并记录消息 id」这条路，
#: 而不是普通的 「chain_result」 —— 因为普通发送拿不到消息 id，也就没法撤回。
PICK_NOTE = "pick"


@dataclass(frozen=True)
class PickOption:
    """候选列表里的一项。「index」 是展示给用户的序号，从 1 开始。"""

    index: int
    label: str
    url: str
    detail: str = ""
    group_id: int = 0
    tags: tuple[str, ...] = ()


@dataclass
class PickSession:
    """一次「等用户回数字」的会话。"""

    umo: str
    kind: str
    name: str
    options: tuple[PickOption, ...]
    subject_id: int = 0
    cover: str = ""
    created_at: float = field(default_factory=time.time)
    message_ids: list[str] = field(default_factory=list)

    @property
    def expired(self) -> bool:
        return time.time() - self.created_at > PICK_SESSION_SECONDS

    def option(self, index: int) -> PickOption | None:
        for item in self.options:
            if item.index == index:
                return item
        return None


class PickRegistry:
    """按会话保存待选列表。纯内存、无锁 —— 事件处理本身是单线程协程。"""

    def __init__(self) -> None:
        self._sessions: dict[str, PickSession] = {}

    def open(
        self,
        umo: str,
        *,
        kind: str,
        name: str,
        options: Sequence[PickOption],
        subject_id: int = 0,
        cover: str = "",
    ) -> PickSession:
        """开一个新会话，顶掉该会话此前未完成的选择。"""

        session = PickSession(
            umo=umo,
            kind=kind,
            name=name,
            options=tuple(options),
            subject_id=subject_id,
            cover=cover,
        )
        self._sessions[umo] = session
        self._sweep()
        return session

    def get(self, umo: str) -> PickSession | None:
        """取当前会话；已过期的顺手清掉。"""

        session = self._sessions.get(umo)
        if session is None:
            return None
        if session.expired:
            self._sessions.pop(umo, None)
            return None
        return session

    def note_message(self, umo: str, message_id: str) -> None:
        """记下列表消息的 id，供选完之后撤回。"""

        session = self._sessions.get(umo)
        if session is not None and message_id:
            session.message_ids.append(str(message_id))

    def resolve(self, umo: str, text: str) -> tuple[PickSession, PickOption] | None:
        """把一条普通消息解释成「选了第几个」。

        只认「整条消息就是一个范围内的数字」这一种写法。放宽到「消息里含数字」
        会把群里正常聊天（「第 3 集好看」）也吞掉，这是上游同类插件的常见翻车点。
        """
        session = self.get(umo)
        if session is None:
            return None
        token = str(text or "").strip().strip(".。、")
        if not token.isdigit():
            return None
        option = session.option(int(token))
        return (session, option) if option is not None else None

    def drop(self, umo: str) -> PickSession | None:
        """结束会话并返回它，便于调用方拿 「message_ids」 去撤回。"""

        return self._sessions.pop(umo, None)

    def stats(self) -> dict[str, int]:
        self._sweep()
        return {"pending": len(self._sessions)}

    def _sweep(self) -> None:
        """顺手清掉过期会话，避免长期运行时字典只增不减。"""

        stale = [umo for umo, session in self._sessions.items() if session.expired]
        for umo in stale:
            self._sessions.pop(umo, None)
