"""Collection-specific, schema-bound erasure decisions. No substring matching.

Raw shared contributions are never selected by a derived reference. The common
postorder walker can erase an explicitly selected subtree even after its parent
has gone; admission of conversation/account/tenant deletion is a separate concern.
"""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from brainoid_agent_core.types import MemoryRecord
from brainoid_contracts import UtcTimestamp
from brainoid_contracts.transactions import Document, Transaction
from brainoid_jobs import Job
from brainoid_memory.deletion_ledger import DeletionIntent, DeletionTarget
from brainoid_memory.forget import operation_path, target_path
from pydantic import TypeAdapter

from brainoid_worker.memory_erasure import (
    ErasureDependencies,
    NoteDependency,
    TaskDependency,
    capture,
    receipt_notes,
    receipt_records,
    receipt_tasks,
    retained,
)
from brainoid_worker.note_erasure import (
    fact_markers,
    fact_selected,
    note_copy,
    note_targets,
    sourced,
    task_copy,
)

ROOTS = frozenset(
    {"conversations", "runs", "jobs", "tool_executions", "memory_effects", "run_proposals"}
)
VECTOR = re.compile(r"memory_vectors_[0-9a-f]{32}\Z")


@dataclass(frozen=True)
class Decision:
    erase: bool = False
    replacement: Document | None = None
    children: bool = False
    history: Document | None = None
    dependency_run: Document | None = None
    selected: bool = False
    deletion_markers: tuple[tuple[str, Document], ...] = ()


class LegacyProvenanceMissing(Exception):
    """Old self-forget cleared inputs; only its own accepted purge can resolve it."""


class CollectionHandler(Protocol):
    def select(self, document: Document, intent: DeletionIntent) -> bool: ...


class ReferenceCopies:
    """Opt-in contract for future notification preview/cache/export DB records.

    A producer must save tenant_id and canonical purge_sources (kind/resource_id/
    parent_id), fence creation on those sources, and keep originals elsewhere.
    This is an adapter contract, not a notification/export service implementation.
    """

    def select(self, document: Document, intent: DeletionIntent) -> bool:
        if document.get("tenant_id") != intent.tenant_id:
            raise ValueError("Invalid copy tenant")
        references = document.get("purge_sources")
        if not isinstance(references, list):
            raise ValueError("Missing copy provenance")
        return any(
            isinstance(ref, dict)
            and (ref.get("kind"), ref.get("resource_id"), ref.get("parent_id"))
            == (target.kind, target.resource_id, target.parent_id)
            for ref in references
            for target in intent.targets
        )


