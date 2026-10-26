"""Canonical parent/child reads shared by API admission and the worker.

Conversation version is the settings/completed-history fence. One active run owns
the append slot; accepting its user message does not replace the saved source grant.
Every child is checked against that parent, its version and its immutable content.
"""

from collections.abc import Callable
from datetime import datetime
from typing import get_args

from brainoid_agent_core.context import (
    ContextInput,
    ConversationContext,
    ConversationSummary,
    snapshot_message,
)
from brainoid_agent_core.definition import AgentDefinition
from brainoid_agent_core.types import ModelMessage, TextPart
from brainoid_contracts import (
    DeviceActorContext,
    DisplayPolicy,
    ResourceId,
    Scope,
    UtcTimestamp,
    Visibility,
    normalize_utc,
)
from brainoid_contracts.deletion import fenced_document
from brainoid_contracts.guards import protect
from brainoid_contracts.reuse import direct_user, reusable_text
from brainoid_contracts.transactions import Document, Transaction
from brainoid_jobs import Job
from brainoid_jobs.models import Reason, job_id
from brainoid_jobs.repository import path as job_path
from brainoid_policy.accounts import AccountUnavailable, account_stamp, fingerprint
from brainoid_policy.control_ledger import ControlLedger
from brainoid_policy.models import Action, ResourceKind
from brainoid_policy.scopes import PolicyDenied
from brainoid_policy.transactions import transaction_policy
from brainoid_usage import QuotaExceeded, live_run
from pydantic import BaseModel, TypeAdapter

from brainoid_runs.models import CredentialBinding, Message, Run

KIND = "run_turn"


def conversation_path(tenant: ResourceId, conversation: ResourceId) -> str:
    return f"tenants/{tenant}/conversations/{conversation}"


def run_path(tenant: ResourceId, run: ResourceId) -> str:
    return f"tenants/{tenant}/runs/{run}"


def view[T: BaseModel](model: type[T], document: Document) -> T:
    # Deliberate projection: public DTOs never expose execution receipts or signatures.
    return model.model_validate(
        {field: document[field] for field in model.model_fields if field in document}
    )


def agent(tx: Transaction, tenant: ResourceId, identity: ResourceId) -> AgentDefinition:
    doc = tx.get(f"tenants/{tenant}/agents/{identity}")
    if doc is None or doc.get("tenant_id") != tenant or doc.get("id") != identity:
        raise PolicyDenied()
    return view(AgentDefinition, doc)


def history(
    tx: Transaction, tenant: ResourceId, conversation: ResourceId
) -> tuple[list[Document], tuple[ModelMessage, ...], str]:
    raw = tx.collection(conversation_path(tenant, conversation) + "/messages")
    return project_history(
        tenant,
        conversation,
        [
            (
                path,
                fenced_document(
                    tx,
                    tenant,
                    "messages",
                    TypeAdapter(ResourceId).validate_python(doc.get("id")),
                    doc,
                    parent=conversation,
                ),
            )
            for path, doc in raw
        ],
    )


