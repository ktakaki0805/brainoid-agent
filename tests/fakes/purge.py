"""Synthetic bounded-page adapter; production uses server-side paging RPCs."""

import copy

from brainoid_contracts.transactions import Document
from brainoid_memory.deletion_ledger import DeletionIntent

from fakes.memory_repository import GraphStore


class FakePurgePages:
    def __init__(self, store: GraphStore) -> None:
        self.store = store
        self.calls: list[tuple[str, bool, str, int]] = []

    def children(
        self, parent: str, *, collections: bool, token: str = "", limit: int = 50
    ) -> tuple[list[str], str]:
        self.calls.append((parent, collections, token, limit))
        paths = sorted(
            {
                parent + "/" + path[len(parent) + 1 :].split("/")[0]
                for path in self.store.data
                if path.startswith(parent + "/")
            }
        )
        chosen = [p for p in paths if p > token][:limit]
        return chosen, chosen[-1] if len(chosen) == limit else ""

    def page(
        self, collection: str, *, after: str | None = None, limit: int = 100
    ) -> list[tuple[str, Document]]:
        return [
            (p, copy.deepcopy(d))
            for p, d in sorted(self.store.data.items())
            if p.rsplit("/", 1)[0] == collection and (after is None or p.rsplit("/", 1)[1] > after)
        ][:limit]


class FakeInvalidation:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.applied: set[str] = set()
        self.fail = False
        self.lose_response = False

    def invalidate(self, intent: DeletionIntent, *, key: str) -> str:
        self.calls.append(key)
        if self.fail:
            raise RuntimeError("Synthetic external erasure outage")
        self.applied.add(key)
        if self.lose_response:
            self.lose_response = False
            raise RuntimeError("Synthetic lost external response")
        return "synthetic-receipt-v1"
