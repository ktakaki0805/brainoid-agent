"""Resumable postorder erasure and verification with bounded discovery pages."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from brainoid_contracts import ResourceId, normalize_utc
from brainoid_contracts.transactions import Document, Transaction
from brainoid_jobs import AuthorizationUnavailable, Job, LeaseLost, RetryableError
from brainoid_jobs.models import job_id
from brainoid_memory.calendar_export import completion as calendar_export_completion
from brainoid_memory.deletion_ledger import DeletionIntent
from brainoid_memory.forget import operation_path, target_path
from brainoid_memory.lifecycle import KIND as LIFECYCLE_KIND
from brainoid_memory.push_installations import ACCOUNT_GLOBALS
from brainoid_policy.control_ledger import LedgerError
from pydantic import JsonValue

from brainoid_worker.dispatcher import Dispatcher, JobPages, Queue, RecoveryPage
from brainoid_worker.entry import Execution
from brainoid_worker.lifecycle_handlers import (
    DEVICE_GLOBALS,
    LifecycleHandlers,
    ProfileParentUnknown,
)
from brainoid_worker.note_erasure import fact_selected
from brainoid_worker.purge_authority import KIND, PurgeJobs
from brainoid_worker.purge_handlers import VECTOR, Decision, ForgetHandlers, LegacyProvenanceMissing


@dataclass(frozen=True)
class MonitoringPage:
    inspected: int
    overdue: int
    next_cursor: str | None


class UnprovenDerivedChild(Exception):
    """A missing ancestor permits empty traversal, never deletion of live children."""


class HierarchyPages(Protocol):
    def children(
        self, parent: str, *, collections: bool, token: str = "", limit: int = 50
    ) -> tuple[list[str], str]:
        """One bounded server page; include document-less ancestors of child data."""
        ...


class Invalidation(Protocol):
    def invalidate(self, intent: DeletionIntent, *, key: str) -> str:
        """Idempotent erasure by stable key; retry after ambiguous response is safe.

        Return a content-free durable receipt only after ALL matching external copies
        are invalidated. Implementations must fence concurrent creation with the same
        tombstones, including export downloads. Never return a signed URL or body.
        """
        ...


class AccountFinalizer(Protocol):
    def finalize(self, user: str, *, key: str) -> str:
        """Idempotently delete/revoke the identity after all application erasure."""
        ...


class NoExternalCopies:
    """Explicit composition for deployments with no cache/export object backend.

    Replace this adapter BEFORE enabling such a backend. It does not claim to delete
    remote storage, device caches, provider retention, or future notification services.
    """

    def invalidate(self, intent: DeletionIntent, *, key: str) -> str:
        return "no-backend-v1"


def frame(path: str, *, force: bool = False) -> Document:
    return {
        "path": path,
        "force": force,
        "entered": False,
        "token": "",
        "pending": [],
        "done": False,
    }


class PurgeResource:
    def __init__(
        self,
        jobs: PurgeJobs,
        pages: HierarchyPages,
        *,
        hooks: Mapping[str, Invalidation],
        handlers: ForgetHandlers | None = None,
        page_size: int = 50,
        step_budget: int = 1000,
        account_finalizer: AccountFinalizer | None = None,
    ) -> None:
        if (
            set(hooks) not in ({"cache", "export"}, {"cache", "export", "calendar"})
            or not 1 <= page_size <= 100
            or step_budget < 1
        ):
            raise ValueError("Explicit cache/export adapters and bounded pages required")
        self.jobs, self.pages, self.hooks = jobs, pages, dict(hooks)
        self.account_finalizer = account_finalizer
        self.handlers = handlers or ForgetHandlers()
        self.handlers.verified_operation = self._verified_operation
        self.page_size, self.step_budget = page_size, step_budget

    def _verified_operation(self, tx: Transaction, identity: str) -> DeletionIntent | None:
        # Cross-operation evidence requires the same independently readable ledger,
        # accepted envelope and exact tombstones; a bare DB flag is not authority.
        try:
            intent = self.jobs.deletion_ledger.get(ResourceId(identity))
        except LedgerError:
            raise AuthorizationUnavailable() from None
        if intent is None:
            return None
        raw = tx.get(
            f"tenants/{intent.tenant_id}/jobs/{job_id(intent.tenant_id, KIND, intent.operation_id)}"
        )
        if raw is None:
            return None
        try:
            _, saved = self.jobs.accepted(tx, Job.model_validate(raw))
        except ValueError:
            return None
        state = saved.get("purge", {})
        if (
            state.get("schema_version") != 1
            or state.get("locally_verified") is not True
            or state.get("pass_changes") != 0
            or state.get("passes", 0) < 1
            or set(state.get("receipts", {}))
            not in ({"cache", "export"}, {"cache", "export", "calendar"})
            or (state.get("calendar_required") and "calendar" not in state.get("receipts", {}))
        ):
            return None
        return intent

    def _stack(self, intent: DeletionIntent, saved: Document | None = None) -> list[Document]:
        if intent.kind not in ("forget_memory", "delete_resource"):
            scopes = [
                frame(f"tenants/{s.tenant_id}") for s in intent.scopes if s.tenant_id is not None
            ]
            if intent.kind == "delete_account":
                # Bounded show_missing discovers unknown global parents too. The
                # account handler selects only this canonical UID, never a path guess.
                scopes.append(frame("users"))
                scopes.extend(frame(collection) for collection in ACCOUNT_GLOBALS)
            if intent.kind == "delete_tenant":
                scopes.extend(frame(collection) for collection in DEVICE_GLOBALS)
            return scopes
        # LIFO: derived scan first, original targets last. Sources are available
        # during the first scan; their cutoffs are retained for later verification.
        targets = [
            t for t in intent.targets if t.kind == "fact" or intent.source_policy == "delete_source"
        ]
        return [
            *(frame(target_path(intent.tenant_id, t), force=True) for t in reversed(targets)),
            frame(f"tenants/{intent.tenant_id}"),
        ]

    def _read(self, tx: Transaction, execution: Execution) -> tuple[Job, DeletionIntent, Document]:
        job = self.jobs._live(tx, execution.lease)
        if job is None:
            raise LeaseLost()
        intent, saved = self.jobs.accepted(tx, job)
        if intent.kind not in ("forget_memory", "delete_resource") and not isinstance(
            self.handlers, LifecycleHandlers
        ):
            raise ValueError("Lifecycle handler composition required")
        if intent.kind in ("forget_memory", "delete_resource") and isinstance(
            self.handlers, LifecycleHandlers
        ):
            raise ValueError("Forget handler composition required")
        if intent.kind == "delete_account" and self.account_finalizer is None:
            raise ValueError("Account provider finalizer required")
        if intent.kind == "disconnect_calendar" and "calendar" not in self.hooks:
            raise ValueError("Calendar secret erasure hook required")
        if saved.get("purge", {}).get("schema_version", 1) != 1:
            raise ValueError("Unsupported purge progress schema")
        if "schema_version" not in saved.get("purge", {}):
            cutoffs: Document = {
                "_bounds": saved.get("purge_bounds", {}),
                "_epoch": saved.get("purge_admission_epoch"),
                "_accepted_at": saved.get("purge_accepted_at", saved["operation"]["updated_at"]),
            }
            for target in intent.targets:
                if target.kind == "message":
                    source = tx.get(target_path(intent.tenant_id, target))
                    sequence = 0 if source is None else source.get("sequence", 0)
                    if type(sequence) is not int:
                        raise ValueError("Invalid source sequence")
                    parent = str(target.parent_id)
                    cutoffs[parent] = min(cutoffs.get(parent, sequence), sequence)
            saved["purge"] = {
                "schema_version": 1,
                "stack": self._stack(intent, saved),
                "cutoffs": cutoffs,
                "processed": 0,
                "pages": 0,
                "pass_changes": 0,
                "passes": 0,
                "receipts": {},
                "retry_reason": None,
                "overdue": False,
                "verified": False,
            }
        if saved["purge"].get("calendar_required") and "calendar" not in self.hooks:
            raise RetryableError()
        if saved["purge"]["stack"]:
            self._bound_path(tx, job, intent, saved, saved["purge"]["stack"][-1])
        return job, intent, saved

    def _bound_path(
        self, tx: Transaction, job: Job, intent: DeletionIntent, saved: Document, item: Document
    ) -> bool:
        cutoffs = saved["purge"]["cutoffs"]
        path = item["path"]
        if intent.kind not in ("forget_memory", "delete_resource"):
            assert isinstance(self.handlers, LifecycleHandlers)
            return self.handlers.bound(tx, path, intent, item["force"])
        base = f"tenants/{intent.tenant_id}"
        if path == base and not item["force"]:
            return True
        if not path.startswith(base + "/"):
            raise ValueError("Cursor escapes accepted tenant")
        for target in intent.targets:
            if target.kind != "fact" and intent.source_policy != "delete_source":
                continue
            allowed = target_path(intent.tenant_id, target)
            if path == allowed or path.startswith(allowed + "/"):
                self.handlers.original(tx, target, intent, job, saved)
                return True
        parts = path.split("/")
        if not self.handlers.root(parts[2], intent):
            raise ValueError("Cursor escapes registered derived inventory")
        if VECTOR.fullmatch(parts[2]) and len(parts) >= 4 and fact_selected(tx, parts[3], intent):
            ancestor = "/".join(parts[:4])
            self.handlers.decide(tx, ancestor, tx.get(ancestor), intent, cutoffs)
            return True
        if (
            intent.kind == "delete_resource"
            and intent.include_derived_memories
            and parts[2] == "facts"
            and len(parts) >= 4
            and fact_selected(tx, parts[3], intent)
        ):
            return True
        depth = 6 if parts[2] == "conversations" else 4
        if len(parts) >= depth:
            ancestor = "/".join(parts[:depth])
            doc = tx.get(ancestor)
            if doc is None:
                # A peer may have removed this selected ancestor during a pause.
                # Return no deletion authority. Read-only traversal proves emptiness
                # of the remaining branch; a live child must stop, not be skipped.
                return False
        if item["force"]:
            # Durable progress is not itself an authority to add deletion targets.
            # Revalidate the selected canonical ancestor before traversing/deleting
            # any inherited subtree, including after retries/restoration.
            if len(parts) < depth:
                raise ValueError("Unbound derived deletion")
            decision = self.handlers.decide(tx, ancestor, doc, intent, cutoffs)
            if not (
                decision.erase
                or decision.selected
                or decision.replacement is not None
                or (doc is not None and doc.get("purged_by") == intent.operation_id)
            ):
                raise ValueError("Changed derived deletion scope")
        return True

    def _save(self, tx: Transaction, job: Job, saved: Document) -> None:
        now = normalize_utc(self.jobs.now())
        saved["operation"].update(status="running", updated_at=now.isoformat())
        saved["purge"]["overdue"] = now - job.created_at > timedelta(hours=24)
        if saved["purge"]["pass_changes"]:
            saved["purge"]["locally_verified"] = False
        tx.set(operation_path(job.tenant_id, job.operation_id), saved)
        self.jobs._write(tx, job, lease_expires_at=now + self.jobs.lease_duration)

    def _decide(
        self,
        tx: Transaction,
        path: str,
        doc: Document | None,
        intent: DeletionIntent,
        state: Document,
    ) -> Decision:
        try:
            if (
                isinstance(self.handlers, LifecycleHandlers)
                and doc is not None
                and (
                    "/calendar_authorizations/" in path
                    or ("/integrations/" in path and doc.get("provider") == "google_calendar")
                )
                and self.handlers.selected(tx, path, doc, intent)
            ):
                # A canonical deletion without its external credential erasure is
                # incomplete; never let NoExternalCopies certify this producer.
                state["calendar_required"] = True
                if "calendar" not in self.hooks:
                    raise LegacyProvenanceMissing()
            return self.handlers.decide(tx, path, doc, intent, state["cutoffs"])
        except LegacyProvenanceMissing as error:
            # Continue bounded discovery so another unknown run cannot prevent
            # this operation's own accepted cleanup. Unknown scopes are untouched.
            state["legacy_pending"] = True
            state["legacy_reason"] = (
                "profile_parent_unknown"
                if isinstance(error, ProfileParentUnknown)
                else "legacy_provenance_missing"
            )
            return Decision()

    def step(self, execution: Execution) -> bool:
        """One durable step. Discovery is untrusted until lease/cursor revalidation."""
        try:
            return self._step(execution)
        except LegacyProvenanceMissing:
            self._reason(execution, "legacy_provenance_missing")
            raise RetryableError() from None
        except UnprovenDerivedChild:
            self._reason(execution, "derived_ancestor_missing")
            raise RetryableError() from None

    def _step(self, execution: Execution) -> bool:
        _, intent, snapshot = self.jobs.store.run(lambda tx: self._read(tx, execution))
        progress = snapshot["purge"]
        stack = progress["stack"]
        if not stack:
            if progress["passes"] == 0 or progress["pass_changes"]:

                def restart(tx: Transaction) -> None:
                    job, current, saved = self._read(tx, execution)
                    if saved["purge"] != progress:
                        raise LeaseLost()
                    saved["purge"].update(
                        stack=self._stack(current, saved),
                        pass_changes=0,
                        passes=progress["passes"] + 1,
                        legacy_pending=False,
                    )
                    self._save(tx, job, saved)

                self.jobs.store.run(restart)
                return False
            for name, hook in self.hooks.items():
                if name in progress["receipts"]:
                    continue
                execution.check(renew=True)
                # This port is specifically idempotent deletion, never a generic
                # provider effect. Lost receipts are reconciled with the same key.
                try:
                    receipt = hook.invalidate(intent, key=f"{intent.operation_id}:{name}:v1")
                    if not isinstance(receipt, str) or not 1 <= len(receipt) <= 100:
                        raise ValueError("Invalid invalidation receipt")
                except Exception:
                    self._reason(execution, f"{name}_invalidation_failed")
                    raise RetryableError() from None

                def record(tx: Transaction, name: str = name, receipt: str = receipt) -> None:
                    job, _, saved = self._read(tx, execution)
                    if saved["purge"] != progress:
                        raise LeaseLost()
                    saved["purge"]["receipts"][name] = receipt
                    self._save(tx, job, saved)

                self.jobs.store.run(record)
                return False

            if (
                intent.kind == "delete_account"
                and not progress.get("legacy_pending", False)
                and "identity" not in progress["receipts"]
            ):
                execution.check(renew=True)
                assert self.account_finalizer is not None
                try:
                    receipt = self.account_finalizer.finalize(
                        snapshot["requested_by"], key=f"{intent.operation_id}:identity:v1"
                    )
                    if not isinstance(receipt, str) or not 1 <= len(receipt) <= 100:
                        raise ValueError("Invalid identity receipt")
                except Exception:
                    self._reason(execution, "identity_finalization_failed")
                    raise RetryableError() from None

                def identity_receipt(tx: Transaction) -> None:
                    job, _, saved = self._read(tx, execution)
                    if saved["purge"] != progress:
                        raise LeaseLost()
                    saved["purge"]["receipts"]["identity"] = receipt
                    self._save(tx, job, saved)

                self.jobs.store.run(identity_receipt)
                return False

            def verified(tx: Transaction) -> None:
                job, current, saved = self._read(tx, execution)
                if saved["purge"] != progress:
                    raise LeaseLost()
                blocked = progress.get("legacy_pending", False)
                saved["purge"].update(
                    locally_verified=True,
                    verified=not blocked,
                    retry_reason=progress.get("legacy_reason", "legacy_provenance_missing")
                    if blocked
                    else None,
                )
                if blocked:
                    # Only the positively bound cleanup and its hooks are verified.
                    # The operation stays pending until all unknown legacy inputs
                    # resolve, and retries rescan from the beginning without resets
                    # of processed counts or completed invalidation receipts.
                    saved["purge"].update(
                        stack=self._stack(current, saved),
                        legacy_pending=False,
                        pass_changes=0,
                        passes=progress["passes"] + 1,
                    )
                self._save(tx, job, saved)

            self.jobs.store.run(verified)
            if progress.get("legacy_pending", False):
                raise RetryableError()
            return True
        top = stack[-1]
        current_path = top["path"]
        is_document = len(current_path.split("/")) % 2 == 0
        discovered: tuple[list[str], str] | None = None
        if top["entered"] and not top["pending"] and not top["done"]:
            discovered = self.pages.children(
                current_path, collections=is_document, token=top["token"], limit=self.page_size
            )
            paths, token = discovered
            if (
                len(paths) > self.page_size
                or len(set(paths)) != len(paths)
                or any(
                    not p.startswith(current_path + "/") or "/" in p[len(current_path) + 1 :]
                    for p in paths
                )
            ):
                raise ValueError("Invalid hierarchy page")
            if token and token == top["token"]:
                raise ValueError("Nonadvancing hierarchy cursor")

        def apply(tx: Transaction) -> None:
            job, current, saved = self._read(tx, execution)
            if saved["purge"] != progress:
                raise LeaseLost()
            state = saved["purge"]
            frames = state["stack"]
            item = frames[-1]
            bound = self._bound_path(tx, job, current, saved, item)
            if not bound and is_document and tx.get(current_path) is not None:
                raise UnprovenDerivedChild()
            erase = False
            replacement = None
            doc = None
            related = None
            dependency_run = None
            deletion_markers: tuple[tuple[str, Document], ...] = ()
            if not item["entered"]:
                item["entered"] = True
                if is_document and current_path != f"tenants/{current.tenant_id}":
                    doc = tx.get(current_path)
                    if item["force"]:
                        if (
                            doc is not None
                            and current_path.startswith("tenants/")
                            and doc.get("tenant_id", current_path.split("/")[1])
                            != current_path.split("/")[1]
                        ):
                            raise ValueError("Child tenant mismatch")
                        for target in current.targets:
                            if current_path == target_path(current.tenant_id, target):
                                self.handlers.original(tx, target, current, job, saved)
                        item["erase"] = True
                    else:
                        decision = self._decide(tx, current_path, doc, current, state)
                        related = decision.history
                        dependency_run = decision.dependency_run
                        deletion_markers = decision.deletion_markers
                        item["erase"] = decision.erase
                        # Selection is rechecked at finalization, never stored bodies.
                        item["selected"] = (
                            decision.erase
                            or decision.selected
                            or (
                                decision.children
                                and len(current_path.split("/")) > 2
                                and VECTOR.fullmatch(current_path.split("/")[2]) is not None
                            )
                            or decision.replacement is not None
                            or (doc is not None and doc.get("purged_by") == current.operation_id)
                        )
                        if not decision.children:
                            item["done"] = True
            elif item["pending"]:
                child = item["pending"].pop(0)
                force = item["force"] or item.get("selected", False)
                if current_path == f"tenants/{current.tenant_id}" and not self.handlers.root(
                    child.rsplit("/", 1)[-1], current
                ):
                    pass
                else:
                    frames.append(frame(child, force=force))
            elif discovered is not None:
                paths, token = discovered
                item.update(pending=paths, token=token, done=not token)
                state["pages"] += 1
            else:
                if is_document and current_path != f"tenants/{current.tenant_id}":
                    doc = tx.get(current_path)
                    if item["force"]:
                        if (
                            doc is not None
                            and current_path.startswith("tenants/")
                            and doc.get("tenant_id", current_path.split("/")[1])
                            != current_path.split("/")[1]
                        ):
                            raise ValueError("Child tenant mismatch")
                        for target in current.targets:
                            if current_path == target_path(current.tenant_id, target):
                                self.handlers.original(tx, target, current, job, saved)
                        erase = doc is not None
                    else:
                        decision = self._decide(tx, current_path, doc, current, state)
                        related = decision.history
                        dependency_run = decision.dependency_run
                        deletion_markers = decision.deletion_markers
                        erase, replacement = decision.erase, decision.replacement
                    if erase or replacement is not None:
                        state["processed"] += 1
                        state["pass_changes"] += 1
                frames.pop()
            marker_path = None
            if related is not None:
                location = self.handlers.history_path(current, related["conversation_id"])
                marker = tx.get(location)
                if marker is None or related["sequence"] < marker["sequence"]:
                    marker_path = location
                    state["pass_changes"] += 1
            for deletion_marker in deletion_markers:
                tx.set(*deletion_marker)
            if marker_path is not None:
                assert related is not None
                tx.set(marker_path, related)
            if dependency_run is not None:
                # Preserve every canonical Fact reference before erasing the last
                # receipt that carries it. No external effects or extra DB scans.
                tx.set(
                    f"tenants/{current.tenant_id}/runs/{dependency_run['run_id']}",
                    dependency_run,
                )
                state["pass_changes"] += 1
            if (erase or (item.get("selected") and not item["force"])) and isinstance(
                self.handlers, LifecycleHandlers
            ):
                self.handlers.record_selection(tx, current_path, current)
            if erase:
                tx.delete(current_path)
            elif replacement is not None:
                tx.set(current_path, replacement)
            self._save(tx, job, saved)

        self.jobs.store.run(apply)
        return False

    def _reason(self, execution: Execution, reason: str) -> None:
        def record(tx: Transaction) -> None:
            job = self.jobs._live(tx, execution.lease)
            if job is None:
                raise LeaseLost()
            _, saved = self.jobs.accepted(tx, job)
            # Recording a reason neither consumes nor authorizes the blocked cursor.
            saved["purge"]["retry_reason"] = reason
            self._save(tx, job, saved)

        self.jobs.store.run(record)

    def run(self, execution: Execution) -> JsonValue:
        for _ in range(self.step_budget):
            if self.step(execution):
                return {"purged": True}
        raise RetryableError()

    def commit(self, tx: Transaction, job: Job, result: JsonValue) -> None:
        intent, saved = self.jobs.accepted(tx, job)
        state = saved.get("purge", {})
        if (
            result != {"purged": True}
            or not state.get("verified")
            or state.get("stack")
            or set(state.get("receipts", {}))
            != (set(self.hooks) | ({"identity"} if intent.kind == "delete_account" else set()))
        ):
            raise ValueError("Unverified purge")
        tenant_path = f"tenants/{intent.tenant_id}"
        tenant = tx.get(tenant_path) if intent.kind == "delete_tenant" else None
        saved["operation"].update(
            status="succeeded", updated_at=normalize_utc(self.jobs.now()).isoformat()
        )
        saved["purge"]["retry_reason"] = None
        if intent.kind == "delete_account":
            saved["provider_finalized"] = True
        if tenant is not None:
            tx.set(
                tenant_path,
                {
                    k: v
                    for k, v in tenant.items()
                    if k in {"id", "tenant_id", "status", "policy_version", "deletion_epoch"}
                },
            )
        if intent.kind == "disconnect_calendar":
            export_writes = calendar_export_completion(tx, intent)
            location = f"tenants/{intent.tenant_id}/integrations/{intent.scopes[0].resource_id}"
            integration = tx.get(location)
            if (
                integration is not None
                and integration.get("disconnect_operation_id") == intent.operation_id
            ):
                tx.set(
                    location,
                    {
                        **integration,
                        "status": "revoked",
                        "cleanup_status": "complete",
                        "version": integration["version"] + 1,
                        "updated_at": self.jobs.now(),
                    },
                )
            for path, document in export_writes:
                tx.set(path, document)
        tx.set(operation_path(job.tenant_id, job.operation_id), saved)

    def monitor(
        self, tenant: ResourceId, pages: JobPages, *, after: str | None = None, limit: int = 100
    ) -> MonitoringPage:
        documents = pages.page(f"tenants/{tenant}/operations", after=after, limit=limit)
        overdue = 0
        for path, _ in documents:

            def update(tx: Transaction, path: str = path) -> bool:
                saved = tx.get(path)
                if saved is None or saved.get("operation", {}).get("kind") not in {
                    "forget_memory",
                    "delete_resource",
                    "leave_tenant",
                    "delete_tenant",
                    "delete_account",
                    "disconnect_calendar",
                }:
                    return False
                intent = DeletionIntent.model_validate(saved["intent"])
                late = saved["operation"]["status"] != "succeeded" and normalize_utc(
                    self.jobs.now()
                ) - intent.created_at > timedelta(hours=24)
                saved.setdefault("purge_monitor", {}).update(
                    overdue=late, checked_at=normalize_utc(self.jobs.now()).isoformat()
                )
                tx.set(path, saved)
                return late

            overdue += self.jobs.store.run(update)
        return MonitoringPage(
            len(documents),
            overdue,
            documents[-1][0].rsplit("/", 1)[-1] if len(documents) == limit else None,
        )


class PurgeDispatcher:
    def __init__(self, handler: PurgeResource, pages: JobPages, queue: Queue) -> None:
        self.handler, self.pages = handler, pages
        self.dispatcher = Dispatcher(
            handler.jobs,
            pages,
            queue,
            queue_name=LIFECYCLE_KIND if isinstance(handler.handlers, LifecycleHandlers) else KIND,
        )

    def recover(
        self, tenant: ResourceId, *, after: str | None = None, limit: int = 100
    ) -> RecoveryPage:
        documents = self.pages.page(f"tenants/{tenant}/jobs", after=after, limit=limit)
        for location, document in documents:
            if document.get("kind") == self.dispatcher.queue_name:
                identity = ResourceId(location.rsplit("/", 1)[-1])
                if document.get("status") == "waiting":
                    self.handler.jobs.activate(tenant, identity)
                else:
                    self.handler.jobs.recover_expired(tenant, identity)
        return self.dispatcher.recover(tenant, after=after, limit=limit)
