"""A real policy authorizer and synthetic confirmation effect, shared with emulator tests."""

import json
from collections.abc import AsyncGenerator, Callable
from uuid import UUID, uuid5

from brainoid_agent_core.ports import CallControl
from brainoid_agent_core.types import (
    Completed,
    ModelEvent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextDelta,
    TextPart,
    ToolCall,
    ToolExecution,
    ToolReply,
    ToolRequested,
    ToolResult,
)
from brainoid_contracts import ResourceId
from brainoid_contracts.transactions import Transaction
from brainoid_jobs import Jobs
from brainoid_jobs.models import job_id
from brainoid_policy.accounts import fingerprint
from brainoid_policy.models import Action, ResourceKind, ResourceRef
from brainoid_runs.state import KIND
from brainoid_tools import (
    Arguments,
    Authority,
    HandlerResult,
    Invocation,
    Registration,
    Registry,
    ResultConsumer,
    ResultSource,
    ResultUse,
    ToolRejected,
)
from brainoid_worker.resume_run import ExternalClaim


class ArgumentsForTest(Arguments):
    note_id: ResourceId
    expected_version: int


class ConfirmationModel:
    def __init__(self, note: ResourceId) -> None:
        self.call = ToolCall(
            call_key="opaque-call",
            name="notes.delete",
            arguments_json=json.dumps({"note_id": note, "expected_version": 1}),
        )
        self.requests: list[ModelRequest] = []
        self.final_text = "FIXTURE_完了。"
        self.signed = True
        self.prefix = "FIXTURE_確認します。"
        self.hook: Callable[[], None] = lambda: None

    async def stream(
        self, request: ModelRequest, *, control: CallControl
    ) -> AsyncGenerator[ModelEvent]:
        self.requests.append(request)
        self.hook()
        continued = any(m.role == "tool" for m in request.messages)
        text = self.final_text if continued else self.prefix
        yield TextDelta(text=text)
        if continued:
            message = ModelMessage(
                role="assistant",
                parts=(TextPart(text=text),),
                provider_state={"signature": "PRIVATE_SIGNATURE_FINAL"},
            )
        else:
            yield ToolRequested(call=self.call)
            message = ModelMessage(
                role="assistant",
                parts=(TextPart(text=text), self.call),
                provider_state={"signature": "PRIVATE_SIGNATURE_CALL"},
            )
        if self.signed:
            yield ModelResponse(message=message)
        yield Completed(finish_reason="stop" if continued else "tool_calls")


class EchoResultModel(ConfirmationModel):
    """Expose any accepted result text verbatim, to detect audience leaks at the boundary."""

    async def stream(
        self, request: ModelRequest, *, control: CallControl
    ) -> AsyncGenerator[ModelEvent]:
        replies = [message for message in request.messages if message.role == "tool"]
        if not replies:
            async for event in super().stream(request, control=control):
                yield event
            return
        self.requests.append(request)
        reply = replies[-1].parts[0]
        assert isinstance(reply, ToolReply)
        payload = reply.result["result"]
        assert isinstance(payload, dict) and isinstance(payload["text"], str)
        text = payload["text"]
        yield TextDelta(text=text)
        yield ModelResponse(message=ModelMessage(role="assistant", parts=(TextPart(text=text),)))
        yield Completed()


class ConfirmationAuthority:
    def __init__(self, jobs: Jobs, run_id: ResourceId) -> None:
        self.jobs, self.run_id = jobs, run_id
        self.waiting = True

    def check(self, tx: Transaction, request: ToolExecution, arguments: Arguments) -> Authority:
        if not isinstance(arguments, ArgumentsForTest):
            raise ToolRejected("permission_denied")
        job = self.jobs._read(
            tx, request.context.tenant_id, job_id(request.context.tenant_id, KIND, self.run_id)
        )
        assert job is not None
        grant = self.jobs._policy(tx).resource(
            request.context.tenant_id,
            job.grant.principal.subject,
            ResourceRef(
                tenant_id=request.context.tenant_id, kind=ResourceKind.NOTE, id=arguments.note_id
            ),
            action=Action.WRITE,
            output=job.output,
        )
        if grant.resource.version != arguments.expected_version:
            raise ToolRejected("permission_denied")
        return Authority(
            context=request.context,
            run_id=self.run_id,
            receipt=fingerprint([grant.resource.model_dump(mode="json"), grant.principal.stamp]),
            allowed_tools=frozenset({"notes.delete"}),
            max_calls=4,
            confirmation="waiting" if self.waiting else "not_required",
            proposal_id=ResourceId(str(uuid5(UUID(request.tool_call_id), "proposal")))
            if self.waiting
            else None,
        )

    def check_result(
        self,
        tx: Transaction,
        request: ToolExecution,
        arguments: Arguments,
        result: ToolResult,
        *,
        consumer: ResultConsumer,
    ) -> ResultUse:
        if not isinstance(arguments, ArgumentsForTest):
            raise ToolRejected("permission_denied")
        current_job = self.jobs._read(
            tx,
            consumer.context.tenant_id,
            job_id(consumer.context.tenant_id, KIND, consumer.run_id),
        )
        if current_job is None or current_job.grant.context != consumer.context:
            raise ToolRejected("permission_denied")
        grant = self.jobs._policy(tx).resource(
            consumer.context.tenant_id,
            current_job.grant.principal.subject,
            ResourceRef(
                tenant_id=consumer.context.tenant_id, kind=ResourceKind.NOTE, id=arguments.note_id
            ),
            action=Action.MODEL_READ,
            output=current_job.output,
        )
        if grant.resource.version != arguments.expected_version:
            raise ToolRejected("permission_denied")
        if (
            consumer.conversation_id != current_job.grant.ref.id
            or consumer.conversation_scope != current_job.grant.scope
            or consumer.conversation_owner_user_id != current_job.grant.owner_user_id
        ):
            raise ToolRejected("permission_denied")
        return ResultUse(
            receipt=fingerprint(
                [
                    grant.resource.model_dump(mode="json"),
                    grant.principal.stamp,
                    grant.output_stamp,
                    self.jobs._accounts(tx, grant, current_job.output),
                ]
            ),
            sources=(ResultSource(scope=grant.scope, owner_user_id=grant.owner_user_id),),
        )


