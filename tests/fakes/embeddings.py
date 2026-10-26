"""Synthetic deterministic embeddings with explicit external-call hooks."""

from collections.abc import Callable
from typing import Literal

from brainoid_memory.vector_store import EmbeddingConfig

CONFIG = EmbeddingConfig(model="synthetic-embedding", revision="fixture-v1", dimensions=3)


class FakeEmbeddings:
    def __init__(self, config: EmbeddingConfig = CONFIG) -> None:
        self.config = config
        self.calls: list[tuple[str, str]] = []
        self.hook: Callable[[], None] = lambda: None

    def embed(self, text: str, *, task: Literal["document", "query"]) -> tuple[float, ...]:
        self.calls.append((task, text))
        self.hook()
        return tuple(1.0 for _ in range(self.config.dimensions))
