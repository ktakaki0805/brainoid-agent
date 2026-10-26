"""Direct-user conversation summaries with exact canonical prefix provenance.

Assistant/tool/derived user text is deliberately unsupported: its dependencies must
not be laundered into a summary. The Fact SourceResolver contract is unchanged.
"""

from dataclasses import dataclass
from typing import Annotated, Literal
from uuid import UUID, uuid5

from brainoid_contracts import FirebaseUid, ResourceId, Scope, UtcTimestamp, Version, normalize_utc
from brainoid_contracts.guards import protect
from brainoid_contracts.reuse import direct_user, reusable_text
from brainoid_contracts.transactions import Document
from brainoid_policy.accounts import fingerprint
from brainoid_policy.models import Action, ResourceKind
from brainoid_policy.scopes import PolicyDenied, PolicyUnavailable, ResourceGrant
from pydantic import Field, TypeAdapter, ValidationError

from brainoid_memory.models import Epoch, MemoryValue
from brainoid_memory.repository import _MemoryTransaction, _path


class MessageSource(MemoryValue):
    resource_id: ResourceId
    version: Version
    deletion_epoch: Epoch
    digest: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    sequence: Annotated[int, Field(strict=True, ge=1)]
    included: bool


class SummarySources(MemoryValue):
    conversation_id: ResourceId
    conversation_version: Version
    deletion_epoch: Epoch
    owner_user_id: FirebaseUid | None
    scope: Scope
    # No independent retention policy. Missing expiry means the parent's lifetime.
    expires_at: UtcTimestamp | None
    messages: Annotated[tuple[MessageSource, ...], Field(min_length=1, max_length=100)]

    @property
    def cutoff(self) -> int:
        return max(m.sequence for m in self.messages)


class Summary(MemoryValue):
    id: ResourceId
    tenant_id: ResourceId
    kind: Literal["direct_user_summary"] = "direct_user_summary"
    version: Literal[1] = 1
    sources: SummarySources
    created_at: UtcTimestamp
    text: Annotated[str, Field(strict=True, min_length=1, max_length=8000, repr=False)]


@dataclass(frozen=True)
class SummaryInput:
    sources: SummarySources
    texts: tuple[str, ...]
    grant: ResourceGrant


def summary_path(tenant: ResourceId, conversation: ResourceId, identity: ResourceId) -> str:
    return f"tenants/{tenant}/conversations/{conversation}/summaries/{identity}"


def summary_id(sources: SummarySources) -> ResourceId:
    return ResourceId(
        str(uuid5(UUID(sources.conversation_id), fingerprint(sources.model_dump(mode="json"))))
    )


