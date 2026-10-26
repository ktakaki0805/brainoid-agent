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

from brainoid_worker.note_erasure import note_targets


class FactDependency(CoreValue):
    memory_id: ResourceId
    version: Version
    deletion_epoch: Annotated[int, Field(strict=True, ge=0)]


class NoteDependency(CoreValue):
    note_id: ResourceId
    version: Version


class TaskDependency(CoreValue):
    task_id: ResourceId
    version: Version


class ReminderDependency(CoreValue):
    reminder_id: ResourceId
    version: Version


class ErasureDependencies(CoreValue):
    schema_version: Literal[1] = 1
    tenant_id: ResourceId
    run_id: ResourceId
    # False for old runs whose already-cleared direct inputs cannot be recovered.
    complete: bool
    facts: tuple[FactDependency, ...]
    notes: tuple[NoteDependency, ...] = Field(default=(), exclude_if=lambda value: not value)

    tasks: tuple[TaskDependency, ...] = Field(default=(), exclude_if=lambda value: not value)

    reminders: tuple[ReminderDependency, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )

    def matches(self, intent: DeletionIntent) -> bool:
        if self.tenant_id != intent.tenant_id:
            return False
        if any(
            n.note_id == target.resource_id and n.version <= target.version
            for n in self.notes
            for target in note_targets(intent)
        ):
            return True
        if intent.kind == "delete_resource" and any(
            task.task_id == target.resource_id and task.version <= target.version
            for task in self.tasks
            for target in intent.targets
            if target.kind == "task"
        ):
            return True
        target = intent.targets[0]
        if intent.kind == "delete_resource":
            return False
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


def _receipt_result(saved: Document, run: Document, identity: str) -> ToolResult | None:
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
        return None
    result = ToolResult.model_validate(saved["result"])
    if result.tool_call_id != identity or result.operation_id != saved["effect_id"]:
        raise ValueError("Invalid erasure result binding")
    return result


def receipt_records(saved: Document, run: Document, identity: str) -> tuple[MemoryRecord, ...]:
    """Only exact canonical memory tool results; never model response JSON/text."""
    if saved.get("name") not in {"memory.search", "memory.remember", "memory.revise"}:
        return ()
    result = _receipt_result(saved, run, identity)
    return () if result is None else result_records(result)


def note_records(result: ToolResult, tenant: str) -> tuple[NoteDependency, ...]:
    raw = result.result.get("notes", [])
    if not isinstance(raw, list):
        raise ValueError("Invalid note inventory")
    notes = []
    for note in raw:
        if not isinstance(note, dict) or note.get("tenant_id") != tenant:
            raise ValueError("Invalid erasure note tenant")
        notes.append(
            NoteDependency.model_validate({"note_id": note["id"], "version": note["version"]})
        )
    return tuple(notes)


def receipt_notes(saved: Document, run: Document, identity: str) -> tuple[NoteDependency, ...]:
    if saved.get("name") not in {"notes.create", "notes.list", "notes.update"}:
        return ()
    result = _receipt_result(saved, run, identity)
    return () if result is None else note_records(result, run["tenant_id"])


def task_records(result: ToolResult, tenant: str) -> tuple[TaskDependency, ...]:
    raw = result.result.get("tasks", [])
    if not isinstance(raw, list):
        raise ValueError("Invalid task inventory")
    tasks = []
    for task in raw:
        if not isinstance(task, dict) or task.get("tenant_id") != tenant:
            raise ValueError("Invalid erasure task tenant")
        tasks.append(
            TaskDependency.model_validate({"task_id": task["id"], "version": task["version"]})
        )
    return tuple(tasks)


def receipt_tasks(saved: Document, run: Document, identity: str) -> tuple[TaskDependency, ...]:
    if saved.get("name") not in {"tasks.create", "tasks.list", "tasks.update"}:
        return ()
    result = _receipt_result(saved, run, identity)
    return () if result is None else task_records(result, run["tenant_id"])


def reminder_records(result: ToolResult, tenant: str) -> tuple[ReminderDependency, ...]:
    raw = result.result.get("reminders", [])
    if not isinstance(raw, list):
        raise ValueError("Invalid reminder inventory")
    reminders = []
    for reminder in raw:
        if not isinstance(reminder, dict) or reminder.get("tenant_id") != tenant:
            raise ValueError("Invalid erasure reminder tenant")
        reminders.append(
            ReminderDependency.model_validate(
                {"reminder_id": reminder["id"], "version": reminder["version"]}
            )
        )
    return tuple(reminders)


def receipt_reminders(
    saved: Document, run: Document, identity: str
) -> tuple[ReminderDependency, ...]:
    if saved.get("name") not in {
        "reminders.create",
        "reminders.list",
        "reminders.update",
        "reminders.snooze",
    }:
        return ()
    result = _receipt_result(saved, run, identity)
    return () if result is None else reminder_records(result, run["tenant_id"])


def capture(
    run: Document,
    *,
    complete: bool = False,
    records: Iterable[MemoryRecord] = (),
    note_dependencies: Iterable[NoteDependency] = (),
    task_dependencies: Iterable[TaskDependency] = (),
    reminder_dependencies: Iterable[ReminderDependency] = (),
) -> ErasureDependencies:
    previous = retained(run)
    known = set(previous.facts if previous is not None else ())
    notes = set(previous.notes if previous is not None else ())
    notes.update(note_dependencies)
    tasks = set(previous.tasks if previous is not None else ())
    tasks.update(task_dependencies)
    reminders = set(previous.reminders if previous is not None else ())
    reminders.update(reminder_dependencies)
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
        if binding.request.call.name in {
            "reminders.create",
            "reminders.list",
            "reminders.update",
            "reminders.snooze",
        }:
            reminders.update(reminder_records(binding.result, run["tenant_id"]))
        if binding.request.call.name in {"tasks.create", "tasks.list", "tasks.update"}:
            tasks.update(task_records(binding.result, run["tenant_id"]))
        if binding.request.call.name in {"notes.create", "notes.list", "notes.update"}:
            notes.update(note_records(binding.result, run["tenant_id"]))
        if binding.request.call.name in {"memory.search", "memory.remember", "memory.revise"}:
            values.extend(result_records(binding.result))
    for record in values:
        if record.tenant_id != run["tenant_id"]:
            raise ValueError("Invalid erasure memory tenant")
        for source in record.content.source_refs:
            if source.kind == "note":
                notes.add(NoteDependency(note_id=source.resource_id, version=source.version))
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
        notes=tuple(sorted(notes, key=lambda n: (n.note_id, n.version))),
        tasks=tuple(sorted(tasks, key=lambda n: (n.task_id, n.version))),
        reminders=tuple(sorted(reminders, key=lambda n: (n.reminder_id, n.version))),
    )
