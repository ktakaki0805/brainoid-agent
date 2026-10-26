"""Durable reservation, current authority, and an explicit outside-transaction effect."""

import hashlib
import json
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Protocol
from uuid import UUID, uuid5

from brainoid_agent_core.ports import CallControl, Clock
from brainoid_agent_core.types import (
    Cancelled,
    Failed,
    TimedOut,
    ToolExecution,
    ToolExecutionResult,
    ToolResult,
)
from brainoid_agent_core.types.common import CoreValue
from brainoid_contracts import ResourceId, TenantContext
from brainoid_contracts.transactions import Document, Transaction, TransactionStore
from pydantic import Field

from brainoid_tools.registry import Arguments, Invocation, Registration, Registry
from brainoid_tools.results import HandlerResult, ToolRejected, correlate


class Authority(CoreValue):
    """Server-selected current state. receipt binds account, source, target and authority."""

    context: TenantContext
    run_id: ResourceId
    receipt: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    allowed_tools: frozenset[str]
    max_calls: Annotated[int, Field(strict=True, ge=1, le=10000)]
    confirmation: Literal["not_required", "waiting", "confirmed", "rejected"]
    proposal_id: ResourceId | None = None


class Authorizer(Protocol):
    def check(self, tx: Transaction, request: ToolExecution, arguments: Arguments) -> Authority:
        """Read-only, same transaction: IAM/account/credential, run lease, agent allowlist,
        canonical target/source/version, explicit intent, confirmation and output rights.
        Derive current context from trusted server state. A previous context/grant alone
        is never authority. Raise ToolRejected(permission_denied) on stale/denied state.
        Infrastructure errors must propagate, never be disguised as successful authority.
        """
        ...


@dataclass(frozen=True)
class Prepared:
    authority: Authority
    existing: Document | None