class SummaryTransaction(_MemoryTransaction):
    def capture(self, conversation: ResourceId, *, cutoff: int | None = None) -> SummaryInput:
        ref = self._repo._ref(self._context, conversation, ResourceKind.CONVERSATION)
        grant = self._authority.policy.resource(
            self._context.tenant_id,
            self._authority.subject,
            ref,
            action=Action.MODEL_READ,
            output=self._repo._output,
        )
        parent = self._tx.get(_path(ref))
        if parent is None or grant.resource.deletion_epoch != 0:
            raise PolicyDenied()
        expires = parent.get("expires_at")
        if expires is not None:
            expires = TypeAdapter(UtcTimestamp).validate_python(expires)
            protect(self._tx, lambda: self._deadline(expires))
        # Output session validity must survive DB response / provider handoff too.
        if self._repo._output is not None:
            destination = self._authority.policy.principal(ref.tenant_id, self._repo._output)
            session = getattr(destination.context.actor, "private_session", None)
            if session is not None:
                deadline = min(session.expires_at, session.idle_expires_at)
                protect(self._tx, lambda: self._deadline(deadline))
        records = []
        for location, doc in self._tx.collection(_path(ref) + "/messages"):
            sequence = doc.get("sequence")
            if type(sequence) is not int or sequence < 1:
                raise PolicyDenied()
            if cutoff is not None and sequence > cutoff:
                continue
            identity = TypeAdapter(ResourceId).validate_python(doc.get("id"))
            if (
                location != _path(ref) + "/messages/" + identity
                or doc.get("tenant_id") != ref.tenant_id
                or doc.get("conversation_id") != ref.id
            ):
                raise PolicyDenied()
            records.append(doc)
        records.sort(key=lambda d: d["sequence"])
        if not 1 <= len(records) <= 100 or len({d["sequence"] for d in records}) != len(records):
            raise PolicyDenied()
        sources, texts = [], []
        for doc in records:
            text = reusable_text(doc) if direct_user(doc) else None
            if text:
                texts.append(text)
            sources.append(
                MessageSource(
                    resource_id=doc["id"],
                    version=doc["version"],
                    deletion_epoch=doc["deletion_epoch"],
                    digest=fingerprint(TypeAdapter(Document).dump_python(doc, mode="json")),
                    sequence=doc["sequence"],
                    included=bool(text),
                )
            )
        if not texts or sum(len(text) for text in texts) > 64000:
            raise PolicyDenied()
        self.finish()
        return SummaryInput(
            SummarySources(
                conversation_id=ref.id,
                conversation_version=grant.resource.version,
                deletion_epoch=grant.resource.deletion_epoch,
                owner_user_id=grant.owner_user_id,
                scope=grant.scope,
                expires_at=expires,
                messages=tuple(sources),
            ),
            tuple(texts),
            grant,
        )

    def _deadline(self, deadline: UtcTimestamp) -> None:
        self.finish()
        if normalize_utc(self._repo._now()) >= deadline:
            raise PolicyDenied()

    def current(self, sources: SummarySources, *, generating: bool = False) -> SummaryInput:
        current = self.capture(sources.conversation_id, cutoff=sources.cutoff)
        expected = current.sources
        if (
            expected.conversation_version < sources.conversation_version
            or (generating and expected.conversation_version != sources.conversation_version)
            or expected.model_copy(update={"conversation_version": sources.conversation_version})
            != sources
        ):
            raise PolicyDenied()
        return current

    def read(self, conversation: ResourceId, identity: ResourceId) -> Summary:
        raw = self._tx.get(summary_path(self._context.tenant_id, conversation, identity))
        record = Summary.model_validate(raw)
        if (
            record.id != identity
            or record.tenant_id != self._context.tenant_id
            or record.sources.conversation_id != conversation
            or summary_id(record.sources) != identity
        ):
            raise PolicyDenied()
        self.current(record.sources)
        self.finish()
        return record

    def latest(self, conversation: ResourceId, *, before: int) -> Summary | None:
        # Parent authority precedes even inspecting private summary candidates.
        ref = self._repo._ref(self._context, conversation, ResourceKind.CONVERSATION)
        self._authority.policy.resource(
            ref.tenant_id,
            self._authority.subject,
            ref,
            action=Action.MODEL_READ,
            output=self._repo._output,
        )
        results = []
        for location, raw in self._tx.collection(_path(ref) + "/summaries"):
            try:
                record = Summary.model_validate(raw)
                if location != summary_path(ref.tenant_id, conversation, record.id):
                    continue
                if record.sources.cutoff >= before:
                    continue
                results.append(self.read(conversation, record.id))
            except PolicyUnavailable:
                raise
            except (PolicyDenied, ValidationError):
                continue
        self.finish()
        return max(results, key=lambda r: (r.sources.cutoff, r.created_at, r.id), default=None)

    def save(self, record: Summary) -> None:
        # All canonical reads, including absence, precede writes/checkpoint commit.
        record = Summary.model_validate(record.model_dump())
        if record.tenant_id != self._context.tenant_id or record.id != summary_id(record.sources):
            raise PolicyDenied()
        self.current(record.sources, generating=True)
        path = summary_path(record.tenant_id, record.sources.conversation_id, record.id)
        existing = self._tx.get(path)
        if existing is not None and Summary.model_validate(existing) != record:
            raise PolicyDenied()
        self.finish()
        self._tx.set(path, record.model_dump(mode="json"))
