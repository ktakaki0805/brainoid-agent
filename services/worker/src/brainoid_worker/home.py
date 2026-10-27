"""Run-bound command admission and a fixed acceptance acknowledgement."""

import json
from collections.abc import Callable
from typing import TYPE_CHECKING
from uuid import UUID, uuid5

from brainoid_agent_core.ports import CallControl
from brainoid_agent_core.types import ToolCall, ToolExecution, ToolResult
from brainoid_contracts import ResourceId, Scope
from brainoid_contracts.access import fingerprint
from brainoid_contracts.reuse import direct_user
from brainoid_contracts.transactions import Document, Transaction
from brainoid_jobs import Job
from brainoid_memory.commands import CommandCreate, CommandRecord, Commands, command_path
from brainoid_memory.repository import FactTransaction
from brainoid_policy.scopes import PolicyDenied, PolicyUnavailable
from brainoid_runs.state import conversation_path, run_path
from brainoid_tools import Authority, ResultConsumer, ResultSource, ResultUse, ToolRejected
from brainoid_tools.home import SetStateArguments
from brainoid_tools.registry import Invocation
from brainoid_tools.results import HandlerResult
from brainoid_tools.reuse import receipt
from pydantic import ValidationError

from brainoid_worker.memory_erasure import capture
from brainoid_worker.run_event_writer import memory_write

if TYPE_CHECKING:
    from brainoid_worker.memory import MemoryRuntime


