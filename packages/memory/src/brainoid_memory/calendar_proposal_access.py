"""Current original producer and source fences for proposal previews and exports."""

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from functools import partial

from brainoid_contracts import ResourceId
from brainoid_contracts.access import fingerprint
from brainoid_contracts.deletion import fenced_document, purge_epoch
from brainoid_contracts.errors import APIError
from brainoid_contracts.guards import GuardedTransaction, protect
from brainoid_contracts.transactions import Document, Transaction
from brainoid_policy.devices import CredentialBinding, current_family
from brainoid_policy.evaluator import Evaluator
from brainoid_policy.models import Action, ResourceKind, ResourceRef, Subject
from brainoid_policy.scopes import PolicyDenied, Principal
from pydantic import TypeAdapter

from brainoid_memory.calendar_access import current_integration
from brainoid_memory.calendar_proposals import CalendarProposals, proposal_path, validate_proposal
from brainoid_memory.calendar_reader import integration_stamp
from brainoid_memory.direct_resources import DirectRecord
from brainoid_memory.models import Fact
from brainoid_memory.notes import Note
from brainoid_memory.reminders import Reminder
from brainoid_memory.repository import check_fact_dependencies, check_model_fact
from brainoid_memory.sources import SourceResolver
from brainoid_memory.tasks import Task


def _model_validity(fact: Fact, now: Callable[[], datetime]) -> None:
    check_model_fact(fact, now())


def _check_origin(
    tx: Transaction,
    policy: Evaluator,
    record: Document,
    *,
    now: Callable[[], datetime],
    credential_epoch: ResourceId | None,
    sources: SourceResolver,
    for_model: bool = False,
) -> Principal:
    validate_proposal(record)
    tenant = ResourceId(record["tenant_id"])
    record = fenced_document(tx, tenant, "calendar_proposals", ResourceId(record["id"]), record)
    if record.get("deleted") is not False or record.get("suppress_reuse"):
        raise PolicyDenied()
    origin = record["origin"]
    principal = policy.principal(tenant, TypeAdapter(Subject).validate_python(origin["subject"]))
    integration, scope = current_integration(
        tx,
        policy,
        principal,
        ResourceId(record["integration_id"]),
        calendar=record["normalized_request"]["calendar_id"],
        operation="calendar.propose",
    )
    CalendarProposals.authorized(record, integration, scope)
    if scope.binding() != record["binding"] or origin["purge_epoch"] != purge_epoch(tx, tenant):
        raise PolicyDenied()
    protect(tx, lambda: scope.ensure_live(now()))
    if origin.get("credential") is not None:
        current_family(
            tx,
            CredentialBinding.model_validate(origin["credential"]),
            credential_epoch=credential_epoch,
            now=now,
        )
    output = (
        TypeAdapter(Subject).validate_python(origin["output"]) if origin.get("output") else None
    )
    if output is not None:
        _, output_scope = current_integration(
            tx,
            policy,
            policy.principal(tenant, output),
            ResourceId(record["integration_id"]),
            calendar=record["normalized_request"]["calendar_id"],
            operation="calendar.propose",
        )
        protect(tx, lambda: output_scope.ensure_live(now()))
    for group in origin.get("dependencies", []):
        if group.get("tenant_id") != tenant or not group.get("complete"):
            raise PolicyDenied()
        for collection, identity_field in (
            ("facts", "memory_id"),
            ("notes", "note_id"),
            ("tasks", "task_id"),
            ("reminders", "reminder_id"),
        ):
            for dependency in group.get(collection, []):
                identity = ResourceId(dependency[identity_field])
                document = tx.get(f"tenants/{tenant}/{collection}/{identity}")
                if document is None:
                    raise PolicyDenied()
                document = fenced_document(tx, tenant, collection, identity, document)
                if (
                    document.get("version") != dependency["version"]
                    or document.get("deleted")
                    or document.get("suppress_reuse")
                ):
                    raise PolicyDenied()
                policy.resource(
                    tenant,
                    principal.subject,
                    ResourceRef(tenant_id=tenant, kind=ResourceKind(collection), id=identity),
                    action=Action.MODEL_READ if for_model else Action.READ,
                    output=output,
                )
                if collection == "facts":
                    fact = Fact.model_validate(document)
                    if fact.id != identity or fact.tenant_id != tenant:
                        raise PolicyDenied()
                    check_fact_dependencies(
                        tx,
                        principal.context,
                        policy,
                        principal.subject,
                        fact,
                        sources=sources,
                        output=output,
                        reuse=True,
                    )
                    if for_model:
                        protect(tx, partial(_model_validity, fact, now))
                else:
                    models: dict[str, type[DirectRecord]] = {
                        "notes": Note,
                        "tasks": Task,
                        "reminders": Reminder,
                    }
                    direct = models[collection].model_validate(document)
                    if direct.id != identity or direct.tenant_id != tenant or direct.deletion_epoch:
                        raise PolicyDenied()
    for source_run_id in {g["run_id"] for g in origin.get("dependencies", [])}:
        run = tx.get(f"tenants/{tenant}/runs/{source_run_id}")
        if run is None or run.get("purged_by"):
            raise PolicyDenied()
        parent = ResourceId(run["conversation_id"])
        policy.resource(
            tenant,
            principal.subject,
            ResourceRef(tenant_id=tenant, kind=ResourceKind.CONVERSATION, id=parent),
            action=Action.MODEL_READ if for_model else Action.READ,
            output=output,
        )
        message_id = ResourceId(run["input_message_id"])
        message = tx.get(f"tenants/{tenant}/conversations/{parent}/messages/{message_id}")
        if message is None:
            raise PolicyDenied()
        message = fenced_document(tx, tenant, "messages", message_id, message, parent=parent)
        if (
            message.get("deleted") is not False
            or message.get("suppress_reuse")
            or message.get("suppressed_spans")
            or message.get("text") != run["input_text"]
        ):
            raise PolicyDenied()
    return principal