def project_history(
    tenant: ResourceId,
    conversation: ResourceId,
    raw: list[tuple[str, Document]],
) -> tuple[list[Document], tuple[ModelMessage, ...], str]:
    """Pure canonical projection, also used before an atomic forget commit."""
    base = conversation_path(tenant, conversation)
    documents = []
    for path, doc in raw:
        if (
            path != base + "/messages/" + str(doc.get("id"))
            or doc.get("tenant_id") != tenant
            or doc.get("conversation_id") != conversation
            or type(doc.get("version")) is not int
            or doc["version"] < 1
            or type(doc.get("deletion_epoch")) is not int
            or doc["deletion_epoch"] < 0
            or type(doc.get("deleted")) is not bool
            or type(doc.get("suppress_reuse")) is not bool
            or type(doc.get("sequence")) is not int
            or doc["sequence"] < 1
            or doc.get("role") not in ("user", "assistant", "tool")
        ):
            raise PolicyDenied()
    raw.sort(key=lambda pair: pair[1]["sequence"])
    if len({doc["sequence"] for _, doc in raw}) != len(raw):
        raise PolicyDenied()
    tainted = False
    next_sequence = 1
    for _, doc in raw:
        if doc["sequence"] != next_sequence:
            tainted = True
        next_sequence = doc["sequence"] + 1
        text = reusable_text(doc)
        changed = text != doc.get("text") or doc["version"] != 1
        if text is None or changed:
            tainted = True
        # Derived replies may have consumed any earlier source. Without a complete
        # replacement proof they cannot survive a corrected/suppressed prefix.
        if text is None or (tainted and not direct_user(doc)):
            continue
        if doc["role"] == "tool":
            raise PolicyDenied()
        documents.append({**doc, "text": text})
    messages: list[ModelMessage] = []
    for doc in documents:
        public = view(Message, doc)
        if doc.get("model_messages") is not None:
            from brainoid_agent_core.checkpoints import continuation_messages

            snapshots = tuple(ModelMessage.model_validate(m) for m in doc["model_messages"])
            if (
                not snapshots
                or snapshots[-1].role != "assistant"
                or any(p.type != "text" for p in snapshots[-1].parts)
            ):
                raise PolicyDenied()
            continuation_messages(snapshots[:-1])
        else:
            snapshots = (
                ModelMessage.model_validate(doc["model_message"])
                if doc.get("model_message") is not None
                else ModelMessage(role=public.role, parts=(TextPart(text=public.text),)),
            )
            if snapshots[0].role != public.role or any(
                p.type != "text" for p in snapshots[0].parts
            ):
                raise PolicyDenied()
        if "".join(p.text for m in snapshots for p in m.parts if p.type == "text") != public.text:
            raise PolicyDenied()
        messages.extend(snapshot_message(m) for m in snapshots)
    return documents, tuple(messages), history_hash(documents, tuple(messages))


def history_hash(documents: list[Document], messages: tuple[ModelMessage, ...]) -> str:
    if any(doc.get("version") != 1 or doc.get("suppressed_spans") for doc in documents):
        return fingerprint(
            [
                [
                    [
                        view(Message, doc).model_dump(mode="json"),
                        doc["sequence"],
                        doc["version"],
                        doc.get("suppressed_spans", []),
                    ]
                    for doc in documents
                ],
                [message.model_dump(mode="json") for message in messages],
            ]
        )
    # Preserve RUN-04 receipts for histories with one snapshot per public message.
    if len(documents) == len(messages):
        return fingerprint(
            [
                [
                    view(Message, doc).model_dump(mode="json"),
                    doc["sequence"],
                    message.model_dump(mode="json"),
                ]
                for doc, message in zip(documents, messages, strict=True)
            ]
        )
    return fingerprint(
        [
            [[view(Message, doc).model_dump(mode="json"), doc["sequence"]] for doc in documents],
            [message.model_dump(mode="json") for message in messages],
        ]
    )


def failed_projection(tx: Transaction, document: Document) -> Document:
    """Read-only reconciliation plan; callers write it only after their other reads.

    Infrastructure terminal jobs can precede a handler callback (obsolete, unknown
    outcome, exhausted attempts). Public reads/next admission converge these runs
    without publishing their buffered result or reviving a job.
    """
    if document["status"] not in ("queued", "running", "waiting_confirmation"):
        return document
    tenant, run = ResourceId(document["tenant_id"]), ResourceId(document["run_id"])
    doc = tx.get(f"tenants/{tenant}/jobs/{job_id(tenant, KIND, run)}")
    if doc is not None and doc.get("status") in ("failed", "obsolete"):
        return {
            **document,
            "status": "failed",
            "version": document["version"] + 1,
            "error_code": doc.get("reason")
            if doc.get("reason") in get_args(Reason)
            else "invalid_job",
        }
    return document


