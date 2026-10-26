"""Canonical parent/child reads shared by API admission and the worker.

Conversation version is the settings/completed-history fence. One active run owns
the append slot; accepting its user message does not replace the saved source grant.
Every child is checked against that parent, its version and its immutable content.
"""

from collections.abc import Callable
from datetime import datetime
from typing import get_args

from brainoid_agent_core.context import ContextInput, ConversationContext, snapshot_message
from brainoid_agent_core.definition import AgentDefinition
from brainoid_agent_core.types import ModelMessage, TextPart
from brainoid_contracts import ResourceId, Scope, Visibility, normalize_utc
from brainoid_contracts.transactions import Document, Transaction
from brainoid_jobs import Job
from brainoid_jobs.models import Reason, job_id
from brainoid_jobs.repository import path as job_path
from brainoid_policy.accounts import AccountUnavailable, account_stamp, fingerprint
from brainoid_policy.control_ledger import ControlLedger
from brainoid_policy.models import Action, ResourceKind
from brainoid_policy.scopes import PolicyDenied
from brainoid_policy.transactions import transaction_policy
from pydantic import BaseModel

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
    base = conversation_path(tenant, conversation)
    documents = []
    for path, doc in tx.collection(base + "/messages"):
        public_message = view(Message, doc)
        if (
            path != base + "/messages/" + public_message.id
            or doc.get("tenant_id") != tenant
            or doc.get("conversation_id") != conversation
            or doc.get("version") != 1
            or doc.get("deletion_epoch") != 0
            or doc.get("deleted") is not False
            or doc.get("suppress_reuse") is not False
            or type(doc.get("sequence")) is not int
            or doc["sequence"] < 1
            or public_message.role not in ("user", "assistant")
        ):
            raise PolicyDenied()
        documents.append(doc)
    documents.sort(key=lambda d: d["sequence"])
    if len({d["sequence"] for d in documents}) != len(documents):
        raise PolicyDenied()
    messages = []
    for doc in documents:
        public = view(Message, doc)
        message = (
            ModelMessage.model_validate(doc["model_message"])
            if doc.get("model_message") is not None
            else ModelMessage(role=public.role, parts=(TextPart(text=public.text),))
        )
        if (
            message.role != public.role
            or any(p.type != "text" for p in message.parts)
            or "".join(p.text for p in message.parts if p.type == "text") != public.text
        ):
            raise PolicyDenied()
        messages.append(snapshot_message(message))
    return documents, tuple(messages), history_hash(documents, tuple(messages))


def history_hash(documents: list[Document], messages: tuple[ModelMessage, ...]) -> str:
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


def failed_projection(tx: Transaction, document: Document) -> Document:
    """Read-only reconciliation plan; callers write it only after their other reads.

    Infrastructure terminal jobs can precede a handler callback (obsolete, unknown
    outcome, exhausted attempts). Public reads/next admission converge these runs
    without publishing their buffered result or reviving a job.
    """
    if document["status"] not in ("queued", "running"):
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
    ) -> None:
        self.ledger, self.now = ledger, now
        self.credential_epoch = credential_epoch
        self.allow_untracked = allow_untracked_bootstrap

    def check(self, tx: Transaction, job: Job) -> tuple[Document, Document, ContextInput, str]:
        current_job = tx.get(job_path(job.tenant_id, job.id))
        if current_job is None:
            raise PolicyDenied()
        live_job = Job.model_validate(current_job)
        if (
            live_job.status != "running"
            or live_job.lease_generation != job.lease_generation
            or live_job.lease_expires_at is None
            or live_job.lease_expires_at <= self.now()
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
            or run.get("status") not in ("queued", "running")
        ):
            raise PolicyDenied()
        view(Run, run)
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
                if not any(
                    f.visibility == scope.visibility
                    and scope.display_policy in f.display_policies
                    and (f.owner_user_id is None or f.owner_user_id == grant.principal.user_id)
                    for f in search.filters
                ):
                    raise PolicyDenied()
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
                messages=messages[:-1],
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
                run["agent"],
                run["input_message_id"],
                grant.resource.model_dump(mode="json"),
            ]
        )
        return run, parent, request, receipt
