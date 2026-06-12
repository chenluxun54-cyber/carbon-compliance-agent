"""
Tier 1 — TransientMemory: in-process sliding window.

Lives entirely in RAM; never written to disk. Fed directly to the LLM on
every generation call. Oldest messages are evicted when the window is full.
"""

MAX_WINDOW = 20  # max messages kept per session


class TransientMemory:
    def __init__(self, max_window: int = MAX_WINDOW):
        self._max = max_window
        self._windows: dict[str, list] = {}  # session_id → message list

    def add(self, session_id: str, message: dict) -> None:
        """Append a message, evicting the oldest if over capacity."""
        buf = self._windows.setdefault(session_id, [])
        buf.append(message)
        if len(buf) > self._max:
            del buf[0]

    def get(self, session_id: str) -> list:
        """Return current sliding window for this session (may be empty)."""
        return list(self._windows.get(session_id, []))

    def set(self, session_id: str, messages: list) -> None:
        """Replace window wholesale (used when restoring from Redis)."""
        self._windows[session_id] = list(messages[-self._max:])

    def clear(self, session_id: str) -> None:
        self._windows.pop(session_id, None)

    def all_sessions(self) -> list[str]:
        return list(self._windows.keys())
