"""Atomic 24-hour deduplication of database mutations, with current authorization."""

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Annotated

from brainoid_contracts import ResourceId, Version, normalize_utc
from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from brainoid_api.common.access import AuthorizedScope, fingerprint
from brainoid_api.common.transactions import Transaction, TransactionStore
from brainoid_api.errors import APIError, ErrorCode


class MutationResult(BaseModel):
    """Replay-safe receipt, deliberately unable to hold a token or arbitrary response.

    Business endpoints authorize retrieval separately. Token issuance needs its own
    protocol and cannot store secret response bodies in this generic record.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    status_code: Annotated[int, Field(strict=True, ge=200, le=299)]
    resource_id: ResourceId | None = None
    operation_id: ResourceId | None = None
    version: Version | None = None

    @model_validator(mode="after")
    def has_reference(self) -> "MutationResult":
        if self.resource_id is None and self.operation_id is None:
            raise ValueError("Mutation results require a durable reference")
        return self


type Authorize = Callable[[Transaction, MutationResult | None], AuthorizedScope]
type Mutate = Callable[[Transaction, AuthorizedScope], MutationResult]


class Idempotency:
    def __init__(self, store: TransactionStore, *, now: Callable[[], datetime]) -> None:
        self._store = store
        self._now = now

    def execute(
        self,
        *,
        operation: str,
        key: str | None,
        body: JsonValue,
        authorize: Authorize,
        mutate: Mutate,
    ) -> MutationResult:
        """Authorize on every attempt, then commit business writes and receipt together.

        `operation` is a server-defined operation plus target identity (not a raw URL).
        `body` is schema-normalized JSON. Mutate may run multiple times; its only
        effects must be this transaction's DB writes, including any outbox job.
        `authorize(tx, None)` must read current IAM/request policy. On replay it is
        called again with the stored receipt, so generated resource/operation IDs can
        be checked for current ownership, scope and deletion. Both checks use this
        transaction; a captured old context is not a current authorization check.
        """
        if key is None or not 16 <= len(key) <= 128:
            raise APIError(ErrorCode.INVALID_REQUEST)
        if not operation:
            raise ValueError("A server-defined operation is required")
        body_hash = fingerprint(body)

        def execute(transaction: Transaction) -> MutationResult:
            current = authorize(transaction, None)
            now = normalize_utc(self._now())
            current.ensure_live(now)
            actor = current.context.actor
            record_id = fingerprint(
                [
                    str(current.context.tenant_id),
                    actor.actor_type,
                    str(actor.actor_id),
                    operation,
                    key,
                ]
            )
            path = f"tenants/{current.context.tenant_id}/idempotency_records/{record_id}"
            record = transaction.get(path)
            if record is not None and now < record["expires_at"]:
                result = MutationResult.model_validate(record["result"])
                replay_scope = authorize(transaction, result)
                replay_scope.ensure_live(self._now())
                if (
                    record["scope_binding"] != current.binding()
                    or record["scope_binding"] != replay_scope.binding()
                ):
                    raise APIError(ErrorCode.OPERATION_FORBIDDEN)
                if record["body_hash"] != body_hash:
                    raise APIError(ErrorCode.IDEMPOTENCY_CONFLICT)
                return result

            result = mutate(transaction, current)
            current.ensure_live(self._now())
            # Validate the exact allowlist even when a callback returns a subclass.
            result = MutationResult.model_validate(result.model_dump(mode="json"))
            transaction.set(
                path,
                {
                    "body_hash": body_hash,
                    "scope_binding": current.binding(),
                    "created_at": now,
                    "expires_at": now + timedelta(hours=24),
                    "result": result.model_dump(mode="json"),
                },
            )
            return result

        return self._store.run(execute)
