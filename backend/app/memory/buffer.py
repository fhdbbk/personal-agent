from collections import deque
from dataclasses import dataclass
from typing import Literal

Role = Literal["user", "assistant"]


@dataclass(frozen=True)
class Message:
    """One chat turn. Frozen so a stored message can't be mutated out 
    from under the buffer. Also makes it hashable, which is nice to have 
    down the line if we want to use messages as dict keys or set members."""

    role: Role
    content: str


class ConversationBuffer:
    """In-process ring buffer of recent turns, keyed by conversation id.

    No persistence — Phase 3 introduces SQLite-backed long-term memory.
    """

    def __init__(self, maxlen: int = 32) -> None:
        self._maxlen = maxlen
        self._convos: dict[str, deque[Message]] = {}

    def append(self, conversation_id: str, message: Message) -> None:
        # setdefault: return the existing deque, or insert a fresh one on first sight.
        d = self._convos.setdefault(conversation_id, deque(maxlen=self._maxlen))
        d.append(message)

    def history(self, conversation_id: str) -> list[Message]:
        return list(self._convos.get(conversation_id, ()))

    def clear(self, conversation_id: str) -> None:
        self._convos.pop(conversation_id, None)


buffer = ConversationBuffer()
