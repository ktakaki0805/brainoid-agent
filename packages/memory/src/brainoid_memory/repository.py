"""Tenant-bound graph persistence over the shared Firestore transaction adapter."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol, Self, cast

from brainoid_contracts import (
    DeviceActorContext,
    DisplayPolicy,
    FirebaseUid,
    ResourceId,
    Scope,
    TenantContext,
    UserActorContext,
    UtcTimestamp,
    Version,
    Visibility,
    normalize_utc,
)
from brainoid_contracts.transactions import Transaction, TransactionStore
from brainoid_policy.accounts import AccountUnavailable, account_stamp
from brainoid_policy.control_ledger import ControlLedger
from brainoid_policy.evaluator import Evaluator
from brainoid_policy.models import (
    Action,
    DeviceSubject,
    ResourceKind,
    ResourceRef,
    ResourceState,
    Subject,
    UserSubject,
)
from brainoid_policy.scopes import PolicyDenied, Principal
from brainoid_policy.transactions import TransactionPolicyState, resource_state
from pydantic import BaseModel, TypeAdapter, ValidationError, model_validator

from brainoid_memory.models import (
    Entity,
    EntityInput,
    EntityPatch,
    Epoch,
    Fact,
    FactInput,
    MemoryValue,
)
from brainoid_memory.sources import ResolvedSource, SourceResolver


class VersionConflict(Exception):
    def __init__(self) -> None:
        super().__init__("version_conflict")


class AlreadyExists(Exception):
    def __init__(self) -> None:
        super().__init__("resource_already_exists")


class VerifiedCaller(MemoryValue):
    """Current identity and the intersection of its verified credential/delegation times.

    Issued by the trusted authorizer for this transaction, never from TenantContext.
    This value is neither a transferable grant nor evidence of resource permission.
    """

    subject: Subject
    valid_from: UtcTimestamp
    valid_to: UtcTimestamp

    @model_validator(mode="after")
    def interval(self) -> Self:
        if self.valid_to <= self.valid_from:
            raise ValueError("Caller validity must have a nonempty interval")
        return self


class CurrentAuthorizer(Protocol):
    """Bind a verified caller to current credentials inside the protected transaction.

    This server-configured adapter must retain the authenticated identity independently
    of context, recheck credential/account revocation and return VerifiedCaller.
    The interval intersects every verified token's issuance/expiry and any current
    delegation deadline. Devices also require current family/generation checks; any
    family deadline must bound valid_to. Workers resolve the initiator from the
    canonical authorized job and intersect their verified credentials, job/lease
    deadline and any still-required initiator credential/delegation bindings.
    A missing or unprovable validity bound fails closed; never invent a new lifetime.
    Never implement this by merely copying the request's TenantContext actor.
    The repository separately checks current tenant/membership/session/resource state.
    """

    def authorize(self, tx: Transaction, context: TenantContext) -> VerifiedCaller: ...


@dataclass(frozen=True)
class _Authority:
    policy: Evaluator
    caller: VerifiedCaller
    principal: Principal

    @property
    def subject(self) -> Subject:
        return self.caller.subject

    def creator(self) -> FirebaseUid:
        if self.principal.user_id is None:
            raise PolicyDenied(hidden=False)
        return self.principal.user_id

    @property
    def device_id(self) -> ResourceId | None:
        return self.subject.device_id if isinstance(self.subject, DeviceSubject) else None


class _GraphPolicyState(TransactionPolicyState):
    def resource(self, ref: ResourceRef) -> ResourceState | None:
        result = super().resource(ref)
        if result is not None and ref.kind == ResourceKind.ENTITY:
            # Entity allowlists constrain graph reads even without an extra stored ID.
            result = result.model_copy(update={"entity_id": ref.id})
        return result


def _validated[T: BaseModel](model: type[T], value: T) -> T:
    # Pydantic model_copy/model_construct and mutable nested values can bypass normal
    # construction. Validate again before any transaction or persistence side effect.
    if not isinstance(value, model):
        raise TypeError("Expected a validated memory value")
    return model.model_validate(value.model_dump(mode="python", exclude_none=True))


def _path(ref: ResourceRef) -> str:
    return f"tenants/{ref.tenant_id}/{ref.kind}/{ref.id}"


def _inherited_scope(fact: Fact, dependency: Scope) -> None:
    if fact.visibility == Visibility.SHARED and dependency.visibility != Visibility.SHARED:
        raise PolicyDenied()
    exposure = {
        DisplayPolicy.NEVER: 0,
        DisplayPolicy.AUTHENTICATED_SESSION: 1,
        DisplayPolicy.SHARED_DISPLAY: 2,
    }
    if exposure[fact.display_policy] > exposure[dependency.display_policy]:
        raise PolicyDenied()


class MemoryRepository:
    """No tenant paths or caller grants accepted; every method requires TenantContext.

    Pass brainoid_persistence.firestore.FirestoreTransactions in production. All
    authorization, source and version reads precede writes in the same transaction.
    Callbacks must be read-only and retry-safe. External model/ledger writes are not
    allowed here. No API/framework dependencies or implicit credential discovery.
    """

    def __init__(
        self,
        store: TransactionStore,
        *,
        authorizer: CurrentAuthorizer,
        sources: SourceResolver,
        ledger: ControlLedger,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        allow_untracked_bootstrap: bool = False,
    ) -> None:
        self._store = store
        self._authorizer = authorizer
        self._sources = sources
        self._ledger = ledger
        self._now = now
        self._allow_untracked = allow_untracked_bootstrap

    def _ref(self, context: TenantContext, identity: ResourceId, kind: ResourceKind) -> ResourceRef:
        _validated(TenantContext, context)
        return ResourceRef(tenant_id=context.tenant_id, kind=kind, id=identity)

    def _authority(self, tx: Transaction, context: TenantContext) -> _Authority:
        caller = _validated(VerifiedCaller, self._authorizer.authorize(tx, context))
        subject = caller.subject
        now = normalize_utc(self._now())
        if not caller.valid_from <= now < caller.valid_to:
            raise PolicyDenied()
        actor = context.actor
        if isinstance(subject, UserSubject):
            if not isinstance(actor, UserActorContext) or actor.actor_id != subject.user_id:
                raise PolicyDenied()
        elif isinstance(subject, DeviceSubject):
            if not isinstance(actor, DeviceActorContext) or actor.actor_id != subject.device_id:
                raise PolicyDenied()
            session = actor.private_session
            if subject.private_session_id != (session.private_session_id if session else None):
                raise PolicyDenied()
        else:
            raise PolicyDenied()
        policy = Evaluator(
            _GraphPolicyState(
                tx, ledger=self._ledger, allow_untracked_bootstrap=self._allow_untracked
            ),
            now=self._now,
        )
        principal = policy.principal(context.tenant_id, subject)
        if principal.context.actor.authorization_version != actor.authorization_version:
            raise PolicyDenied()
        if principal.user_id is not None:
            try:
                account_stamp(tx, principal.user_id)
            except AccountUnavailable:
                raise PolicyDenied() from None
        result = _Authority(policy, caller, principal)
        self._finish(result)
        return result

    def _finish(self, authority: _Authority, *, model_fact: Fact | None = None) -> None:
        """No I/O: test proven deadlines after the last read and before effect/return."""
        now = normalize_utc(self._now())
        if not authority.caller.valid_from <= now < authority.caller.valid_to:
            raise PolicyDenied()
        actor = authority.principal.context.actor
        if isinstance(actor, DeviceActorContext) and actor.private_session is not None:
            session = actor.private_session
            if now >= min(session.expires_at, session.idle_expires_at):
                raise PolicyDenied()
        if model_fact is not None and (
            model_fact.deletion_epoch != 0
            or model_fact.status != "confirmed"
            or model_fact.valid_from > now
            or (model_fact.valid_to is not None and model_fact.valid_to <= now)
        ):
            raise PolicyDenied()

    def _run[T: Entity | Fact](
        self, work: Callable[[Transaction], tuple[T, _Authority]], *, for_model: bool = False
    ) -> T:
        result, authority = self._store.run(work)
        # A transaction/commit response can arrive after the validity interval. Never
        # release content then. For writes this denial does not undo a durable commit.
        self._finish(
            authority, model_fact=result if for_model and isinstance(result, Fact) else None
        )
        return result

    def _new(
        self,
        policy: Evaluator,
        subject: Subject,
        principal: Principal,
        ref: ResourceRef,
        record: Entity | Fact,
    ) -> None:
        if (
            principal.user_id is None
            or "memory.write" not in principal.context.actor.allowed_scopes
        ):
            raise PolicyDenied(hidden=False)
        state = resource_state(ref, record.model_dump(mode="json"))
        assert state is not None
        if isinstance(record, Entity):
            state = state.model_copy(update={"entity_id": record.id})
        scope = policy.search(ref.tenant_id, subject, ref.kind)
        if not any(item.matches(state) for item in scope.filters):
            raise PolicyDenied()

    def _read[T: Entity | Fact](
        self,
        tx: Transaction,
        policy: Evaluator,
        subject: Subject,
        ref: ResourceRef,
        model: type[T],
        *,
        action: Action = Action.READ,
    ) -> T:
        policy.resource(ref.tenant_id, subject, ref, action=action)
        document = tx.get(_path(ref))
        try:
            result = model.model_validate(document)
        except ValidationError:
            raise PolicyDenied() from None
        if result.id != ref.id or result.tenant_id != ref.tenant_id:
            raise PolicyDenied()
        return cast(T, result)

    def _dependencies(
        self,
        tx: Transaction,
        context: TenantContext,
        policy: Evaluator,
        subject: Subject,
        fact: Fact,
        *,
        reuse: bool,
    ) -> None:
        action = Action.MODEL_READ if reuse else Action.READ
        for identity in (fact.subject_entity_id, fact.object_entity_id):
            if identity is None:
                continue
            entity = self._read(
                tx,
                policy,
                subject,
                self._ref(context, identity, ResourceKind.ENTITY),
                Entity,
                action=action,
            )
            _inherited_scope(fact, entity)
            if reuse and entity.deletion_epoch != 0:
                raise PolicyDenied()
        for source in fact.source_refs:
            resolved = _validated(ResolvedSource, self._sources.resolve(tx, context, source))
            if (
                resolved.source != source
                or resolved.authority.tenant_id != context.tenant_id
                or resolved.deleted
                or (reuse and (resolved.suppress_reuse or resolved.deletion_epoch != 0))
            ):
                raise PolicyDenied()
            grant = policy.resource(context.tenant_id, subject, resolved.authority, action=action)
            authority = tx.get(_path(resolved.authority))
            if (
                authority is None
                or authority.get("deleted") is not False
                or type(authority.get("suppress_reuse")) is not bool
                or (reuse and authority["suppress_reuse"])
            ):
                raise PolicyDenied()
            _inherited_scope(fact, grant.scope)
            if reuse and grant.resource.deletion_epoch != 0:
                raise PolicyDenied()

    def create_entity(
        self, context: TenantContext, identity: ResourceId, values: EntityInput
    ) -> Entity:
        ref = self._ref(context, identity, ResourceKind.ENTITY)
        values = _validated(EntityInput, values)

        def work(tx: Transaction) -> tuple[Entity, _Authority]:
            authority = self._authority(tx, context)
            policy, subject, principal = authority.policy, authority.subject, authority.principal
            now = normalize_utc(self._now())
            record = Entity(
                **values.model_dump(exclude_none=True),
                id=ref.id,
                tenant_id=ref.tenant_id,
                owner_user_id=principal.user_id,
                created_by=authority.creator(),
                created_via_device_id=authority.device_id,
                version=1,
                deletion_epoch=0,
                schema_version=1,
                deleted=False,
                suppress_reuse=False,
                created_at=now,
                updated_at=now,
            )
            self._new(policy, subject, principal, ref, record)
            if tx.get(_path(ref)) is not None:
                # Re-creation never overwrites an existing record, including tombstones.
                policy.resource(ref.tenant_id, subject, ref)
                self._finish(authority)
                raise AlreadyExists()
            document = record.model_dump(mode="json", exclude_none=True)
            self._finish(authority)
            tx.create(_path(ref), document)
            return record, authority

        return self._run(work)

    def create_fact(self, context: TenantContext, identity: ResourceId, values: FactInput) -> Fact:
        ref = self._ref(context, identity, ResourceKind.MEMORY)
        values = _validated(FactInput, values)
        if values.status not in ("candidate", "confirmed"):
            raise ValueError("New facts must be candidate or confirmed")

        def work(tx: Transaction) -> tuple[Fact, _Authority]:
            authority = self._authority(tx, context)
            policy, subject, principal = authority.policy, authority.subject, authority.principal
            now = normalize_utc(self._now())
            record = Fact(
                **values.model_dump(exclude_none=True),
                id=ref.id,
                tenant_id=ref.tenant_id,
                owner_user_id=principal.user_id,
                created_by=authority.creator(),
                created_via_device_id=authority.device_id,
                version=1,
                deletion_epoch=0,
                schema_version=1,
                deleted=False,
                suppress_reuse=False,
                created_at=now,
                updated_at=now,
                recorded_at=now,
            )
            self._new(policy, subject, principal, ref, record)
            self._dependencies(tx, context, policy, subject, record, reuse=True)
            if tx.get(_path(ref)) is not None:
                policy.resource(ref.tenant_id, subject, ref)
                self._finish(authority)
                raise AlreadyExists()
            document = record.model_dump(mode="json", exclude_none=True)
            self._finish(authority)
            tx.create(_path(ref), document)
            return record, authority

        return self._run(work)

    def get_entity(self, context: TenantContext, identity: ResourceId) -> Entity:
        ref = self._ref(context, identity, ResourceKind.ENTITY)

        def work(tx: Transaction) -> tuple[Entity, _Authority]:
            authority = self._authority(tx, context)
            record = self._read(tx, authority.policy, authority.subject, ref, Entity)
            self._finish(authority)
            return record, authority

        return self._run(work)

    def get_fact(
        self, context: TenantContext, identity: ResourceId, *, for_model: bool = False
    ) -> Fact:
        ref = self._ref(context, identity, ResourceKind.MEMORY)

        def work(tx: Transaction) -> tuple[Fact, _Authority]:
            authority = self._authority(tx, context)
            policy, subject = authority.policy, authority.subject
            record = self._read(
                tx,
                policy,
                subject,
                ref,
                Fact,
                action=Action.MODEL_READ if for_model else Action.READ,
            )
            self._finish(authority, model_fact=record if for_model else None)
            self._dependencies(tx, context, policy, subject, record, reuse=for_model)
            self._finish(authority, model_fact=record if for_model else None)
            return record, authority

        return self._run(work, for_model=for_model)

    def _expected(self, version: Version, epoch: Epoch) -> None:
        TypeAdapter(Version).validate_python(version)
        TypeAdapter(Epoch).validate_python(epoch)

    def update_entity(
        self,
        context: TenantContext,
        identity: ResourceId,
        patch: EntityPatch,
        *,
        expected_version: Version,
        expected_deletion_epoch: Epoch,
    ) -> Entity:
        ref = self._ref(context, identity, ResourceKind.ENTITY)
        patch = _validated(EntityPatch, patch)
        self._expected(expected_version, expected_deletion_epoch)

        def work(tx: Transaction) -> tuple[Entity, _Authority]:
            authority = self._authority(tx, context)
            record = self._read(
                tx, authority.policy, authority.subject, ref, Entity, action=Action.WRITE
            )
            if (record.version, record.deletion_epoch) != (
                expected_version,
                expected_deletion_epoch,
            ):
                raise VersionConflict()
            replacement = Entity.model_validate(
                {
                    **record.model_dump(exclude_none=True),
                    **patch.model_dump(exclude_none=True),
                    "version": record.version + 1,
                    "updated_at": normalize_utc(self._now()),
                }
            )
            document = replacement.model_dump(mode="json", exclude_none=True)
            self._finish(authority)
            tx.set(_path(ref), document)
            return replacement, authority

        return self._run(work)

    def update_fact(
        self,
        context: TenantContext,
        identity: ResourceId,
        *,
        status: Literal["retracted"],
        expected_version: Version,
        expected_deletion_epoch: Epoch,
    ) -> Fact:
        """Retract without rewriting a claim. Atomic supersession belongs to MEM-03."""
        ref = self._ref(context, identity, ResourceKind.MEMORY)
        self._expected(expected_version, expected_deletion_epoch)
        if status != "retracted":
            raise ValueError("This repository update only supports retraction")

        def work(tx: Transaction) -> tuple[Fact, _Authority]:
            authority = self._authority(tx, context)
            policy, subject = authority.policy, authority.subject
            record = self._read(tx, policy, subject, ref, Fact, action=Action.WRITE)
            if (record.version, record.deletion_epoch) != (
                expected_version,
                expected_deletion_epoch,
            ):
                raise VersionConflict()
            if record.status not in ("candidate", "confirmed"):
                raise VersionConflict()
            # Withdrawal does not reuse the source in a model, but returning the fact
            # still requires current read access to its references and provenance.
            self._dependencies(tx, context, policy, subject, record, reuse=False)
            replacement = Fact.model_validate(
                {
                    **record.model_dump(exclude_none=True),
                    "status": status,
                    "version": record.version + 1,
                    "updated_at": normalize_utc(self._now()),
                }
            )
            document = replacement.model_dump(mode="json", exclude_none=True)
            self._finish(authority)
            tx.set(_path(ref), document)
            return replacement, authority

        return self._run(work)