def check_origin(
    tx: Transaction,
    policy: Evaluator,
    record: Document,
    *,
    now: Callable[[], datetime],
    credential_epoch: ResourceId | None,
    sources: SourceResolver,
    for_model: bool = False,
) -> None:
    _SourceChecks(
        tx,
        policy,
        now=now,
        credential_epoch=credential_epoch,
        sources=sources,
        for_model=for_model,
    ).check_origin(record)


# A normal complete DAG of 64 proposals has at most 2,080 source edges including
# the caller's root inventory. Bound work as well as unique nodes for malformed DB data.
MAX_PROPOSALS = 64
MAX_SOURCE_EDGES = 4096


@dataclass(frozen=True)
class _Origin:
    record: Document


@dataclass(frozen=True)
class _Sources:
    principal: Principal
    document: Document


@dataclass(frozen=True)
class _Source:
    principal: Principal
    source: Document


@dataclass(frozen=True)
class _Leave:
    key: tuple[ResourceId, ResourceId]


class _SourceChecks:
    """One invocation only: fixed conditions, iterative DFS and retained no-I/O guards.

    Never put this object on a service, transaction adapter or saved document. Public
    entrypoints create a fresh pass, including on transaction retry and mode changes.
    Origin authority/output is intrinsic to the exact immutable record. Reader source
    grants are separate and keyed by the current principal as well as exact source.
    """

    def __init__(
        self,
        tx: Transaction,
        policy: Evaluator,
        *,
        now: Callable[[], datetime],
        credential_epoch: ResourceId | None,
        sources: SourceResolver | None,
        for_model: bool,
    ) -> None:
        self.tx = GuardedTransaction(tx)
        self.policy, self.now = policy, now
        self.credential_epoch, self.sources, self.for_model = credential_epoch, sources, for_model
        self.records: dict[tuple[ResourceId, ResourceId], Document] = {}
        self.active: set[tuple[ResourceId, ResourceId]] = set()
        self.grants: dict[str, Document | None] = {}
        self.edges = 0
        self.used = False

    def check_sources(self, principal: Principal, document: Document) -> None:
        self._walk([_Sources(principal, document)])

    def check_origin(self, record: Document) -> None:
        self._walk([_Origin(record)])

    def _source(self, item: _Source) -> Document | None:
        principal, source = item.principal, item.source
        key = fingerprint(
            [
                principal.context.model_dump(mode="json"),
                principal.subject.model_dump(mode="json"),
                principal.stamp,
                source,
            ]
        )
        if key in self.grants:
            return self.grants[key]
        integration, scope = current_integration(
            self.tx,
            self.policy,
            principal,
            ResourceId(source["integration_id"]),
            calendar=source["calendar_id"],
            operation=source.get("operation", "calendar.read"),
        )
        if source != {
            "integration_id": integration["id"],
            "calendar_id": source["calendar_id"],
            "stamp": integration_stamp(integration),
            **(
                {"operation": "calendar.propose", "proposal_id": source.get("proposal_id")}
                if source.get("operation") == "calendar.propose"
                else {}
            ),
        }:
            raise PolicyDenied()
        protect(self.tx, lambda: scope.ensure_live(self.now()))
        proposal = None
        if source.get("operation") == "calendar.propose":
            if self.sources is None:
                raise PolicyDenied()
            proposal = self.tx.get(
                proposal_path(principal.context.tenant_id, ResourceId(source["proposal_id"]))
            )
            if proposal is None or proposal.get("calendar_source") != source:
                raise PolicyDenied()
        self.grants[key] = proposal
        return proposal

    def _walk(self, work: list[_Origin | _Sources | _Source | _Leave]) -> None:
        if self.used:
            raise PolicyDenied()
        self.used = True
        try:
            while work:
                item = work.pop()
                if isinstance(item, _Leave):
                    self.active.remove(item.key)
                elif isinstance(item, _Sources):
                    values = item.document.get("calendar_sources", [])
                    if not isinstance(values, list) or self.edges + len(values) > MAX_SOURCE_EDGES:
                        raise PolicyDenied()
                    self.edges += len(values)
                    for source in reversed(values):
                        if not isinstance(source, dict):
                            raise PolicyDenied()
                        work.append(_Source(item.principal, source))
                elif isinstance(item, _Source):
                    proposal = self._source(item)
                    if proposal is not None:
                        work.append(_Origin(proposal))
                else:
                    record = item.record
                    key = (ResourceId(record["tenant_id"]), ResourceId(record["id"]))
                    if key in self.active:
                        raise PolicyDenied()
                    if key in self.records:
                        if record != self.records[key]:
                            raise PolicyDenied()
                        continue
                    if len(self.records) >= MAX_PROPOSALS or self.sources is None:
                        raise PolicyDenied()
                    self.records[key] = deepcopy(record)
                    self.active.add(key)
                    principal = _check_origin(
                        self.tx,
                        self.policy,
                        record,
                        now=self.now,
                        credential_epoch=self.credential_epoch,
                        sources=self.sources,
                        for_model=self.for_model,
                    )
                    work.extend((_Leave(key), _Sources(principal, record["origin"])))
            # Only a successful pass contributes release evidence. Consumers may
            # omit denied history/export items and continue in this transaction.
            # Retain every unique ancestor's deadlines through the outer response.
            protect(self.tx.transaction, self._finish)
        except (APIError, ValueError, KeyError, TypeError):
            raise PolicyDenied() from None

    def _finish(self) -> None:
        try:
            self.tx.finish()
        except (APIError, ValueError, KeyError, TypeError):
            raise PolicyDenied() from None