class Executor:
    def __init__(
        self,
        store: TransactionStore,
        registry: Registry,
        authorizer: Authorizer,
        clock: Clock,
    ) -> None:
        self.store = store
        self.registry = registry
        self.authorizer = authorizer
        self.clock = clock

    def _stopped(self, control: CallControl) -> Cancelled | TimedOut | None:
        if control.cancellation.cancelled:
            return Cancelled()
        if control.deadline is not None and self.clock.monotonic() >= control.deadline:
            return TimedOut()
        return None

    def _check(
        self,
        tx: Transaction,
        request: ToolExecution,
        arguments: Arguments,
        previous: Authority | None = None,
    ) -> Authority:
        # Hooks receive independent snapshots so mutation cannot replace later checks.
        current = self.authorizer.check(
            tx, request.model_copy(deep=True), arguments.model_copy(deep=True)
        )
        current = Authority.model_validate(current.model_dump())
        if (
            current.context != request.context
            or request.call.name not in current.allowed_tools
            or current.confirmation == "rejected"
            or (current.confirmation == "waiting" and current.proposal_id is None)
            or (previous is not None and current != previous)
        ):
            raise ToolRejected("permission_denied")
        return current

    def _audit(
        self,
        tx: Transaction,
        request: ToolExecution,
        identity: ResourceId,
        status: str,
        name: str | None,
    ) -> None:
        # Never include arguments, provider call_key, idempotency key, payload or exception.
        tx.set(
            f"tenants/{request.context.tenant_id}/tool_audit/{identity}-{status}",
            {
                "tool_call_id": request.tool_call_id,
                "effect_id": identity,
                "tool_name": name,
                "status": status,
                "recorded_at": self.clock.now().isoformat(),
            },
        )

    async def execute(self, request: ToolExecution, *, control: CallControl) -> ToolExecutionResult:
        return await self._run(request, control=control, reconcile=False)

    async def reconcile(
        self, request: ToolExecution, *, control: CallControl
    ) -> ToolExecutionResult:
        """Explicit provider-state lookup. Never dispatch the original effect again."""
        return await self._run(request, control=control, reconcile=True)

    async def _run(
        self, request: ToolExecution, *, control: CallControl, reconcile: bool
    ) -> ToolExecutionResult:
        request = request.model_copy(deep=True)
        if (stopped := self._stopped(control)) is not None:
            return stopped
        identity = ResourceId(str(uuid5(UUID(request.tool_call_id), "brainoid-tool-effect")))
        registration: Registration[Any] | None = None
        started = False
        try:
            registration, arguments, canonical = self.registry.validate(request.call)
            context_data = request.context.model_dump(mode="json")
            context_data["actor"]["allowed_scopes"] = sorted(request.context.actor.allowed_scopes)
            digest = hashlib.sha256(
                json.dumps(
                    [
                        context_data,
                        request.call.name,
                        canonical,
                        request.idempotency_key,
                    ],
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            base = f"tenants/{request.context.tenant_id}"
            path = f"{base}/tool_executions/{request.tool_call_id}"

            def prepare(tx: Transaction) -> Prepared:
                authority = self._check(tx, request, arguments)
                saved = tx.get(path)
                key = hashlib.sha256(request.idempotency_key.encode()).hexdigest()
                key_path = f"{base}/tool_keys/{authority.run_id}-{key}"
                mapped = tx.get(key_path)
                budget_path = f"{base}/tool_budgets/{authority.run_id}"
                budget = tx.get(budget_path)
                if mapped is not None and mapped != {
                    "tool_call_id": request.tool_call_id,
                    "fingerprint": digest,
                }:
                    raise ToolRejected("conflict")
                if saved is not None:
                    if saved["fingerprint"] != digest:
                        raise ToolRejected("conflict")
                    if Authority.model_validate(saved["authority"]) != authority:
                        raise ToolRejected("permission_denied")
                    return Prepared(authority, saved)
                if reconcile:
                    raise ToolRejected("conflict")
                used = 0 if budget is None else budget["used"]
                if budget is not None and budget["limit"] != authority.max_calls:
                    raise ToolRejected("permission_denied")
                if used >= authority.max_calls:
                    raise ToolRejected("rate_limited")
                # All authorization, dedupe and budget reads precede all writes.
                tx.create(
                    path,
                    {
                        "tool_call_id": request.tool_call_id,
                        "effect_id": identity,
                        "name": registration.name,
                        "fingerprint": digest,
                        "authority": authority.model_dump(mode="json"),
                        "state": "started",
                        "result": None,
                    },
                )
                tx.create(key_path, {"tool_call_id": request.tool_call_id, "fingerprint": digest})
                tx.set(budget_path, {"used": used + 1, "limit": authority.max_calls})
                self._audit(tx, request, identity, "started", registration.name)
                return Prepared(authority, None)

            # An ambiguous start commit is conservatively unknown; there is no I/O yet.
            started = True
            prepared = self.store.run(prepare)
            current = prepared.authority
            replay_unknown = False
            if prepared.existing is not None:
                previous = prepared.existing.get("result")
                if previous is not None:
                    result = ToolResult.model_validate(previous)
                    if not reconcile or result.status != "outcome_unknown":
                        self.store.run(lambda tx: self._check(tx, request, arguments, current))
                        if self._stopped(control) is not None:
                            return self._unknown(request, identity, registration)
                        return result
                if not reconcile:
                    replay_unknown = True
            # A second current transaction immediately precedes I/O or waiting result.
            self.store.run(lambda tx: self._check(tx, request, arguments, current))
            if replay_unknown or self._stopped(control) is not None:
                outcome = HandlerResult(status="outcome_unknown")
            elif current.confirmation == "waiting":
                outcome = HandlerResult(
                    status="waiting_confirmation", proposal_id=current.proposal_id
                )
            else:
                if current.confirmation == "rejected":
                    raise ToolRejected("permission_denied")
                invocation = Invocation(
                    context=current.context,
                    arguments=arguments.model_copy(deep=True),
                    tool_call_id=request.tool_call_id,
                    effect_id=identity,
                    confirmation=current.confirmation,
                )
                try:
                    method = (
                        registration.handler.reconcile
                        if reconcile
                        else registration.handler.execute
                    )
                    outcome = await method(invocation, control=control)
                    if (
                        reconcile
                        and outcome.status == "failed"
                        and outcome.error_code in ("not_found", "unavailable")
                    ):
                        # Absence/temporary lookup failure cannot prove an in-flight
                        # original effect will never complete.
                        outcome = HandlerResult(status="outcome_unknown")
                    if self._stopped(control) is not None:
                        outcome = HandlerResult(status="outcome_unknown")
                    result = correlate(
                        outcome, request.tool_call_id, identity, registration.effect_kind
                    )
                except Exception:
                    # Includes malformed/unexpected handler return values and response loss.
                    outcome = HandlerResult(status="outcome_unknown")
            result = correlate(outcome, request.tool_call_id, identity, registration.effect_kind)

            def finish(tx: Transaction) -> ToolResult:
                self._check(tx, request, arguments, current)
                saved = tx.get(path)
                if saved is None or saved["fingerprint"] != digest:
                    raise ToolRejected("conflict")
                if saved["result"] is not None:
                    existing = ToolResult.model_validate(saved["result"])
                    if existing.status != "outcome_unknown":
                        return existing
                tx.set(
                    path, {**saved, "state": "finished", "result": result.model_dump(mode="json")}
                )
                self._audit(tx, request, identity, result.status, registration.name)
                return result

            result = self.store.run(finish)
            self.store.run(lambda tx: self._check(tx, request, arguments, current))
            if self._stopped(control) is not None:
                return self._unknown(request, identity, registration)
            return result
        except ToolRejected as exc:
            code = exc.code
            # Fixed rejection category only; never expose the candidate name/body.
            try:
                self.store.run(
                    lambda tx: self._audit(
                        tx,
                        request,
                        identity,
                        code,
                        None if registration is None else registration.name,
                    )
                )
            except Exception:
                pass
            return Failed(code=exc.code)
        except Exception:
            if started and registration is not None:
                return self._unknown(request, identity, registration)
            return Failed(code="unavailable", retryable=True)

    @staticmethod
    def _unknown(
        request: ToolExecution, identity: ResourceId, registration: Registration[Any]
    ) -> ToolResult:
        return correlate(
            HandlerResult(status="outcome_unknown"),
            request.tool_call_id,
            identity,
            registration.effect_kind,
        )
