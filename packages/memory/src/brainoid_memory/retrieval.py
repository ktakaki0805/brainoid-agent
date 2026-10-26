"""Scope-first, current canonical retrieval with a bounded two-hop graph walk."""

from collections import deque
from dataclasses import dataclass
from typing import Annotated, Protocol

from brainoid_contracts import ResourceId, Scope, Version
from brainoid_policy.scopes import PolicyDenied
from pydantic import Field

from brainoid_memory.models import Entity, Epoch, Fact, MemoryValue
from brainoid_memory.repository import EntityTransaction, FactTransaction


class SearchInput(MemoryValue):
    query: Annotated[str, Field(strict=True, min_length=1, max_length=2000)]
    limit: Annotated[int, Field(strict=True, ge=1, le=40)] = 10


class Candidate(MemoryValue):
    id: ResourceId
    version: Version
    deletion_epoch: Epoch


class SearchTrace(MemoryValue):
    """Counts of authorized explored data only; no query, labels, IDs or rejected counts."""

    facts: int = 0
    entities: int = 0
    hops: int = 0
    fact_limit_reached: bool = False
    entity_limit_reached: bool = False
    hop_limit_reached: bool = False
    ambiguous_entities: bool = False


class SemanticCandidates(Protocol):
    """DB-only provider boundary; inputs are separately authorized for ONE exact scope.

    A future vector adapter may return stale IDs, never authoritative fact bodies.
    No external provider call is allowed in a retried transaction. The caller still
    checks candidate identity, version, epoch and current canonical dependencies.
    """

    def candidates(
        self, query: str, scope: Scope, facts: tuple[Fact, ...]
    ) -> tuple[Candidate, ...]: ...


class LexicalCandidates:
    """Deterministic local fake for the semantic boundary; no embedding/network I/O."""

    def candidates(
        self, query: str, scope: Scope, facts: tuple[Fact, ...]
    ) -> tuple[Candidate, ...]:
        words = query.casefold().split()
        return tuple(
            Candidate(id=f.id, version=f.version, deletion_epoch=f.deletion_epoch)
            for f in facts
            if any(word in f.model_dump_json().casefold() for word in words)
        )


@dataclass(frozen=True)
class Retrieved:
    records: tuple[Fact, ...]
    trace: SearchTrace


def retrieve(
    facts: FactTransaction,
    entities: EntityTransaction,
    request: SearchInput,
    *,
    scopes: tuple[Scope, ...] | None = None,
    provider: SemanticCandidates | None = None,
) -> Retrieved:
    request = SearchInput.model_validate(request.model_dump())
    available = facts.scopes()
    scopes = available if scopes is None else tuple(dict.fromkeys(scopes))
    if any(scope not in available for scope in scopes):
        raise PolicyDenied()
    selected: dict[ResourceId, Fact] = {}
    visited_entities: set[ResourceId] = set()
    trace = SearchTrace()
    candidates_provider = provider or LexicalCandidates()
    fact_limit = min(40, request.limit)
    for scope in scopes:
        if len(selected) >= fact_limit:
            # Remaining authorized partitions are unsearched, not known-empty.
            trace = trace.model_copy(update={"fact_limit_reached": True})
            break
        # Owner/display predicates are sent to storage BEFORE inspecting any content.
        canonical = {f.id: f for f in facts.candidates(scope=scope, for_model=True)}
        entity_records = {
            e.id: e
            for e in entities.candidates(scope=scope)
            if not e.deleted and not e.suppress_reuse and e.deletion_epoch == 0
        }
        if scope.visibility == "private":
            for shared in available:
                if shared.visibility == "shared":
                    entity_records.update(
                        {
                            e.id: e
                            for e in entities.candidates(scope=shared)
                            if not e.deleted and not e.suppress_reuse and e.deletion_epoch == 0
                        }
                    )
        by_name: dict[str, list[Entity]] = {}
        for e in entity_records.values():
            for name in set((e.label, *e.aliases)):
                if name.casefold() in request.query.casefold():
                    by_name.setdefault(name.casefold(), []).append(e)
        # Opaque provider IDs are compared exactly, after entity authorization.
        # The same spelling in different integrations remains multiple candidates.
        by_external: dict[str, set[ResourceId]] = {}
        for entity in entity_records.values():
            for reference in entity.external_refs:
                if reference.external_id == request.query.strip():
                    by_external.setdefault(reference.external_id, set()).add(entity.id)
        ambiguous = any(len(group) > 1 for group in by_name.values()) or any(
            len(group) > 1 for group in by_external.values()
        )
        trace = trace.model_copy(
            update={"ambiguous_entities": trace.ambiguous_entities or ambiguous}
        )
        seeds = {entity.id for group in by_name.values() for entity in group}
        seeds.update(identity for group in by_external.values() for identity in group)
        # Returned IDs are intersected with exact-scope, current canonical records.
        semantic = candidates_provider.candidates(request.query, scope, tuple(canonical.values()))
        for candidate in semantic:
            fact = canonical.get(candidate.id)
            if fact is None or (fact.version, fact.deletion_epoch) != (
                candidate.version,
                candidate.deletion_epoch,
            ):
                continue
            seeds.add(fact.subject_entity_id)
        queue: deque[tuple[ResourceId, int]] = deque((identity, 0) for identity in sorted(seeds))
        local_seen: set[ResourceId] = set()
        while queue:
            if len(selected) >= fact_limit:
                trace = trace.model_copy(update={"fact_limit_reached": True})
                break
            identity, depth = queue.popleft()
            if identity in local_seen:
                continue
            if identity not in entity_records:
                # Shared entity references in a private fact remain valid, but never
                # cause traversal to cross from this private scope into another scope.
                entity = entities.get(identity, for_model=True)
                entity_records[identity] = entity
            if identity not in visited_entities and len(visited_entities) >= 20:
                trace = trace.model_copy(update={"entity_limit_reached": True})
                break
            visited_entities.add(identity)
            local_seen.add(identity)
            adjacent = sorted(
                (
                    f
                    for f in canonical.values()
                    if identity in (f.subject_entity_id, f.object_entity_id)
                ),
                key=lambda f: f.id,
            )
            if depth >= 2:
                if any(f.id not in selected for f in adjacent):
                    trace = trace.model_copy(update={"hop_limit_reached": True})
                continue
            for fact in adjacent:
                if fact.id in selected:
                    continue
                if len(selected) >= fact_limit:
                    trace = trace.model_copy(update={"fact_limit_reached": True})
                    break
                endpoints = {fact.subject_entity_id}
                if fact.object_entity_id is not None:
                    endpoints.add(fact.object_entity_id)
                if len(visited_entities | endpoints) > 20:
                    trace = trace.model_copy(update={"entity_limit_reached": True})
                    continue
                visited_entities.update(endpoints)
                selected[fact.id] = fact
                trace = trace.model_copy(update={"hops": max(trace.hops, depth + 1)})
                queue.extend(
                    (other, depth + 1) for other in sorted(endpoints) if other not in local_seen
                )
    records = tuple(selected.values())
    facts.finish_records(records)
    return Retrieved(
        records, trace.model_copy(update={"facts": len(records), "entities": len(visited_entities)})
    )
