"""Tenant-bound graph persistence over the shared Firestore transaction adapter."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, Protocol, Self, cast

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
from brainoid_contracts.guards import protect
from brainoid_contracts.transactions import Document, Transaction, TransactionStore
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
from brainoid_policy.scopes import PolicyDenied, PolicyUnavailable, Principal, ResourceGrant
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

if TYPE_CHECKING:
    from brainoid_memory.summaries import SummaryTransaction


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
        document = self._transaction.get(_path(ref))
        result = resource_state(ref, document)
        if result is not None and document is not None and ref.kind == ResourceKind.ENTITY:
            result = _entity_target(result, document.get("kind"))
        return result


def _entity_target(state: ResourceState, kind: object) -> ResourceState:
    # Control allowlists apply to appliances/rooms, not a person's name or topic.
    # Unknown kinds remain fenced and the complete stored model must still validate.
    if kind == "room":
        return state.model_copy(update={"room_id": state.ref.id})
    if kind not in ("person", "activity", "topic"):
        return state.model_copy(update={"entity_id": state.ref.id})
    return state


def _validated[T: BaseModel](model: type[T], value: T) -> T:
    # Pydantic model_copy/model_construct and mutable nested values can bypass normal
    # construction. Validate again before any transaction or persistence side effect.
    if not isinstance(value, model):
        raise TypeError("Expected a validated memory value")
    return model.model_validate(value.model_dump(mode="python", exclude_none=True))


def _stored(record: Entity | Fact, *, mode: Literal["python", "json"] = "python") -> Document:
    # owner_user_id is required even when null on shared records. Other null values
    # stay omitted: Fact's absent object/value and qualifier fields reject explicit null.
    return {
        **record.model_dump(mode=mode, exclude_none=True),
        "owner_user_id": record.owner_user_id,
    }


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
        output: Subject | None = None,
        before_finish: Callable[[], None] = lambda: None,
    ) -> None:
        self._store = store
        self._authorizer = authorizer
        self._sources = sources
        self._ledger = ledger
        self._now = now
        self._allow_untracked = allow_untracked_bootstrap
        self._output = output
        self._before_finish = before_finish

    def entities(self, tx: Transaction, context: TenantContext) -> "EntityTransaction":
        """Compose DB-only entity operations with the caller's transaction.

        Never call store.run here. The caller must call finish after its last read,
        before additional writes, and again after its transaction response arrives.
        The returned object is transaction-local, not a reusable permission grant.
        """
        _validated(TenantContext, context)
        return EntityTransaction(self, tx, context, self._authority(tx, context))

    def facts(self, tx: Transaction, context: TenantContext) -> "FactTransaction":
        """Compose current fact access and atomic revisions with jobs and receipts."""
        _validated(TenantContext, context)
        return FactTransaction(self, tx, context, self._authority(tx, context))

    def summaries(self, tx: Transaction, context: TenantContext) -> "SummaryTransaction":
        from brainoid_memory.summaries import SummaryTransaction

        _validated(TenantContext, context)
        return SummaryTransaction(self, tx, context, self._authority(tx, context))

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
        self._before_finish()
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
            state = _entity_target(state, record.kind)
        scope = policy.search(ref.tenant_id, subject, ref.kind, output=self._output)
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
        policy.resource(ref.tenant_id, subject, ref, action=action, output=self._output)
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
        if fact.supersedes_fact_id is not None:
            predecessor = self._read(
                tx,
                policy,
                subject,
                self._ref(context, fact.supersedes_fact_id, ResourceKind.MEMORY),
                Fact,
            )
            _inherited_scope(fact, predecessor)
            if predecessor.deletion_epoch != 0 or (
                predecessor.visibility == Visibility.PRIVATE
                and predecessor.owner_user_id != fact.owner_user_id
            ):
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
            grant = policy.resource(
                context.tenant_id, subject, resolved.authority, action=action, output=self._output
            )
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
        def work(tx: Transaction) -> tuple[Entity, _Authority]:
            access = self.entities(tx, context)
            return access.create(identity, values), access._authority

        return self._run(work)

    def create_fact(self, context: TenantContext, identity: ResourceId, values: FactInput) -> Fact:
        self._ref(context, identity, ResourceKind.MEMORY)
        values = _validated(FactInput, values)
        if values.status not in ("candidate", "confirmed"):
            raise ValueError("New facts must be candidate or confirmed")

        def work(tx: Transaction) -> tuple[Fact, _Authority]:
            access = self.facts(tx, context)
            change = access.prepare(identity, values)
            access.save(change)
            return change.record, access._authority

        return self._run(work)

    def get_entity(self, context: TenantContext, identity: ResourceId) -> Entity:
        def work(tx: Transaction) -> tuple[Entity, _Authority]:
            access = self.entities(tx, context)
            return access.get(identity), access._authority

        return self._run(work)

    def get_fact(
        self, context: TenantContext, identity: ResourceId, *, for_model: bool = False
    ) -> Fact:
        def work(tx: Transaction) -> tuple[Fact, _Authority]:
            access = self.facts(tx, context)
            return access.get(identity, for_model=for_model), access._authority

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
        def work(tx: Transaction) -> tuple[Entity, _Authority]:
            access = self.entities(tx, context)
            record = access.update(
                identity,
                patch,
                expected_version=expected_version,
                expected_deletion_epoch=expected_deletion_epoch,
            )
            return record, access._authority

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
                    **_stored(record),
                    "status": status,
                    "version": record.version + 1,
                    "updated_at": normalize_utc(self._now()),
                }
            )
            document = _stored(replacement, mode="json")
            self._finish(authority)
            tx.set(_path(ref), document)
            return replacement, authority

        return self._run(work)


class _MemoryTransaction:
    """Transaction-local authority shared by entity and fact operations."""

    def __init__(
        self,
        repository: MemoryRepository,
        tx: Transaction,
        context: TenantContext,
        authority: _Authority,
    ) -> None:
        self._repo, self._tx, self._context, self._authority = repository, tx, context, authority
        self._model_from: datetime | None = None
        self._model_until: datetime | None = None
        protect(tx, self.finish)

    @property
    def principal(self) -> Principal:
        return self._authority.principal

    @property
    def caller(self) -> VerifiedCaller:
        return self._authority.caller

    def finish(self) -> None:
        self._repo._finish(self._authority)
        now = normalize_utc(self._repo._now())
        if (self._model_from is not None and now < self._model_from) or (
            self._model_until is not None and now >= self._model_until
        ):
            raise PolicyDenied()

    def _retain_validity(self, record: Fact) -> None:
        self._model_from = max(self._model_from or record.valid_from, record.valid_from)
        if record.valid_to is not None:
            self._model_until = min(self._model_until or record.valid_to, record.valid_to)
        self.finish()

    def scopes(self) -> tuple[Scope, ...]:
        allowed = self._authority.policy.search(
            self._context.tenant_id,
            self._authority.subject,
            ResourceKind.MEMORY,
            output=self._repo._output,
        )
        self.finish()
        return tuple(
            Scope(visibility=f.visibility, display_policy=d)
            for f in allowed.filters
            for d in sorted(f.display_policies)
            if f.visibility != Visibility.PRIVATE or d != DisplayPolicy.SHARED_DISPLAY
        )

    def _candidates(self, kind: ResourceKind, scope: Scope | None) -> list[tuple[str, Document]]:
        path = f"tenants/{self._context.tenant_id}/{kind}"
        if scope is None:
            return self._tx.collection(path)
        if scope not in self.scopes():
            raise PolicyDenied()
        filters = {"visibility": str(scope.visibility), "display_policy": str(scope.display_policy)}
        if scope.visibility == Visibility.PRIVATE:
            if self.principal.user_id is None:
                raise PolicyDenied()
            filters["owner_user_id"] = self.principal.user_id
        return self._tx.query(path, filters)


class EntityTransaction(_MemoryTransaction):
    """Current entity access for atomic composition with receipts/provider indexes."""

    def get(self, identity: ResourceId, *, write: bool = False, for_model: bool = False) -> Entity:
        result = self._repo._read(
            self._tx,
            self._authority.policy,
            self._authority.subject,
            self._repo._ref(self._context, identity, ResourceKind.ENTITY),
            Entity,
            action=Action.WRITE if write else Action.MODEL_READ if for_model else Action.READ,
        )
        self.finish()
        return result

    def candidates(self, query: str | None = None, *, scope: Scope | None = None) -> list[Entity]:
        """Filter access before inspecting labels, counting or forming cursor positions."""
        allowed = self._authority.policy.search(
            self._context.tenant_id,
            self._authority.subject,
            ResourceKind.ENTITY,
            output=self._repo._output,
        )
        results = []
        for path, doc in self._candidates(ResourceKind.ENTITY, scope):
            try:
                identity = TypeAdapter(ResourceId).validate_python(doc.get("id"))
                ref = self._repo._ref(self._context, identity, ResourceKind.ENTITY)
                state = resource_state(ref, doc)
                if state is None or path != _path(ref):
                    continue
                state = _entity_target(state, doc.get("kind"))
                if not any(f.matches(state) for f in allowed.filters):
                    continue
                record = self.get(ref.id)
            except PolicyUnavailable:
                raise
            except (PolicyDenied, ValidationError):
                continue
            if query is None or any(
                query.casefold() in name.casefold() for name in (record.label, *record.aliases)
            ):
                results.append(record)
        self.finish()
        return sorted(results, key=lambda item: item.id)

    def create(self, identity: ResourceId, values: EntityInput) -> Entity:
        ref = self._repo._ref(self._context, identity, ResourceKind.ENTITY)
        values = _validated(EntityInput, values)
        now = normalize_utc(self._repo._now())
        record = Entity(
            **values.model_dump(exclude_none=True),
            id=ref.id,
            tenant_id=ref.tenant_id,
            owner_user_id=self.principal.user_id,
            created_by=self._authority.creator(),
            created_via_device_id=self._authority.device_id,
            version=1,
            deletion_epoch=0,
            schema_version=1,
            deleted=False,
            suppress_reuse=False,
            created_at=now,
            updated_at=now,
        )
        self._repo._new(
            self._authority.policy, self._authority.subject, self.principal, ref, record
        )
        if self._tx.get(_path(ref)) is not None:
            self.get(ref.id)
            raise AlreadyExists()
        self.finish()
        self._tx.create(_path(ref), _stored(record, mode="json"))
        return record

    def update(
        self,
        identity: ResourceId,
        patch: EntityPatch,
        *,
        expected_version: Version,
        expected_deletion_epoch: Epoch,
    ) -> Entity:
        patch = _validated(EntityPatch, patch)
        self._repo._expected(expected_version, expected_deletion_epoch)
        record = self.get(identity, write=True)
        if (record.version, record.deletion_epoch) != (expected_version, expected_deletion_epoch):
            raise VersionConflict()
        replacement = Entity.model_validate(
            {
                **_stored(record),
                **patch.model_dump(exclude_none=True),
                "version": record.version + 1,
                "updated_at": normalize_utc(self._repo._now()),
            }
        )
        self.finish()
        self._tx.set(
            _path(self._repo._ref(self._context, identity, ResourceKind.ENTITY)),
            _stored(replacement, mode="json"),
        )
        return replacement


@dataclass(frozen=True)
class FactChange:
    """Prepared canonical creation, with optional predecessor replacement."""

    record: Fact
    grant: ResourceGrant
    previous: Fact | None = None

    @property
    def document(self) -> Document:
        return _stored(self.record, mode="json")


class FactTransaction(_MemoryTransaction):
    """All reads in prepare precede save and any caller's job/receipt writes."""

    def get(self, identity: ResourceId, *, write: bool = False, for_model: bool = False) -> Fact:
        record = self._repo._read(
            self._tx,
            self._authority.policy,
            self._authority.subject,
            self._repo._ref(self._context, identity, ResourceKind.MEMORY),
            Fact,
            action=Action.WRITE if write else Action.MODEL_READ if for_model else Action.READ,
        )
        self._repo._dependencies(
            self._tx,
            self._context,
            self._authority.policy,
            self._authority.subject,
            record,
            reuse=for_model,
        )
        self._repo._finish(self._authority, model_fact=record if for_model else None)
        if for_model:
            self._retain_validity(record)
        return record

    def candidates(self, *, scope: Scope | None = None, for_model: bool = False) -> list[Fact]:
        allowed = self._authority.policy.search(
            self._context.tenant_id,
            self._authority.subject,
            ResourceKind.MEMORY,
            output=self._repo._output,
        )
        results = []
        for path, doc in self._candidates(ResourceKind.MEMORY, scope):
            try:
                identity = TypeAdapter(ResourceId).validate_python(doc.get("id"))
                ref = self._repo._ref(self._context, identity, ResourceKind.MEMORY)
                state = resource_state(ref, doc)
                if state is None or path != _path(ref):
                    continue
                if not any(item.matches(state) for item in allowed.filters):
                    continue
                record = self.get(identity, for_model=for_model)
            except PolicyUnavailable:
                raise
            except (PolicyDenied, ValidationError):
                continue
            # History remains addressable by ID, always with its explicit status.
            if record.status == "confirmed":
                results.append(record)
        self.finish()
        return sorted(results, key=lambda record: record.id)

    def revision_input(self, identity: ResourceId) -> Fact:
        """Historical input only. Caller MUST prove its own atomic supersession.

        This never becomes a current confirmed result. Still recheck every original
        dependency, validity, suppress_reuse and epoch as for a current model read.
        """
        record = self._repo._read(
            self._tx,
            self._authority.policy,
            self._authority.subject,
            self._repo._ref(self._context, identity, ResourceKind.MEMORY),
            Fact,
            action=Action.MODEL_READ,
        )
        if record.status != "superseded":
            raise PolicyDenied()
        self._repo._dependencies(
            self._tx,
            self._context,
            self._authority.policy,
            self._authority.subject,
            record,
            reuse=True,
        )
        self._repo._finish(
            self._authority, model_fact=record.model_copy(update={"status": "confirmed"})
        )
        self._retain_validity(record)
        return record

    def entities(self) -> EntityTransaction:
        return self._repo.entities(self._tx, self._context)

    def finish_records(self, records: tuple[Fact, ...]) -> None:
        self.finish()
        for record in records:
            self._repo._finish(self._authority, model_fact=record)

    def prepare(
        self,
        identity: ResourceId,
        values: FactInput,
        *,
        predecessor: ResourceId | None = None,
        expected_version: Version | None = None,
        expected_deletion_epoch: Epoch = 0,
    ) -> FactChange:
        values = _validated(FactInput, values)
        if values.status not in ("candidate", "confirmed"):
            raise ValueError("New facts must be candidate or confirmed")
        prior = None
        if predecessor is not None:
            if expected_version is None:
                raise ValueError("Revisions require an expected version")
            self._repo._expected(expected_version, expected_deletion_epoch)
            prior = self.get(predecessor, write=True)
            if (prior.version, prior.deletion_epoch) != (
                expected_version,
                expected_deletion_epoch,
            ) or prior.status != "confirmed":
                raise VersionConflict()
            # Sharing/ownership changes require a separate explicit workflow. The
            # revision ID is itself provenance and must not publish private history.
            if (values.visibility, values.display_policy) != (
                prior.visibility,
                prior.display_policy,
            ):
                raise PolicyDenied()
        now = normalize_utc(self._repo._now())
        ref = self._repo._ref(self._context, identity, ResourceKind.MEMORY)
        record = Fact(
            **values.model_dump(exclude_none=True),
            id=identity,
            tenant_id=ref.tenant_id,
            owner_user_id=self.principal.user_id if prior is None else prior.owner_user_id,
            created_by=self._authority.creator(),
            created_via_device_id=self._authority.device_id,
            version=1,
            deletion_epoch=0,
            schema_version=1,
            deleted=False,
            suppress_reuse=False,
            created_at=now,
            updated_at=now,
            recorded_at=now,
            supersedes_fact_id=None if prior is None else prior.id,
        )
        self._repo._new(
            self._authority.policy, self._authority.subject, self.principal, ref, record
        )
        self._repo._dependencies(
            self._tx,
            self._context,
            self._authority.policy,
            self._authority.subject,
            record,
            reuse=True,
        )
        if self._tx.get(_path(ref)) is not None:
            self.get(identity)
            raise AlreadyExists()
        state = resource_state(ref, record.model_dump(mode="json"))
        assert state is not None
        self.finish()
        return FactChange(
            record,
            ResourceGrant(self.principal, state, Action.MODEL_READ),
            None
            if prior is None
            else Fact.model_validate(
                {
                    **_stored(prior),
                    "status": "superseded",
                    "version": prior.version + 1,
                    "updated_at": now,
                }
            ),
        )

    def save(self, change: FactChange, *, source_already_created: bool = False) -> None:
        """Writes only; caller must finish again after the commit response."""
        self.finish()
        if not source_already_created:
            self._tx.create(
                _path(self._repo._ref(self._context, change.record.id, ResourceKind.MEMORY)),
                change.document,
            )
        if change.previous is not None:
            self._tx.set(
                _path(self._repo._ref(self._context, change.previous.id, ResourceKind.MEMORY)),
                _stored(change.previous, mode="json"),
            )