class ForgetHandlers:
    def __init__(self, registered: Mapping[str, CollectionHandler] | None = None) -> None:
        self.registered = dict(registered or {})
        if not set(self.registered) <= {"notifications", "caches", "exports"}:
            raise ValueError("Only derived copy collections can be registered")
        self.verified_operation: Callable[[Transaction, str], DeletionIntent | None] = (
            lambda tx, identity: None
        )

    def root(self, name: str, intent: DeletionIntent | None = None) -> bool:
        return (
            name in ROOTS
            or (
                intent is not None
                and (
                    (
                        intent.kind == "delete_resource"
                        and intent.targets[0].kind == "task"
                        and name in {"task_effects", "task_events"}
                    )
                    or (
                        intent.kind == "delete_resource"
                        and intent.targets[0].kind == "note"
                        and name == "facts"
                    )
                    or (
                        bool(note_targets(intent))
                        and name == "note_effects"
                        and intent.source_policy == "delete_source"
                    )
                )
            )
            or name in self.registered
            or VECTOR.fullmatch(name) is not None
        )

    @staticmethod
    def _records(document: Document, intent: DeletionIntent, tx: Transaction | None = None) -> bool:
        records = document.get("memory_records", [])
        if not isinstance(records, list):
            return False
        return any(
            isinstance(r, dict)
            and r.get("tenant_id") == intent.tenant_id
            and (
                sourced(
                    {
                        "tenant_id": intent.tenant_id,
                        "source_refs": r.get("content", {}).get("source_refs", []),
                    },
                    intent,
                )
                or (
                    tx is not None
                    and isinstance(r.get("memory_id"), str)
                    and fact_selected(tx, r["memory_id"], intent)
                )
            )
            for r in records
        )

    def _bindings(
        self, document: Document, intent: DeletionIntent, tx: Transaction | None = None
    ) -> bool:
        results = [document.get("result", {})]
        results.extend(
            b.get("result", {}) for b in document.get("tool_bindings", []) if isinstance(b, dict)
        )
        return any(
            isinstance(r, dict)
            and isinstance(r.get("result"), dict)
            and (
                self._records({"memory_records": r["result"].get("memories", [])}, intent, tx)
                or any(
                    isinstance(t, dict) and task_copy(t, intent)
                    for t in r["result"].get("tasks", [])
                )
                or (
                    any(
                        isinstance(n, dict) and note_copy(n, intent)
                        for n in r["result"].get("notes", [])
                    )
                )
            )
            for r in results
        )

    @staticmethod
    def _sources(sources: Document, intent: DeletionIntent) -> bool:
        return any(
            target.kind == "message"
            and sources.get("conversation_id") == target.parent_id
            and any(
                isinstance(m, dict)
                and m.get("resource_id") == target.resource_id
                and m.get("included") is True
                for m in sources.get("messages", [])
            )
            for target in intent.targets
        )

    @staticmethod
    def history_path(intent: DeletionIntent, conversation: str) -> str:
        return (
            operation_path(intent.tenant_id, intent.operation_id)
            + "/purge_conversations/"
            + conversation
        )

    @staticmethod
    def before_acceptance(run: Document, cutoffs: Document) -> bool:
        epoch, boundary = run.get("purge_epoch"), cutoffs.get("_epoch")
        if type(epoch) is int and type(boundary) is int:
            return epoch < boundary
        return TypeAdapter(UtcTimestamp).validate_python(run.get("created_at")) <= TypeAdapter(
            UtcTimestamp
        ).validate_python(cutoffs["_accepted_at"])

    def _run(
        self, tx: Transaction, run: Document, intent: DeletionIntent, cutoffs: Document
    ) -> bool:
        if run.get("tenant_id") != intent.tenant_id:
            raise ValueError("Invalid erasure run tenant")
        evidence = retained(run)
        if (
            run.get("purged_by") == intent.operation_id
            or (
                evidence is not None
                and (
                    evidence.matches(intent)
                    or (
                        intent.kind == "delete_resource"
                        and any(fact_selected(tx, f.memory_id, intent) for f in evidence.facts)
                    )
                )
            )
            or self._records(run, intent, tx)
            or self._bindings(run, intent, tx)
        ):
            return True
        if run.get("forget_operation_id") == intent.operation_id:
            proof = tx.get(
                f"tenants/{intent.tenant_id}/memory_forget_effects/{intent.operation_id}"
            )
            if (
                proof is not None
                and proof.get("operation_id") == intent.operation_id
                and proof.get("run_id") == run.get("run_id")
                and proof.get("request", {}).get("target_id") == intent.targets[0].resource_id
            ):
                return True
        if (
            run.get("forget_operation_id")
            or run.get("purged_by")
            or run.get("note_update")
            or run.get("task_update")
        ) and (evidence is None or not evidence.complete):
            recovered = self._legacy_dependencies(tx, run)
            if recovered.matches(intent):
                return True
            # Outcome checkpoints contain generated responses, not canonical inputs.
            # Do not guess a lost Fact dependency from their text or tool arguments.
            erased_by = run.get("purged_by")
            own = self.verified_operation(tx, erased_by) if erased_by else None
            proof = (
                tx.get(f"tenants/{intent.tenant_id}/memory_forget_effects/{erased_by}")
                if own is not None
                else None
            )
            erased = (
                own is not None
                and own.tenant_id == intent.tenant_id
                and (
                    recovered.matches(own)
                    or run.get("purged_by") == own.operation_id
                    or (
                        proof is not None
                        and proof.get("operation_id") == erased_by
                        and proof.get("run_id") == run.get("run_id")
                        and proof.get("request", {}).get("target_id") == own.targets[0].resource_id
                    )
                )
                and run.get("status") == "failed"
                and run.get("error_code") == "source_deleted"
                and not any(
                    key in run
                    for key in (
                        "streamed_text",
                        "completed_text",
                        "continuation",
                        "memory_records",
                        "tool_bindings",
                    )
                )
            )
            if not erased and self.before_acceptance(run, cutoffs):
                raise LegacyProvenanceMissing()
        conversation = str(run.get("conversation_id"))
        sequence = run.get("input_sequence")
        if type(sequence) is not int:
            return False
        upper = cutoffs.get("_bounds", {}).get(conversation)
        if type(upper) is int and sequence > upper:
            return False
        marker = tx.get(self.history_path(intent, conversation))
        if marker is not None and sequence >= marker["sequence"]:
            if cutoffs.get("_epoch") is None and "history_run_ids" in run:
                # Receipts admitted before barrier deployment use explicit canonical
                # history IDs on new turns; no same-clock timestamp guess for them.
                return (
                    marker["run_id"] in run["history_run_ids"]
                    or run.get("run_id") == marker["run_id"]
                )
            if self.before_acceptance(run, cutoffs):
                return True
        cutoff = cutoffs.get(conversation)
        upper = cutoffs.get("_bounds", {}).get(conversation)
        return (
            cutoff is not None
            and sequence >= cutoff
            and (sequence <= upper if type(upper) is int else self.before_acceptance(run, cutoffs))
        )

    @staticmethod
    def _legacy_dependencies(tx: Transaction, run: Document) -> ErasureDependencies:
        records: list[MemoryRecord] = []
        notes: list[NoteDependency] = []
        tasks: list[TaskDependency] = []
        identities = run.get("tool_call_ids", [])
        # RunTurn permits at most eight steps of eight tool calls. Never scan a
        # collection to reconstruct a missing input or follow arbitrary model IDs.
        if not isinstance(identities, list) or len(identities) > 64:
            raise ValueError("Invalid legacy tool inventory")
        for identity in identities:
            saved = tx.get(f"tenants/{run['tenant_id']}/tool_executions/{identity}")
            if saved is not None:
                records.extend(receipt_records(saved, run, identity))
                notes.extend(receipt_notes(saved, run, identity))
                tasks.extend(receipt_tasks(saved, run, identity))
        return capture(run, records=records, note_dependencies=notes, task_dependencies=tasks)

    def decide(
        self,
        tx: Transaction,
        path: str,
        doc: Document | None,
        intent: DeletionIntent,
        cutoffs: Document,
    ) -> Decision:
        parts = path.split("/")
        root = parts[2]
        if VECTOR.fullmatch(root) and len(parts) == 4:
            selected = fact_selected(tx, parts[3], intent) and (
                intent.kind != "delete_resource" or intent.include_derived_memories
            )
            if (
                selected
                and doc is not None
                and (doc.get("id") != parts[3] or doc.get("tenant_id") != intent.tenant_id)
            ):
                raise ValueError("Invalid vector binding")
            # An exact accepted Fact ID binds this ancestor even without a body.
            return Decision(erase=selected and doc is not None, children=selected)
        if (
            doc is None
            and root == "facts"
            and len(parts) == 4
            and intent.kind == "delete_resource"
            and intent.include_derived_memories
            and fact_selected(tx, parts[3], intent)
        ):
            return Decision(children=True, selected=True)
        if doc is None:
            # Ghost ancestors can contain unproven children. The walker traverses
            # these without erasure authority and refuses completion on a live child.
            return Decision(
                children=root in ROOTS
                or root in {"facts", "note_effects", "task_effects", "task_events"}
                or root in self.registered
            )
        if doc.get("tenant_id", intent.tenant_id) != intent.tenant_id:
            raise ValueError("Invalid canonical tenant")
        if root == "facts" and len(parts) == 4:
            selected = (
                intent.kind == "delete_resource"
                and intent.include_derived_memories
                and sourced(doc, intent)
            )
            return Decision(
                erase=selected,
                children=selected,
                deletion_markers=fact_markers(tx, parts[3], doc, intent) if selected else (),
            )
        if root in {"task_effects", "task_events"} and len(parts) == 4:
            selected = task_copy(doc.get("record", {}) if root == "task_effects" else doc, intent)
            return Decision(erase=selected, children=selected)
        if root == "note_effects" and len(parts) == 4:
            selected = intent.source_policy == "delete_source" and note_copy(
                doc.get("record", {}), intent
            )
            return Decision(erase=selected, children=selected)
        if root in self.registered and len(parts) == 4:
            selected = self.registered[root].select(doc, intent)
            return Decision(erase=selected, children=selected)
        if root == "conversations":
            if len(parts) == 4:
                return Decision(children=True)
            if len(parts) == 6 and parts[4] == "summaries":
                selected = self._sources(doc.get("sources", {}), intent)
                return Decision(erase=selected, children=selected)
            if len(parts) == 6 and parts[4] == "messages":
                # Direct user history survives suppress_reuse and unrelated shared
                # contributors are preserved even when their conversation is tainted.
                if doc.get("role") == "user":
                    return Decision()
                run = tx.get(f"tenants/{intent.tenant_id}/runs/{doc.get('run_id')}")
                selected = self._records(doc, intent, tx) or self._bindings(doc, intent, tx)
                selected |= run is not None and self._run(tx, run, intent, cutoffs)
                return Decision(erase=selected, children=selected)
        if root == "runs" and len(parts) == 4:
            selected = self._run(tx, doc, intent, cutoffs)
            if not selected:
                return Decision()
            # Keep only content-free status and provenance needed to fence old
            # workers and erase their job/tool receipts in later pages.
            keep = {
                key: value
                for key, value in doc.items()
                if key
                in {
                    "run_id",
                    "tenant_id",
                    "conversation_id",
                    "input_message_id",
                    "input_sequence",
                    "created_at",
                    "version",
                    "last_event_seq",
                    "purge_epoch",
                    "history_run_ids",
                    "forget_operation_id",
                    "tool_call_ids",
                }
            }
            evidence = (
                self._legacy_dependencies(tx, doc)
                if (
                    doc.get("forget_operation_id")
                    or doc.get("note_update")
                    or doc.get("task_update")
                )
                and "purge_dependencies" not in doc
                else capture(doc)
            )
            keep["purge_dependencies"] = evidence.model_dump(mode="json")
            # Keep the first erasing operation stable. Other operations bind via
            # retained Fact dependencies, not by oscillating this status marker.
            keep.update(
                status="failed",
                error_code="source_deleted",
                purged_by=doc.get("purged_by", intent.operation_id),
            )
            related = None
            if type(doc.get("input_sequence")) is int and isinstance(
                doc.get("conversation_id"), str
            ):
                related = {
                    "conversation_id": doc["conversation_id"],
                    "sequence": doc["input_sequence"],
                    "run_id": doc["run_id"],
                }
            return Decision(
                replacement=keep if keep != doc else None,
                children=True,
                history=related,
                selected=True,
            )
        if root == "jobs" and len(parts) == 4:
            if doc.get("kind") == "memory_purge":
                return Decision()
            grant = doc.get("grant", {}).get("resource", {}).get("ref", {})
            selected = (
                grant.get("kind") == "facts"
                and isinstance(grant.get("id"), str)
                and fact_selected(tx, grant["id"], intent)
            )
            selected |= self._sources(doc.get("attributes", {}).get("sources", {}), intent)
            if doc.get("kind") == "run_turn":
                run = tx.get(f"tenants/{intent.tenant_id}/runs/{doc.get('operation_id')}")
                selected |= run is not None and self._run(tx, run, intent, cutoffs)
            if selected:
                clean = {
                    **doc,
                    "status": "obsolete",
                    "reason": "source_deleted",
                    "lease_expires_at": None,
                    "dispatch_pending": False,
                    "checkpoints": [],
                    "result": None,
                }
                return Decision(
                    replacement=clean if clean != doc else None, children=True, selected=True
                )
        if root == "memory_effects" and len(parts) == 4:
            selected = any(
                isinstance(record, dict)
                and record.get("tenant_id") == intent.tenant_id
                and isinstance(record.get("id"), str)
                and fact_selected(tx, record["id"], intent)
                for record in (doc.get("record"), doc.get("previous"))
            )
            return Decision(erase=selected, children=selected)
        if root == "run_proposals" and len(parts) == 4:
            run = tx.get(f"tenants/{intent.tenant_id}/runs/{doc.get('run_id')}")
            selected = run is not None and self._run(tx, run, intent, cutoffs)
            return Decision(erase=selected, children=selected)
        if root == "tool_executions" and len(parts) == 4:
            run_id = doc.get("authority", {}).get("run_id")
            run = tx.get(f"tenants/{intent.tenant_id}/runs/{run_id}") if run_id else None
            if self._bindings(doc, intent, tx) or (
                run is not None and self._run(tx, run, intent, cutoffs)
            ):
                updated = None
                if run is not None:
                    records = receipt_records(doc, run, parts[3])
                    notes = receipt_notes(doc, run, parts[3])
                    tasks = receipt_tasks(doc, run, parts[3])
                    if records or notes or tasks:
                        dependencies = capture(
                            run, records=records, note_dependencies=notes, task_dependencies=tasks
                        ).model_dump(mode="json")
                        if dependencies != run.get("purge_dependencies"):
                            updated = {**run, "purge_dependencies": dependencies}
                return Decision(erase=True, children=True, dependency_run=updated)
        return Decision()

    def original(
        self,
        tx: Transaction,
        target: DeletionTarget,
        intent: DeletionIntent,
        job: Job,
        saved: Document,
    ) -> None:
        doc = tx.get(target_path(intent.tenant_id, target))
        if doc is None:
            return
        if (
            doc.get("tenant_id") != intent.tenant_id
            or doc.get("id") != target.resource_id
            or type(doc.get("version")) is not int
            or doc["version"] > target.version + 1
        ):
            raise ValueError("Original target changed")
        if target.kind == "fact":
            if doc.get("owner_user_id") != job.grant.owner_user_id:
                raise ValueError("Original owner changed")
        elif intent.kind == "delete_resource" and target.kind in {"note", "task"}:
            if (
                doc.get("created_by") != job.grant.resource.created_by
                or doc.get("owner_user_id") != job.grant.owner_user_id
            ):
                raise ValueError("Original resource ownership changed")
        elif doc.get("created_by") is not None:
            if doc["created_by"] != saved.get("requested_by"):
                raise ValueError("Source author changed")
        elif target.kind == "message":
            # Legacy message authorship was proved at admission against this run.
            from brainoid_contracts import ResourceId
            from brainoid_jobs.models import job_id

            source_job = tx.get(
                f"tenants/{intent.tenant_id}/jobs/"
                f"{job_id(intent.tenant_id, 'run_turn', ResourceId(doc['run_id']))}"
            )
            if source_job is None or source_job.get("grant", {}).get("principal", {}).get(
                "user_id"
            ) != saved.get("requested_by"):
                raise ValueError("Unproven source author")
        else:
            raise ValueError("Unproven source author")
