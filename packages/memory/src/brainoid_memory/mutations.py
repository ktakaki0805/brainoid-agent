"""One shared atomic explicit-memory mutation for HTTP and durable typed tools."""

from collections.abc import Callable
from datetime import datetime
from uuid import uuid4

from brainoid_contracts import ResourceId, Scope, UtcTimestamp, Version
from brainoid_contracts.access import AuthorizedScope, fingerprint
from brainoid_contracts.errors import APIError, ErrorCode
from brainoid_contracts.idempotency import Idempotency, MutationResult
from brainoid_contracts.transactions import Transaction, TransactionStore
from brainoid_jobs import Jobs
from brainoid_policy.accounts import account_stamp
from brainoid_policy.scopes import PolicyDenied

from brainoid_memory.dto import MemoryCreate
from brainoid_memory.models import Fact
from brainoid_memory.repository import FactTransaction


class MutationScope(AuthorizedScope):
    valid_from: UtcTimestamp
    valid_to: UtcTimestamp

    def ensure_live(self, now: datetime) -> None:
        super().ensure_live(now)
        if not self.valid_from <= now < self.valid_to:
            raise APIError(ErrorCode.AUTHENTICATION_REQUIRED)

    def binding(self) -> str:
        return AuthorizedScope.model_validate(
            {field: getattr(self, field) for field in AuthorizedScope.model_fields}
        ).binding()


def effect_path(tenant: ResourceId, effect: ResourceId) -> str:
    return f"tenants/{tenant}/memory_effects/{effect}"


class MemoryMutations:
    def __init__(self, store: TransactionStore, jobs: Jobs, *, now: Callable[[], datetime]) -> None:
        self.store, self.jobs, self.now = store, jobs, now
        self.idempotency = Idempotency(store, now=now)

    def mutate(
        self,
        open_access: Callable[[Transaction], FactTransaction],
        body: MemoryCreate,
        key: str | None,
        *,
        predecessor: ResourceId | None = None,
        expected: Version | None = None,
        reason: str | None = None,
        effect: ResourceId | None = None,
    ) -> Fact:
        body = MemoryCreate.model_validate(body.model_dump(exclude_none=True))
        memory = effect or ResourceId(str(uuid4()))
        access: FactTransaction | None = None
        epoch = 0

        def authorize(tx: Transaction, replay: MutationResult | None) -> AuthorizedScope:
            nonlocal access, epoch
            access = open_access(tx)
            principal = access.principal
            if (
                principal.user_id is None
                or "memory.write" not in principal.context.actor.allowed_scopes
            ):
                raise PolicyDenied(hidden=False)
            old = None if predecessor is None else access.get(predecessor, write=True)
            epoch = 0 if old is None else old.deletion_epoch
            record = old
            if replay is not None:
                if replay.resource_id is None:
                    raise PolicyDenied()
                record = access.get(replay.resource_id, write=True)
                if record.supersedes_fact_id != predecessor or record.deletion_epoch != 0:
                    raise PolicyDenied()
            scope = record or body
            current = MutationScope(
                context=principal.context,
                scope=Scope(visibility=scope.visibility, display_policy=scope.display_policy),
                owner_user_id=principal.user_id,
                authorization_stamp=fingerprint(
                    [
                        principal.stamp,
                        account_stamp(tx, principal.user_id),
                        epoch,
                    ]
                ),
                valid_from=access.caller.valid_from,
                valid_to=access.caller.valid_to,
            )
            access.finish()
            return current

        def mutate(tx: Transaction, current: AuthorizedScope) -> MutationResult:
            assert access is not None
            recorded = self.now()
            if body.valid_from is None and body.valid_to is not None and body.valid_to <= recorded:
                raise APIError(ErrorCode.INVALID_REQUEST)
            change = access.prepare(
                memory,
                body.fact(recorded),
                predecessor=predecessor,
                expected_version=expected,
                expected_deletion_epoch=epoch,
            )
            self.jobs.add_created_memory(
                tx,
                grant=change.grant,
                source=change.document,
                before_write=access.finish,
            )
            access.save(change, source_already_created=True)
            if effect is not None:
                # This proof commits with new fact + predecessor + dormant index job
                # + idempotency receipt. A tool execution marker alone proves nothing.
                tx.create(
                    effect_path(current.context.tenant_id, effect),
                    {
                        "effect_id": effect,
                        "record": change.document,
                        "previous": None
                        if change.previous is None
                        else change.previous.model_dump(mode="json", exclude_none=True),
                        "request": body.model_dump(mode="json", exclude_none=True),
                        "expected_version": expected,
                        "predecessor": predecessor,
                    },
                )
            access.finish()
            return MutationResult(
                status_code=201 if predecessor is None else 200, resource_id=memory, version=1
            )

        result = self.idempotency.execute(
            operation="memories.create"
            if predecessor is None
            else f"memories.revise:{predecessor}",
            key=key,
            body={
                "replacement": body.model_dump(mode="json", exclude_none=True),
                "version": expected,
                "reason": reason,
            },
            authorize=authorize,
            mutate=mutate,
        )
        assert access is not None and result.resource_id is not None
        access.finish()

        def read(tx: Transaction) -> tuple[Fact, FactTransaction]:
            current = open_access(tx)
            result = current.get(memory if effect else result_id)
            current.finish()
            return result, current

        result_id = result.resource_id
        record, access = self.store.run(read)
        access.finish()
        return record
