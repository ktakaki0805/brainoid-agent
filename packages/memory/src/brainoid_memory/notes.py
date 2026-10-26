"""Canonical note service for HTTP and tools; no HTTP or provider dependencies."""

from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Self
from uuid import uuid4

from brainoid_contracts import (
    DeviceActorContext,
    DisplayPolicy,
    FirebaseUid,
    ResourceId,
    Scope,
    UtcTimestamp,
    Version,
    Visibility,
)
from brainoid_contracts.access import AuthorizedScope, fingerprint
from brainoid_contracts.deletion import fenced_document, tombstone_path
from brainoid_contracts.errors import APIError, ErrorCode
from brainoid_contracts.guards import GuardedTransactions
from brainoid_contracts.idempotency import Idempotency, MutationResult
from brainoid_contracts.transactions import Transaction, TransactionStore
from brainoid_policy.accounts import account_stamp
from brainoid_policy.models import Action, ResourceKind, ResourceRef
from brainoid_policy.scopes import PolicyDenied, PolicyUnavailable, ResourceGrant
from pydantic import Field, ValidationError, model_validator

from brainoid_memory.models import MemoryValue, StoredMetadata
from brainoid_memory.mutations import MutationScope
from brainoid_memory.repository import FactTransaction, VersionConflict

Title = Annotated[str, Field(strict=True, min_length=1, max_length=200)]
Body = Annotated[str, Field(strict=True, max_length=8000)]


class NoteCreate(Scope):
    title: Title
    body: Body


class NotePatch(MemoryValue):
    title: Title | None = None
    body: Body | None = None
    display_policy: DisplayPolicy | None = None

    @model_validator(mode="after")
    def nonempty(self) -> Self:
        if not self.model_fields_set or any(
            getattr(self, key) is None for key in self.model_fields_set
        ):
            raise ValueError("Nonempty changes without null required")
        return self


class Note(NoteCreate, StoredMetadata):
    # Direct explicit user text, never an unproved model-derived source.
    source_refs: tuple[()] = ()
    updated_by: FirebaseUid
    updated_via_device_id: ResourceId | None = None

    @model_validator(mode="after")
    def owner(self) -> Self:
        if (self.visibility == Visibility.PRIVATE) != (self.owner_user_id is not None):
            raise ValueError("Invalid note ownership")
        if self.updated_at < self.created_at:
            raise ValueError("Invalid update time")
        return self


class NoteView(NoteCreate):
    id: ResourceId
    tenant_id: ResourceId
    version: Version
    created_at: UtcTimestamp
    updated_at: UtcTimestamp
    owner_user_id: FirebaseUid | None = Field(default=None, exclude_if=lambda value: value is None)


class NotePage(MemoryValue):
    items: Annotated[list[NoteView], Field(max_length=100)]
    next_cursor: Annotated[str, Field(max_length=2048)] | None = None


def note_path(tenant: ResourceId, identity: ResourceId) -> str:
    return f"tenants/{tenant}/notes/{identity}"


def effect_path(tenant: ResourceId, effect: ResourceId) -> str:
    return f"tenants/{tenant}/note_effects/{effect}"


def view(record: Note) -> NoteView:
    return NoteView.model_validate({key: getattr(record, key) for key in NoteView.model_fields})