class RunState:
    def __init__(
        self,
        ledger: ControlLedger,
        *,
        now: Callable[[], datetime],
        credential_epoch: ResourceId | None,
        allow_untracked_bootstrap: bool = False,
        check_results: Callable[[Transaction, Job, Document, list[Document]], str] | None = None,
        load_summary: Callable[[Transaction, Job, int], ConversationSummary | None] | None = None,
    ) -> None:
        self.ledger, self.now = ledger, now
        self.credential_epoch = credential_epoch
        self.allow_untracked = allow_untracked_bootstrap
        self.check_results = check_results
        self.load_summary = load_summary

    def check(
        self, tx: Transaction, job: Job, *, waiting: bool = False, check_results: bool = True
    ) -> tuple[Document, Document, ContextInput, str]:
        current_job = tx.get(job_path(job.tenant_id, job.id))
        if current_job is None:
            raise PolicyDenied()
        live_job = Job.model_validate(current_job)
        if (
            live_job.status != ("waiting" if waiting else "running")
            or live_job.lease_generation != job.lease_generation
            or (
                not waiting
                and (live_job.lease_expires_at is None or live_job.lease_expires_at <= self.now())
            )
            or live_job.grant != job.grant
            or live_job.output != job.output
        ):
            raise PolicyDenied()
        policy = transaction_policy(
            tx, now=self.now, ledger=self.ledger, allow_untracked_bootstrap=self.allow_untracked
        )
        grant = policy.worker(job.grant, output=job.output)
        try:
            if any(account_stamp(tx, uid) != stamp for uid, stamp in job.account_stamps.items()):
                raise PolicyDenied()
        except AccountUnavailable:
            raise PolicyDenied() from None
        run = tx.get(run_path(job.tenant_id, job.operation_id))
        parent = tx.get(conversation_path(job.tenant_id, job.grant.ref.id))
        if (
            job.kind != KIND
            or job.grant.ref.kind != ResourceKind.CONVERSATION
            or run is None
            or parent is None
            or run.get("tenant_id") != job.tenant_id
            or run.get("run_id") != job.operation_id
            or run.get("conversation_id") != job.grant.ref.id
            or parent.get("active_run_id") != job.operation_id
            or parent.get("history_revision", 0) != run.get("history_revision")
            or run.get("status")
            not in (("waiting_confirmation",) if waiting else ("queued", "running"))
        ):
            raise PolicyDenied()
        try:
            live_run(tx, job.tenant_id, job.operation_id, now=self.now())
        except QuotaExceeded:
            raise PolicyDenied() from None
        view(Run, run)
        family_expiry = None
        if run.get("credential") is not None:
            binding = CredentialBinding.model_validate(run["credential"])
            family = tx.get("device_credential_families/" + binding.family_id)
            if (
                family is None
                or self.credential_epoch is None
                or binding.credential_epoch != self.credential_epoch
                or family.get("epoch") != self.credential_epoch
                or family.get("status") != "active"
                or normalize_utc(self.now()) >= family["expires_at"]
                or any(
                    family.get(field) != getattr(binding, field)
                    for field in (
                        "tenant_id",
                        "device_id",
                        "authorization_version",
                        "connection_generation",
                    )
                )
                or binding.tenant_id != job.tenant_id
                or binding.device_id != grant.context.actor.actor_id
            ):
                raise PolicyDenied()
            family_expiry = family["expires_at"]
        definition = agent(tx, job.tenant_id, parent["agent_id"])
        if definition.model_dump(mode="json") != run["agent"]:
            raise PolicyDenied()
        policy.resource(
            job.tenant_id,
            grant.principal.subject,
            grant.ref,
            action=Action.MODEL_READ,
            output=job.output,
        )
        documents, messages, _ = history(tx, job.tenant_id, job.grant.ref.id)
        if (
            not documents
            or documents[-1]["id"] != run["input_message_id"]
            or documents[-1]["run_id"] != job.operation_id
            or documents[-1]["role"] != "user"
            or documents[-1]["text"] != run["input_text"]
            or documents[-1]["sequence"] != run["input_sequence"]
            or parent.get("next_sequence") != run["input_sequence"] + 1
            or history_hash(documents[:-1], messages[:-1]) != run["history_hash"]
        ):
            raise PolicyDenied()
        reuse_receipt = None
        if not waiting and check_results:
            if self.check_results is not None:
                reuse_receipt = self.check_results(tx, job, run, documents)
            elif run.get("continuation") or any(d.get("model_messages") for d in documents):
                raise PolicyDenied()
        scopes = tuple(Scope.model_validate(s) for s in run["memory_scopes"])
        if scopes:
            search = policy.search(
                job.tenant_id, grant.principal.subject, ResourceKind.MEMORY, output=job.output
            )
            for scope in scopes:
                if (
                    grant.scope.visibility == Visibility.SHARED
                    and scope.visibility != Visibility.SHARED
                ):
                    raise PolicyDenied()
                display = {
                    DisplayPolicy.NEVER: 0,
                    DisplayPolicy.AUTHENTICATED_SESSION: 1,
                    DisplayPolicy.SHARED_DISPLAY: 2,
                }
                if display[grant.scope.display_policy] > display[scope.display_policy]:
                    raise PolicyDenied()
                if not any(
                    f.visibility == scope.visibility
                    and scope.display_policy in f.display_policies
                    and (f.owner_user_id is None or f.owner_user_id == grant.principal.user_id)
                    for f in search.filters
                ):
                    raise PolicyDenied()
        summary = None
        context_messages = messages[:-1]
        if not waiting and check_results and self.load_summary is not None:
            summary = self.load_summary(tx, job, run["input_sequence"])
            if summary is not None:
                included = {source.resource_id for source in summary.source_refs}
                remaining: list[ModelMessage] = []
                position = 0
                for document in documents[:-1]:
                    count = (
                        len(document["model_messages"])
                        if document.get("model_messages") is not None
                        else 1
                    )
                    if document["id"] not in included:
                        remaining.extend(context_messages[position : position + count])
                    position += count
                context_messages = tuple(remaining)
        request = ContextInput(
            context=job.grant.context,
            agent=definition,
            expected_agent_version=run["agent"]["version"],
            conversation=ConversationContext(
                id=grant.ref.id,
                tenant_id=job.tenant_id,
                owner_user_id=grant.owner_user_id,
                scope=grant.scope,
                version=grant.resource.version,
                deletion_epoch=grant.resource.deletion_epoch,
                messages=context_messages,
                summary=summary,
            ),
            user_text=run["input_text"],
            memory_scopes=scopes,
        )
        receipt = fingerprint(
            [
                job.id,
                grant.principal.stamp,
                grant.output_stamp,
                run["history_hash"],
                reuse_receipt,
                run["agent"],
                run["input_message_id"],
                grant.resource.model_dump(mode="json"),
                summary.model_dump(mode="json") if summary is not None else None,
            ]
        )
        deadlines = [
            value for value in (live_job.lease_expires_at, family_expiry) if value is not None
        ]
        if parent.get("expires_at") is not None:
            deadlines.append(TypeAdapter(UtcTimestamp).validate_python(parent["expires_at"]))
        principals = [grant.principal]
        if job.output is not None:
            principals.append(policy.principal(job.tenant_id, job.output))
        for principal in principals:
            actor = principal.context.actor
            if isinstance(actor, DeviceActorContext) and actor.private_session is not None:
                deadlines.extend(
                    (actor.private_session.expires_at, actor.private_session.idle_expires_at)
                )

        def finish() -> None:
            now = normalize_utc(self.now())
            if now < live_job.created_at or any(now >= deadline for deadline in deadlines):
                raise PolicyDenied()

        protect(tx, finish)
        return run, parent, request, receipt
