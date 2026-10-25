"""Create-only object fake with deterministic crash hooks, shared by emulator tests."""

from collections.abc import Callable
from threading import Lock


class MemoryObjects:
    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}
        self.lock = Lock()
        self.before: Callable[[str], None] = lambda name: None
        self.after: Callable[[str], None] = lambda name: None
        self.writes = 0

    def read(self, name: str) -> bytes | None:
        with self.lock:
            return self.data.get(name)

    def names(self, prefix: str) -> list[str]:
        with self.lock:
            return sorted(name for name in self.data if name.startswith(prefix))

    def create(self, name: str, data: bytes) -> bool:
        self.before(name)
        with self.lock:
            if name in self.data:
                return False
            self.data[name] = data
            self.writes += 1
        self.after(name)
        return True