class Notes:
    def __init__(self, store: TransactionStore, *, now: Callable[[], datetime]) -> None:
        self.store = store if isinstance(store, GuardedTransactions) else GuardedTransactions(store)
        self.now = now
        self.idempotency = Idempotency(self.store, now=now)

    @staticmethod
    def scopes(access: FactTransaction) -> tuple[Scope, ...]:
        allowed = access._authority.policy.search(
            access.principal.context.tenant_id,
            access.caller.subject,
            ResourceKind.NOTE,
            output=access._repo._output,
        )
        access.finish()
        return tuple(
            Scope(visibility=f.visibility, display_policy=d)
            for f in allowed.filters
            for d in sorted(f.display_policies)
            if f.visibility != Visibility.PRIVATE or d != DisplayPolicy.SHARED_DISPLAY
        )

    @staticmethod
    def get(
        access: FactTransaction,
        identity: ResourceId,
        *,
        write: bool = False,
        for_model: bool = False,
    ) -> tuple[Note, ResourceGrant]:
        tenant, tx = access.principal.context.tenant_id, access._tx
        grant = access._authority.policy.resource(
            tenant,
            access.caller.subject,
            ResourceRef(tenant_id=tenant, kind=ResourceKind.NOTE, id=identity),
            action=Action.WRITE if write else Action.MODEL_READ if for_model else Action.READ,
            output=access._repo._output,
        )
        document = tx.get(note_path(tenant, identity))
        if document is None:
            raise PolicyDenied()
        try:
            record = Note.model_validate(fenced_document(tx, tenant, "notes", identity, document))
        except ValidationError:
            raise PolicyDenied() from None
        if (
            record.id != identity
            or record.tenant_id != tenant
            or record.deleted
            or record.deletion_epoch
        ):
            raise PolicyDenied()
        if for_model and record.suppress_reuse:
            raise PolicyDenied()
        access.finish()
        return record, grant

    @classmethod
    def candidates(
        cls, access: FactTransaction, *, query: str = "", for_model: bool = False
    ) -> list[Note]:
        cls.scopes(access)
        tenant = access.principal.context.tenant_id
        records = []
        for path, doc in access._tx.collection(f"tenants/{tenant}/notes"):
            try:
                identity = ResourceId(doc["id"])
                if path != note_path(tenant, identity):
                    continue
                note, _ = cls.get(access, identity, for_model=for_model)
            except PolicyUnavailable:
                raise
            except (PolicyDenied, ValueError, KeyError):
                continue
            if query.casefold() in (note.title + "\n" + note.body).casefold():
                records.append(note)
        access.finish()
        return sorted(records, key=lambda r: r.id)

    @staticmethod
    def scope(tx: Transaction, access: FactTransaction, scope: Scope) -> MutationScope:
        principal = access.principal
        if principal.user_id is None or "notes" not in principal.context.actor.allowed_scopes:
            raise PolicyDenied(hidden=False)
        if scope not in Notes.scopes(access):
            raise PolicyDenied()
        return MutationScope(
            context=principal.context,
            scope=scope,
            owner_user_id=principal.user_id,
            authorization_stamp=fingerprint(
                [principal.stamp, account_stamp(tx, principal.user_id)]
            ),
            valid_from=access.caller.valid_from,
            valid_to=access.caller.valid_to,
        )

    def mutate(
        self,
        open_access: Callable[[Transaction], FactTransaction],
        body: NoteCreate | NotePatch,
        key: str | None,
        *,
        identity: ResourceId | None = None,
        expected: Version | None = None,
        effect: ResourceId | None = None,
        extra: Callable[[Transaction, Note], Callable[[], None]] | None = None,
    ) -> Note:
        body = type(body).model_validate(body.model_dump(exclude_unset=True))
        creating = isinstance(body, NoteCreate)
        if creating != (identity is None) or (not creating and expected is None):
            raise ValueError("Invalid note mutation")
        target = identity or effect or ResourceId(str(uuid4()))
        access: FactTransaction | None = None
        old: Note | None = None

        def authorize(tx: Transaction, replay: MutationResult | None) -> AuthorizedScope:
            nonlocal access, old
            access = open_access(tx)
            record = None
            if identity is not None:
                old, _ = self.get(access, identity, write=True)
                record = old
            if replay is not None:
                if replay.resource_id is None:
                    raise PolicyDenied()
                record, _ = self.get(access, replay.resource_id, write=True)
            scope = (
                Scope(visibility=body.visibility, display_policy=body.display_policy)
                if isinstance(body, NoteCreate)
                else Scope(visibility=record.visibility, display_policy=record.display_policy)
                if record
                else None
            )
            assert scope is not None
            # Request scope binds replays even if this operation changes display_policy.
            if isinstance(body, NotePatch) and body.display_policy is not None:
                try:
                    scope = Scope(visibility=scope.visibility, display_policy=body.display_policy)
                except ValidationError:
                    raise APIError(ErrorCode.INVALID_REQUEST) from None
            return self.scope(tx, access, scope)

        def mutate(tx: Transaction, current: AuthorizedScope) -> MutationResult:
            assert access is not None
            tenant = current.context.tenant_id
            actor = current.context.actor
            uid = access.principal.user_id
            assert uid is not None
            device = actor.actor_id if isinstance(actor, DeviceActorContext) else None
            if (
                tx.get(tombstone_path(tenant, "notes", target)) is not None
                or tx.get(f"resource_deletion_fences/{fingerprint(note_path(tenant, target))}")
                is not None
            ):
                raise PolicyDenied()
            lock = tx.get(
                f"tenants/{tenant}/source_forget_reservations/{fingerprint(note_path(tenant, target))}"
            )
            if lock is not None:
                raise VersionConflict()
            if creating:
                if tx.get(note_path(tenant, target)) is not None:
                    raise APIError(ErrorCode.CONFLICT)
                record = Note(
                    **body.model_dump(),
                    id=target,
                    tenant_id=tenant,
                    version=1,
                    owner_user_id=uid if current.scope.visibility == Visibility.PRIVATE else None,
                    created_by=uid,
                    created_via_device_id=device,
                    updated_by=uid,
                    updated_via_device_id=device,
                    schema_version=1,
                    deletion_epoch=0,
                    deleted=False,
                    suppress_reuse=False,
                    created_at=self.now(),
                    updated_at=self.now(),
                )
            else:
                assert old is not None
                if old.version != expected:
                    raise VersionConflict()
                record = Note.model_validate(
                    {
                        **old.model_dump(),
                        **body.model_dump(exclude_unset=True),
                        "version": old.version + 1,
                        "updated_at": self.now(),
                        "updated_by": uid,
                        "updated_via_device_id": device,
                    }
                )
            # Creation and edited display policy must be readable at the current output.
            if Scope(
                visibility=record.visibility, display_policy=record.display_policy
            ) not in self.scopes(access):
                raise PolicyDenied()
            proof = None
            if effect is not None:
                if tx.get(effect_path(tenant, effect)) is not None:
                    raise APIError(ErrorCode.CONFLICT)
                proof = {
                    "effect_id": effect,
                    "target_id": target,
                    "request": body.model_dump(mode="json", exclude_unset=True),
                    "expected_version": expected,
                    "record": record.model_dump(mode="json"),
                }
            save_extra = extra(tx, record) if extra is not None else None
            access.finish()
            tx.set(note_path(tenant, target), record.model_dump(mode="json"))
            if proof is not None:
                assert effect is not None
                tx.create(effect_path(tenant, effect), proof)
            if save_extra is not None:
                save_extra()
            return MutationResult(
                status_code=201 if creating else 200, resource_id=target, version=record.version
            )

        result = self.idempotency.execute(
            operation="notes.create" if creating else f"notes.update:{target}",
            key=key,
            body={
                "body": body.model_dump(mode="json", exclude_unset=True),
                "expected_version": expected,
            },
            authorize=authorize,
            mutate=mutate,
        )
        assert access is not None and result.resource_id is not None
        access.finish()

        def read(tx: Transaction) -> tuple[Note, FactTransaction]:
            current = open_access(tx)
            record, _ = self.get(current, result.resource_id)  # type: ignore[arg-type]
            return record, current

        record, access = self.store.run(read)
        access.finish()
        return record

    @classmethod
    def proof(
        cls,
        access: FactTransaction,
        effect: ResourceId,
        body: NoteCreate | NotePatch,
        identity: ResourceId | None,
        expected: Version | None,
        *,
        write: bool = True,
    ) -> Note:
        proof = access._tx.get(effect_path(access.principal.context.tenant_id, effect))
        if (
            proof is None
            or proof.get("effect_id") != effect
            or proof.get("request") != body.model_dump(mode="json", exclude_unset=True)
            or proof.get("expected_version") != expected
            or proof.get("target_id") != (identity or effect)
        ):
            raise PolicyDenied()
        record, _ = cls.get(access, identity or effect, write=write, for_model=True)
        if proof.get("record") != record.model_dump(mode="json"):
            raise PolicyDenied()
        return record
