"""Content-free erasure evidence; never an authorization or model reuse snapshot."""

from collections.abc import Iterable
from typing import Annotated, Literal
from uuid import UUID, uuid5

from brainoid_agent_core.types import MemoryRecord, ToolResult
from brainoid_agent_core.types.common import CoreValue
from brainoid_contracts import ResourceId, Version
from brainoid_contracts.transactions import Document
from brainoid_memory.deletion_ledger import DeletionIntent
from brainoid_tools import Authority
from brainoid_tools.reuse import ResultBinding
from pydantic import Field, ValidationError


class FactDependency(CoreValue):
    memory_id: ResourceId
    version: Version
    deletion_epoch: Annotated[int, Field(strict=True, ge=0)]


class ErasureDependencies(CoreValue):
    schema_version: Literal[1] = 1
    tenant_id: ResourceId
    run_id: ResourceId
    # False for old runs whose already-cleared direct inputs cannot be recovered.
    complete: bool
    facts: tuple[FactDependency, ...]

    def matches(self, intent: DeletionIntent) -> bool:
        target = intent.targets[0]
        return self.tenant_id == intent.tenant_id and any(
            fact.memory_id == target.resource_id
            and fact.version <= target.version
            and fact.deletion_epoch < target.deletion_epoch
            for fact in self.facts
        )


def retained(run: Document) -> ErasureDependencies | None:
    if "purge_dependencies" not in run:
        return None
    saved = ErasureDependencies.model_validate(run["purge_dependencies"])
    if saved.tenant_id != run.get("tenant_id") or saved.run_id != run.get("run_id"):
        raise ValueError("Invalid erasure dependency binding")
    return saved


def result_records(result: ToolResult) -> tuple[MemoryRecord, ...]:
    values = result.result.get("memories", [])
    if not isinstance(values, list):
        raise ValueError("Invalid memory result inventory")
    return tuple(MemoryRecord.model_validate(r) for r in values)


def receipt_records(saved: Document, run: Document, identity: str) -> tuple[MemoryRecord, ...]:
    """Only exact canonical memory tool results; never model response JSON/text."""
    if saved.get("name") not in {"memory.search", "memory.remember", "memory.revise"}:
        return ()
    authority = Authority.model_validate(saved["authority"])
    if (
        authority.context.tenant_id != run.get("tenant_id")
        or authority.run_id != run.get("run_id")
        or saved.get("tool_call_id") != identity
        or identity not in run.get("tool_call_ids", [])
        or saved.get("effect_id") != str(uuid5(UUID(identity), "brainoid-tool-effect"))
    ):
        raise ValueError("Invalid erasure receipt binding")
    if saved.get("result") is None:
        return ()
    result = ToolResult.model_validate(saved["result"])
    if result.tool_call_id != identity or result.operation_id != saved["effect_id"]:
        raise ValueError("Invalid erasure result binding")
    return result_records(result)


def capture(
    run: Document, *, complete: bool = False, records: Iterable[MemoryRecord] = ()
) -> ErasureDependencies:
    previous = retained(run)
    known = set(previous.facts if previous is not None else ())
    values = list(records)
    for raw in run.get("memory_records", []):
        try:
            values.append(MemoryRecord.model_validate(raw))
        except ValidationError:
            if complete:
                raise
            # Incomplete pre-schema records are not invented into valid evidence.
    for raw in run.get("tool_bindings", []):
        binding = ResultBinding.model_validate(raw)
        if (
            binding.request.context.tenant_id != run["tenant_id"]
            or binding.request.tool_call_id not in run.get("tool_call_ids", [])
            or binding.result.tool_call_id != binding.request.tool_call_id
        ):
            raise ValueError("Invalid erasure tool binding")
        if binding.request.call.name in {"memory.search", "memory.remember", "memory.revise"}:
            values.extend(result_records(binding.result))
    for record in values:
        if record.tenant_id != run["tenant_id"]:
            raise ValueError("Invalid erasure memory tenant")
        known.add(
            FactDependency(
                memory_id=record.memory_id,
                version=record.version,
                deletion_epoch=record.deletion_epoch,
            )
        )
    return ErasureDependencies(
        tenant_id=run["tenant_id"],
        run_id=run["run_id"],
        complete=complete or (previous is not None and previous.complete),
        facts=tuple(sorted(known, key=lambda f: (f.memory_id, f.version, f.deletion_epoch))),
    )
