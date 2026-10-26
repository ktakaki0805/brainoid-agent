"""Read-only use of an already committed tool result, separate from execution rights."""

import hashlib
from typing import Annotated, Protocol, Self, runtime_checkable
from uuid import UUID, uuid5

from brainoid_agent_core.types import ToolExecution, ToolResult
from brainoid_agent_core.types.common import CoreValue
from brainoid_contracts import (
    DisplayPolicy,
    FirebaseUid,
    ResourceId,
    Scope,
    TenantContext,
    Visibility,
)
from brainoid_contracts.transactions import Transaction
from pydantic import Field, model_validator

from brainoid_tools.executor import Authority, execution_fingerprint
from brainoid_tools.registry import Arguments, Registry
from brainoid_tools.results import HandlerResult, ToolRejected, correlate


class ResultConsumer(CoreValue):
    """Current server-authorized consuming run, never the old execution principal."""

    run_id: ResourceId
    context: TenantContext
    conversation_id: ResourceId
    conversation_scope: Scope
    conversation_owner_user_id: FirebaseUid | None


class ResultSource(CoreValue):
    """Server-derived disclosure scope of result data, including identifiers/metadata."""

    scope: Scope
    owner_user_id: FirebaseUid | None = None

    @model_validator(mode="after")
    def private_owner(self) -> Self:
        if self.scope.visibility == Visibility.PRIVATE and self.owner_user_id is None:
            raise ValueError("Private result source requires its owner")
        return self


class ResultUse(CoreValue):
    receipt: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    sources: Annotated[tuple[ResultSource, ...], Field(min_length=1, max_length=64)]


class ResultBinding(CoreValue):
    request: ToolExecution = Field(repr=False)
    result: ToolResult = Field(repr=False)


@runtime_checkable
class ResultAuthorizer(Protocol):
    def check_result(
        self,
        tx: Transaction,
        request: ToolExecution,
        arguments: Arguments,
        result: ToolResult,
        *,
        consumer: ResultConsumer,
    ) -> ResultUse:
        """Read-only: current consumer IAM/account/output AND durable conversation audience.

        The worker validates the current consuming run/lease and the immutable old
        execution/result first. Derive the current reader AND output from consumer's
        canonical run/job, not request's former actor. Verify every result source's
        version/deletion/suppress_reuse; self-mutations require atomic operation proof.
        Return current source scopes/owners and a SHA-256 receipt covering all bindings.
        Include every source of result data/identifiers, never scope supplied by a model.
        The common boundary prevents visibility/owner/display widening into the saved
        conversation. The hook must also reject any business-specific source ACL whose
        audience is narrower than that conversation's readers/future display destinations.
        Never invoke I/O or require an old execution lease. Raise ToolRejected on denial.
        Infrastructure errors propagate. Execution check() is not a substitute.
        """
        ...


def verify_result(
    tx: Transaction,
    registry: Registry,
    binding: ResultBinding,
    origin_run: ResourceId,
) -> Arguments:
    request, expected = binding.request, binding.result
    registration, arguments, canonical = registry.validate(request.call)
    identity = ResourceId(str(uuid5(UUID(request.tool_call_id), "brainoid-tool-effect")))
    saved = tx.get(f"tenants/{request.context.tenant_id}/tool_executions/{request.tool_call_id}")
    if saved is None:
        raise ToolRejected("permission_denied")
    authority = Authority.model_validate(saved["authority"])
    checked = correlate(
        HandlerResult(
            status=expected.status,
            result=expected.result,
            error_code=expected.error_code,
            proposal_id=expected.proposal_id,
        ),
        request.tool_call_id,
        identity,
        registration.effect_kind,
    )
    if (
        expected.status not in ("succeeded", "failed")
        or checked != expected
        or saved.get("state") != "finished"
        or saved.get("result") != expected.model_dump(mode="json")
        or saved.get("fingerprint") != execution_fingerprint(request, canonical)
        or saved.get("tool_call_id") != request.tool_call_id
        or saved.get("effect_id") != identity
        or saved.get("name") != request.call.name
        or saved.get("effect_kind") != registration.effect_kind
        or authority.context != request.context
        or authority.run_id != origin_run
    ):
        raise ToolRejected("permission_denied")
    return arguments


def receipt(value: ResultUse, consumer: ResultConsumer) -> str:
    """Do not make authorized private input public through a shared transcript."""
    if not isinstance(value, ResultUse):
        raise ToolRejected("permission_denied")
    try:
        checked = ResultUse.model_validate(value.model_dump()).model_copy(deep=True)
    except (TypeError, ValueError):
        raise ToolRejected("permission_denied") from None
    display = {
        DisplayPolicy.NEVER: 0,
        DisplayPolicy.AUTHENTICATED_SESSION: 1,
        DisplayPolicy.SHARED_DISPLAY: 2,
    }
    destination = consumer.conversation_scope
    for source in checked.sources:
        if source.scope.visibility == Visibility.PRIVATE and (
            destination.visibility != Visibility.PRIVATE
            or source.owner_user_id != consumer.conversation_owner_user_id
        ):
            raise ToolRejected("permission_denied")
        if display[destination.display_policy] > display[source.scope.display_policy]:
            raise ToolRejected("permission_denied")
    return hashlib.sha256(checked.model_dump_json().encode()).hexdigest()