class HomeRuntime:
    update_marker = "home_command"
    UPDATE_ACK = "家電操作を受け付けました。実行結果はまだ確認されていません。"
    NOT_ACCEPTED = "家電操作は受け付けていません。実行結果は確認できません。"

    def __init__(self, memory: "MemoryRuntime") -> None:
        self.memory, self.handler = memory, memory.handler
        self.service = Commands(memory.handler.jobs.store, now=memory.handler.jobs.now)

    @staticmethod
    def parse_intent(text: str) -> SetStateArguments | None:
        """Recognize the admission grammar independently of any model proposal."""
        for prefix in ("Set home state ", "家電の状態を設定 "):
            if not text.startswith(prefix):
                continue
            payload = text[len(prefix) :]
            try:
                arguments = SetStateArguments.model_validate_json(payload)
            except ValidationError:
                return None
            canonical = json.dumps(
                arguments.model_dump(mode="json"), ensure_ascii=False, sort_keys=True
            )
            return arguments if payload == canonical else None
        return None

    @classmethod
    def intent(cls, text: str, arguments: SetStateArguments) -> bool:
        return cls.parse_intent(text) == arguments

    def direct_intent(self, tx: Transaction, job: Job, run: Document) -> bool:
        # RunState has validated the canonical current message and its text in
        # this transaction. Derived user-shaped content never supplies authority.
        message = tx.get(
            conversation_path(job.tenant_id, job.grant.ref.id)
            + "/messages/"
            + run["input_message_id"]
        )
        return (
            message is not None
            and direct_user(message)
            and message.get("text") == run["input_text"]
            and self.parse_intent(run["input_text"]) is not None
        )

    def check(
        self, tx: Transaction, request: ToolExecution, arguments: SetStateArguments
    ) -> Authority:
        try:
            job, run, expected = self.memory._target(tx, request)
            message = tx.get(
                conversation_path(job.tenant_id, job.grant.ref.id)
                + "/messages/"
                + run["input_message_id"]
            )
            if (
                message is None
                or not direct_user(message)
                or not self.intent(expected.user_text, arguments)
            ):
                raise PolicyDenied()
            access = self.memory.open(tx, job)
            entity, _ = self.service.target(access, arguments)
            if (
                Scope(visibility=entity.visibility, display_policy=entity.display_policy)
                != expected.conversation.scope
            ):
                raise PolicyDenied()
            effect = ResourceId(str(uuid5(UUID(request.tool_call_id), "brainoid-tool-effect")))
            if tx.get(command_path(job.tenant_id, effect)) is not None:
                record = self.service.get(access, effect)
                if CommandCreate.model_validate(
                    {k: getattr(record, k) for k in CommandCreate.model_fields}
                ) != CommandCreate.model_validate(arguments.model_dump()):
                    raise PolicyDenied()
            access.finish()
            return Authority(
                context=request.context,
                run_id=job.operation_id,
                receipt=fingerprint(
                    [
                        job.id,
                        job.grant.principal.stamp,
                        job.grant.output_stamp,
                        run["input_message_id"],
                        arguments.model_dump(mode="json"),
                    ]
                ),
                allowed_tools=frozenset(run["agent"]["allowed_tools"]),
                max_calls=64,
                confirmation="not_required",
            )
        except PolicyUnavailable:
            raise
        except PolicyDenied:
            raise ToolRejected("permission_denied") from None

    def terminal_update(self, tx: Transaction, job: Job, run: Document) -> str:
        marker = run.get(self.update_marker)
        if not isinstance(marker, dict) or any(
            run.get(k)
            for k in (
                "memory_records",
                "tool_bindings",
                "continuation",
                "completed_text",
                "streamed_text",
            )
        ):
            raise PolicyDenied()
        call = marker.get("tool_call_id")
        if call not in run.get("tool_call_ids", []):
            raise PolicyDenied()
        effect = ResourceId(str(uuid5(UUID(call), "brainoid-tool-effect")))
        saved = tx.get(f"tenants/{job.tenant_id}/tool_executions/{call}")
        if (
            marker.get("effect_id") != effect
            or saved is None
            or saved.get("name") != "home.set_state"
            or saved.get("effect_id") != effect
            or saved.get("effect_kind") != "command"
            or saved.get("authority", {}).get("run_id") != job.operation_id
            or Authority.model_validate(saved["authority"]).context != job.grant.context
        ):
            raise PolicyDenied()
        access = self.memory.open(tx, job)
        record = self.service.get(access, effect)
        args = SetStateArguments.model_validate(
            {k: getattr(record, k) for k in CommandCreate.model_fields}
        )
        if not self.intent(run["input_text"], args):
            raise PolicyDenied()
        self.audience(record, self.memory.consumer(job))
        access.finish()
        # Immutable acceptance only; later device results cannot turn this ACK into success.
        return fingerprint(
            [
                marker,
                record.id,
                record.created_at.isoformat(),
                job.grant.context.model_dump(mode="json"),
            ]
        )

    async def execute_home(
        self,
        invocation: Invocation[SetStateArguments],
        *,
        control: CallControl,
        reconcile: bool = False,
    ) -> HandlerResult:
        def work() -> HandlerResult:
            def opened(tx: Transaction) -> FactTransaction:
                saved = tx.get(
                    f"tenants/{invocation.context.tenant_id}/tool_executions/{invocation.tool_call_id}"
                )
                if (
                    saved is None
                    or saved.get("effect_id") != invocation.effect_id
                    or saved.get("name") != "home.set_state"
                ):
                    raise PolicyDenied()
                job = self.memory.job(
                    tx, invocation.context.tenant_id, ResourceId(saved["authority"]["run_id"])
                )
                run, _, request, _ = self.handler.state.check(tx, job)
                if (
                    request.context != invocation.context
                    or invocation.tool_call_id not in run["tool_call_ids"]
                ):
                    raise PolicyDenied()
                self.check(
                    tx,
                    ToolExecution(
                        context=invocation.context,
                        tool_call_id=invocation.tool_call_id,
                        idempotency_key=str(invocation.effect_id),
                        call=ToolCall(
                            call_key="home",
                            name="home.set_state",
                            arguments_json=invocation.arguments.model_dump_json(),
                        ),
                    ),
                    invocation.arguments,
                )
                return self.memory.open(tx, job, control)

            if reconcile:
                record = self.service.read(opened, invocation.effect_id)
            else:

                def terminal(tx: Transaction, record: CommandRecord) -> Callable[[], None]:
                    saved = tx.get(
                        f"tenants/{record.tenant_id}/tool_executions/{invocation.tool_call_id}"
                    )
                    assert saved is not None
                    path = run_path(record.tenant_id, ResourceId(saved["authority"]["run_id"]))
                    run = tx.get(path)
                    if run is None or run.get(self.update_marker) is not None:
                        raise PolicyDenied()
                    updated = {
                        **run,
                        "purge_dependencies": capture(run, complete=True).model_dump(mode="json"),
                        self.update_marker: {
                            "tool_call_id": invocation.tool_call_id,
                            "effect_id": record.id,
                        },
                        "memory_records": [],
                        "tool_bindings": [],
                        "continuation": [],
                        "completed_text": "",
                        "streamed_text": "",
                    }
                    return lambda: tx.set(path, updated)

                record = self.service.create(
                    opened,
                    invocation.arguments,
                    str(invocation.effect_id),
                    effect=invocation.effect_id,
                    extra=terminal,
                )
            return HandlerResult(
                status="succeeded", result={"accepted": True, "command_id": record.id}
            )

        return await memory_write(
            self.handler.writer, work, stopped=lambda: self.memory.stopped(control)
        )

    @staticmethod
    def audience(record: CommandRecord, consumer: ResultConsumer) -> None:
        receipt(
            ResultUse(
                receipt="0" * 64,
                sources=(
                    ResultSource(
                        scope=Scope(
                            visibility=record.visibility, display_policy=record.display_policy
                        ),
                        owner_user_id=record.owner_user_id,
                    ),
                ),
            ),
            consumer,
        )

    def check_result(
        self,
        tx: Transaction,
        request: ToolExecution,
        arguments: SetStateArguments,
        result: ToolResult,
        *,
        consumer: ResultConsumer,
    ) -> ResultUse:
        try:
            job = self.memory.job(tx, consumer.context.tenant_id, consumer.run_id)
            self.handler.state.check(tx, job, check_results=False)
            if self.memory.consumer(job) != consumer:
                raise PolicyDenied()
            access = self.memory.open(tx, job)
            if result.status == "succeeded":
                effect = ResourceId(str(uuid5(UUID(request.tool_call_id), "brainoid-tool-effect")))
                if result.command_id != effect or result.operation_id is not None:
                    raise PolicyDenied()
                record = self.service.get(access, effect)
                if result.result != {"accepted": True, "command_id": record.id}:
                    raise PolicyDenied()
                self.audience(record, consumer)
            elif result.result:
                raise PolicyDenied()
            access.finish()
            return ResultUse(
                receipt=fingerprint(
                    [result.model_dump(mode="json"), consumer.model_dump(mode="json")]
                ),
                sources=(
                    ResultSource(
                        scope=consumer.conversation_scope,
                        owner_user_id=consumer.conversation_owner_user_id,
                    ),
                ),
            )
        except PolicyUnavailable:
            raise
        except PolicyDenied:
            raise ToolRejected("permission_denied") from None
