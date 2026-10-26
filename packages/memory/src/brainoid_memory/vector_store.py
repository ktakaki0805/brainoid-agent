"""Explicit embedding generations and scope-first, two-phase vector retrieval.

No SDK/provider dependency. Providers only receive the query or a current canonical
projection; vector hits are untrusted identifiers until retrieve rechecks them.
"""

import hashlib
import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Literal, Protocol

from brainoid_contracts import FirebaseUid, ResourceId, Scope
from brainoid_contracts.transactions import Transaction, TransactionStore
from brainoid_policy.scopes import PolicyDenied
from pydantic import Field

from brainoid_memory.models import Fact, MemoryValue
from brainoid_memory.repository import FactTransaction
from brainoid_memory.retrieval import Candidate, SearchInput


class VectorConfigurationError(ValueError):
    def __init__(self) -> None:
        super().__init__("memory_vector_configuration_mismatch")


class VectorUnavailable(Exception):
    def __init__(self) -> None:
        super().__init__("memory_vector_unavailable")


class EmbeddingConfig(MemoryValue):
    model: Annotated[str, Field(pattern=r"^[a-z][a-z0-9.-]{0,126}$")]
    revision: Annotated[str, Field(pattern=r"^[a-zA-Z0-9_.-]{1,64}$")]
    dimensions: Annotated[int, Field(strict=True, ge=1, le=2048)]
    format_version: Literal[1] = 1

    @property
    def key(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()[:32]

    @property
    def collection(self) -> str:
        return "memory_vectors_" + self.key

    def vector(self, values: tuple[float, ...]) -> tuple[float, ...]:
        if len(values) != self.dimensions or any(
            type(value) not in (int, float) or not math.isfinite(value) for value in values
        ):
            raise VectorConfigurationError()
        # No truncation, padding, projection, or normalization of returned vectors.
        return values


def scope_key(tenant: ResourceId, scope: Scope, owner: FirebaseUid | None) -> str:
    if (scope.visibility == "private") != (owner is not None):
        raise PolicyDenied()
    encoded = json.dumps([tenant, scope.visibility, scope.display_policy, owner])
    return hashlib.sha256(encoded.encode()).hexdigest()


def fact_text(fact: Fact) -> str:
    """Content projection: no tenant, owner, creator, source body or credentials."""
    return json.dumps(
        fact.model_dump(
            mode="json",
            include={"subject_entity_id", "predicate", "object_entity_id", "value", "qualifiers"},
            exclude_none=True,
        ),
        ensure_ascii=False,
        sort_keys=True,
    )


class Embeddings(Protocol):
    config: EmbeddingConfig

    def embed(self, text: str, *, task: Literal["document", "query"]) -> tuple[float, ...]:
        """Single bounded attempt outside transactions; never silently retry billing."""
        ...


class VectorIndex(Protocol):
    config: EmbeddingConfig

    def nearest(
        self, tenant: ResourceId, partition: str, vector: tuple[float, ...], *, limit: int
    ) -> tuple[Candidate, ...]:
        """Tenant collection + exact server scope key BEFORE nearest selection."""
        ...

    def save(self, tx: Transaction, fact: Fact, vector: tuple[float, ...]) -> None:
        """Write only through the canonical commit transaction."""
        ...

    def current(self, tx: Transaction, fact: Fact) -> bool:
        """Verify a completed checkpoint still has a correctly bound stored vector."""
        ...


@dataclass(frozen=True)
class PreparedCandidates:
    query: str
    scopes: tuple[Scope, ...]
    results: tuple[tuple[Candidate, ...], ...]

    def candidates(
        self, query: str, scope: Scope, facts: tuple[Fact, ...]
    ) -> tuple[Candidate, ...]:
        if query != self.query or scope not in self.scopes:
            raise PolicyDenied()
        return self.results[self.scopes.index(scope)]


class VectorSearch:
    def __init__(self, embeddings: Embeddings, index: VectorIndex) -> None:
        if embeddings.config != index.config:
            raise VectorConfigurationError()
        self.embeddings, self.index = embeddings, index

    def prepare(
        self,
        store: TransactionStore,
        opened: Callable[[Transaction], FactTransaction],
        request: SearchInput,
        *,
        scopes: tuple[Scope, ...] | None = None,
    ) -> PreparedCandidates:
        request = SearchInput.model_validate(request.model_dump())

        def authorize(tx: Transaction) -> tuple[FactTransaction, tuple[Scope, ...]]:
            access = opened(tx)
            available = access.scopes()
            selected = available if scopes is None else tuple(dict.fromkeys(scopes))
            if any(scope not in available for scope in selected):
                raise PolicyDenied()
            access.finish()
            return access, selected

        access, selected = store.run(authorize)
        access.finish()
        vector = self.index.config.vector(self.embeddings.embed(request.query, task="query"))
        results = []
        for scope in selected:
            access.finish()
            owner = access.principal.user_id if scope.visibility == "private" else None
            results.append(
                self.index.nearest(
                    access.principal.context.tenant_id,
                    scope_key(access.principal.context.tenant_id, scope, owner),
                    vector,
                    limit=request.limit,
                )
            )
        access.finish()
        return PreparedCandidates(request.query, selected, tuple(results))