class ExternalBusinessQueue:
    """Same-transaction business target check/outbox; no external I/O in prepare."""

    def __init__(self, registry: Registry, authority: ConfirmationAuthority) -> None:
        self.registry, self.authority = registry, authority

    def prepare(self, tx: Transaction, request: ToolExecution, claim: ExternalClaim) -> None:
        _, arguments, _ = self.registry.validate(request.call)
        self.authority.check(tx, request, arguments)
        tx.create(
            f"tenants/{claim.tenant_id}/fixture_external_jobs/{claim.effect_id}",
            {
                "claim": claim.model_dump(mode="json"),
                "status": "pending",
            },
        )


class ConfirmationEffect:
    def __init__(self) -> None:
        self.calls: list[ResourceId] = []
        self.hook: Callable[[], None] = lambda: None
        self.registry = Registry()
        self.registry.register(Registration("notes.delete", ArgumentsForTest, self))

    async def execute(
        self, invocation: Invocation[ArgumentsForTest], *, control: CallControl
    ) -> HandlerResult:
        self.calls.append(invocation.effect_id)
        self.hook()
        return HandlerResult(status="succeeded", result={"fixture_effect": "done"})

    async def reconcile(
        self, invocation: Invocation[ArgumentsForTest], *, control: CallControl
    ) -> HandlerResult:
        return HandlerResult(status="succeeded", result={"fixture_effect": "done"})


class ProposalCreatingEffect(ConfirmationEffect):
    """A business handler that durably proposes, without executing the proposed action."""

    def __init__(self, jobs: Jobs) -> None:
        super().__init__()
        self.jobs = jobs

    async def execute(
        self, invocation: Invocation[ArgumentsForTest], *, control: CallControl
    ) -> HandlerResult:
        proposal = ResourceId(str(uuid5(UUID(invocation.tool_call_id), "business-proposal")))

        def create(tx: Transaction) -> None:
            path = f"tenants/{invocation.context.tenant_id}/fixture_business_proposals/{proposal}"
            saved = tx.get(path)
            tx.set(
                path,
                {
                    "id": proposal,
                    "tool_call_id": invocation.tool_call_id,
                    "effect_id": invocation.effect_id,
                    "created_count": 1 if saved is None else saved["created_count"] + 1,
                },
            )

        self.jobs.store.run(create)
        self.calls.append(invocation.effect_id)
        return HandlerResult(status="waiting_confirmation", proposal_id=proposal)


class MultiStageModel:
    def __init__(self, calls: tuple[ToolCall, ...]) -> None:
        self.calls = calls
        self.requests: list[ModelRequest] = []

    async def stream(
        self, request: ModelRequest, *, control: CallControl
    ) -> AsyncGenerator[ModelEvent]:
        self.requests.append(request)
        step = sum(message.role == "tool" for message in request.messages)
        calls = () if step >= len(self.calls) else (self.calls[step],)
        text = f"FIXTURE_stage_{step}."
        yield TextDelta(text=text)
        for call in calls:
            yield ToolRequested(call=call)
        yield ModelResponse(
            message=ModelMessage(
                role="assistant",
                parts=(TextPart(text=text), *calls),
                provider_state={"signature": f"PRIVATE_STAGE_{step}"},
            )
        )
        yield Completed(finish_reason="tool_calls" if calls else "stop")
