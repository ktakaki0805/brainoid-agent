"""Transactional writes shared only by explicit notes and tasks."""

from collections.abc import Callable
from datetime import datetime
from typing import Self
from uuid import uuid4

from brainoid_contracts import (
    DeviceActorContext,
    DisplayPolicy,
    FirebaseUid,
    ResourceId,
    Scope,
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
from pydantic import ValidationError, model_validator

from brainoid_memory.models import MemoryValue, StoredMetadata
from brainoid_memory.mutations import MutationScope
from brainoid_memory.repository import FactTransaction, VersionConflict


class DirectRecord(Scope, StoredMetadata):
    title: str
    updated_by: FirebaseUid
    updated_via_device_id: ResourceId | None = None

    @model_validator(mode="after")
    def owner(self) -> Self:
        if (self.visibility == Visibility.PRIVATE) != (self.owner_user_id is not None):
            raise ValueError("Invalid resource ownership")
        if self.updated_at < self.created_at:
            raise ValueError("Invalid update time")
        return self


class DirectResources[Create: Scope, Patch: MemoryValue, Record: DirectRecord]:
    kind: ResourceKind
    create_type: type[Create]
    patch_type: type[Patch]
    record_type: type[Record]

    @classmethod
    def path(cls, tenant: ResourceId, identity: ResourceId) -> str:
        return f"tenants/{tenant}/{cls.kind}/{identity}"

    @classmethod
    def effect_path(cls, tenant: ResourceId, effect: ResourceId) -> str:
        return f"tenants/{tenant}/{str(cls.kind)[:-1]}_effects/{effect}"

    @classmethod
    def validate_body(cls, access: FactTransaction, body: Create | Patch) -> None:
        pass

    @staticmethod
    def prepare_write(tx: Transaction, old: Record | None, record: Record) -> Callable[[], None]:
        return lambda: None

    @staticmethod
    def text(record: Record) -> str:
        return record.title

    def __init__(self, store: TransactionStore, *, now: Callable[[], datetime]) -> None:
        self.store = store if isinstance(store, GuardedTransactions) else GuardedTransactions(store)
        self.now = now
        self.idempotency = Idempotency(self.store, now=now)

    @classmethod
    def scopes(cls, access: FactTransaction) -> tuple[Scope, ...]:
        allowed = access._authority.policy.search(
            access.principal.context.tenant_id,
            access.caller.subject,
            cls.kind,
            output=access._repo._output,
        )
        access.finish()
        return tuple(
            Scope(visibility=f.visibility, display_policy=d)
            for f in allowed.filters
            for d in sorted(f.display_policies)
            if f.visibility != Visibility.PRIVATE or d != DisplayPolicy.SHARED_DISPLAY
        )

    @classmethod
    def get(
        cls,
        access: FactTransaction,
        identity: ResourceId,
        *,
        write: bool = False,
        for_model: bool = False,
    ) -> tuple[Record, ResourceGrant]:
        tenant, tx = access.principal.context.tenant_id, access._tx
        grant = access._authority.policy.resource(
            tenant,
            access.caller.subject,
            ResourceRef(tenant_id=tenant, kind=cls.kind, id=identity),
            action=Action.WRITE if write else Action.MODEL_READ if for_model else Action.READ,
            output=access._repo._output,
        )
        document = tx.get(cls.path(tenant, identity))
        if document is None:
            raise PolicyDenied()
        try:
            record = cls.record_type.model_validate(
                fenced_document(tx, tenant, str(cls.kind), identity, document)
            )
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
    ) -> list[Record]:
        cls.scopes(access)
        tenant = access.principal.context.tenant_id
        records = []
        for path, doc in access._tx.collection(f"tenants/{tenant}/{cls.kind}"):
            try:
                identity = ResourceId(doc["id"])
                if path != cls.path(tenant, identity):
                    continue
                note, _ = cls.get(access, identity, for_model=for_model)
            except PolicyUnavailable:
                raise
            except (PolicyDenied, ValueError, KeyError):
                continue
            if query.casefold() in cls.text(note).casefold():
                records.append(note)
        access.finish()
        return sorted(records, key=lambda r: r.id)

    @classmethod
    def scope(cls, tx: Transaction, access: FactTransaction, scope: Scope) -> MutationScope:
        principal = access.principal
        if principal.user_id is None or str(cls.kind) not in principal.context.actor.allowed_scopes:
            raise PolicyDenied(hidden=False)
        if scope not in cls.scopes(access):
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
        body: Create | Patch,
        key: str | None,
        *,
        identity: ResourceId | None = None,
        expected: Version | None = None,
        effect: ResourceId | None = None,
        extra: Callable[[Transaction, Record], Callable[[], None]] | None = None,
    ) -> Record:
        body = type(body).model_validate(body.model_dump(exclude_unset=True))
        creating = isinstance(body, self.create_type)
        if creating != (identity is None) or (not creating and expected is None):
            raise ValueError("Invalid note mutation")
        target = identity or effect or ResourceId(str(uuid4()))
        access: FactTransaction | None = None
        old: Record | None = None

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
                if isinstance(body, self.create_type)
                else Scope(visibility=record.visibility, display_policy=record.display_policy)
                if record
                else None
            )
            assert scope is not None
            # Request scope binds replays even if this operation changes display_policy.
            new_policy = getattr(body, "display_policy", None)
            if isinstance(body, self.patch_type) and new_policy is not None:
                try:
                    scope = Scope(visibility=scope.visibility, display_policy=new_policy)
                except ValidationError:
                    raise APIError(ErrorCode.INVALID_REQUEST) from None
            self.validate_body(access, body)
            return self.scope(tx, access, scope)

        def mutate(tx: Transaction, current: AuthorizedScope) -> MutationResult:
            assert access is not None
            tenant = current.context.tenant_id
            actor = current.context.actor
            uid = access.principal.user_id
            assert uid is not None
            device = actor.actor_id if isinstance(actor, DeviceActorContext) else None
            if (
                tx.get(tombstone_path(tenant, str(self.kind), target)) is not None
                or tx.get(f"resource_deletion_fences/{fingerprint(self.path(tenant, target))}")
                is not None
            ):
                raise PolicyDenied()
            lock = tx.get(
                f"tenants/{tenant}/source_forget_reservations/{fingerprint(self.path(tenant, target))}"
            )
            if lock is not None:
                raise VersionConflict()
            if creating:
                if tx.get(self.path(tenant, target)) is not None:
                    raise APIError(ErrorCode.CONFLICT)
                record = self.record_type(
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
                record = self.record_type.model_validate(
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
                if tx.get(self.effect_path(tenant, effect)) is not None:
                    raise APIError(ErrorCode.CONFLICT)
                proof = {
                    "effect_id": effect,
                    "target_id": target,
                    "request": body.model_dump(mode="json", exclude_unset=True),
                    "expected_version": expected,
                    "record": record.model_dump(mode="json"),
                }
            save_domain = self.prepare_write(tx, old, record)
            save_extra = extra(tx, record) if extra is not None else None
            access.finish()
            tx.set(self.path(tenant, target), record.model_dump(mode="json"))
            if proof is not None:
                assert effect is not None
                tx.create(self.effect_path(tenant, effect), proof)
            save_domain()
            if save_extra is not None:
                save_extra()
            return MutationResult(
                status_code=201 if creating else 200, resource_id=target, version=record.version
            )

        result = self.idempotency.execute(
            operation=f"{self.kind}.create" if creating else f"{self.kind}.update:{target}",
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

        def read(tx: Transaction) -> tuple[Record, FactTransaction]:
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
        body: Create | Patch,
        identity: ResourceId | None,
        expected: Version | None,
        *,
        write: bool = True,
    ) -> Record:
        proof = access._tx.get(cls.effect_path(access.principal.context.tenant_id, effect))
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
