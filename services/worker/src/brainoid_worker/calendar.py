"""Calendar tools with durable source binding and current reuse/output checks."""

from typing import TYPE_CHECKING

from brainoid_agent_core.ports import CallControl
from brainoid_agent_core.types import ToolExecution, ToolResult
from brainoid_contracts import ResourceId
from brainoid_contracts.access import AuthorizedScope
from brainoid_contracts.errors import APIError, ErrorCode
from brainoid_contracts.transactions import Document, Transaction
from brainoid_jobs import Job
from brainoid_memory.calendar_access import CALENDAR_SCOPE, current_integration
from brainoid_memory.calendar_reader import (
    CalendarPage,
    CalendarQuery,
    CalendarReader,
    integration_stamp,
)
from brainoid_policy.accounts import fingerprint
from brainoid_policy.scopes import PolicyDenied, PolicyUnavailable
from brainoid_runs.state import run_path
from brainoid_tools import Authority, ResultConsumer, ResultSource, ResultUse, ToolRejected
from brainoid_tools.calendar import ListCalendarArguments
from brainoid_tools.registry import Invocation
from brainoid_tools.results import HandlerResult
from brainoid_tools.reuse import receipt
from pydantic import JsonValue

from brainoid_worker.run_event_writer import memory_write

if TYPE_CHECKING:
    from brainoid_worker.memory import MemoryRuntime


class CalendarRuntime:
    def __init__(self, memory: "MemoryRuntime", reader: CalendarReader | None) -> None:
        self.memory, self.reader = memory, reader
        self.handler = memory.handler

    def current(
        self, tx: Transaction, job: Job, args: ListCalendarArguments
    ) -> tuple[Document, AuthorizedScope]:
        self.handler.state.check(tx, job, check_results=False)
        policy = self.handler.jobs._policy(tx)
        principal = policy.principal(job.tenant_id, job.grant.principal.subject)
        record, scope = current_integration(
            tx, policy, principal, args.integration_id, calendar=args.calendar_id
        )
        use = ResultUse(
            receipt=integration_stamp(record),
            sources=(ResultSource(scope=CALENDAR_SCOPE, owner_user_id=principal.user_id),),
        )
        receipt(use, self.memory.consumer(job))
        if job.output is not None:
            output = policy.principal(job.tenant_id, job.output)
            current_integration(tx, policy, output, args.integration_id, calendar=args.calendar_id)
        return record, scope

    def check(
        self, tx: Transaction, request: ToolExecution, args: ListCalendarArguments
    ) -> Authority:
        try:
            job, run, _ = self.memory._target(tx, request)
            record, scope = self.current(tx, job, args)
            return Authority(
                context=request.context,
                run_id=job.operation_id,
                receipt=fingerprint(
                    [scope.binding(), integration_stamp(record), args.model_dump(mode="json")]
                ),
                allowed_tools=frozenset(run["agent"]["allowed_tools"]),
                max_calls=64,
                confirmation="not_required",
            )
        except PolicyUnavailable:
            raise
        except PolicyDenied:
            raise ToolRejected("permission_denied") from None

    async def execute(
        self, invocation: Invocation[ListCalendarArguments], *, control: CallControl
    ) -> HandlerResult:
        if self.reader is None:
            return HandlerResult(status="failed", error_code="unavailable")
        args = invocation.arguments
        binding: list[str] = []

        def authorize(tx: Transaction) -> tuple[Document, AuthorizedScope]:
            saved = tx.get(
                f"tenants/{invocation.context.tenant_id}/tool_executions/{invocation.tool_call_id}"
            )
            if (
                saved is None
                or saved.get("effect_id") != invocation.effect_id
                or saved.get("name") != "calendar.list"
            ):
                raise PolicyDenied()
            authority = Authority.model_validate(saved["authority"])
            if authority.context != invocation.context:
                raise PolicyDenied()
            job = self.memory.job(tx, invocation.context.tenant_id, authority.run_id)
            current, scope = self.current(tx, job, args)
            stamp = integration_stamp(current)
            if binding and binding[0] != stamp:
                raise PolicyDenied()
            if not binding:
                binding.append(stamp)
            return current, scope

        def work() -> HandlerResult:
            assert self.reader is not None
            # Use the same service with the run's guarded store, not a detached API transaction.
            reader = CalendarReader(
                self.handler.jobs.store,
                self.reader.provider,
                self.reader.secrets,
                self.reader.cursors,
                now=self.reader.now,
            )
            query = CalendarQuery.model_validate(args.model_dump(exclude={"integration_id"}))
            page = reader.list(authorize, query)
            record, _ = reader.store.run(authorize)
            source: dict[str, JsonValue] = {
                "integration_id": args.integration_id,
                "calendar_id": args.calendar_id,
                "stamp": integration_stamp(record),
            }

            def retain(tx: Transaction) -> None:
                current, _ = authorize(tx)
                if integration_stamp(current) != source["stamp"]:
                    raise PolicyDenied()
                saved = tx.get(
                    f"tenants/{invocation.context.tenant_id}/tool_executions/{invocation.tool_call_id}"
                )
                assert saved is not None
                run_id = ResourceId(saved["authority"]["run_id"])
                path = run_path(invocation.context.tenant_id, run_id)
                run = tx.get(path)
                assert run is not None
                sources = run.get("calendar_sources", [])
                if source not in sources:
                    sources = [*sources, source]
                tx.set(path, {**run, "calendar_sources": sources})

            reader.store.run(retain)
            return HandlerResult(
                status="succeeded", result={"page": page.model_dump(mode="json"), "source": source}
            )

        try:
            return await memory_write(
                self.handler.writer, work, stopped=lambda: self.memory.stopped(control)
            )
        except APIError as error:
            return HandlerResult(
                status="failed",
                error_code="unavailable"
                if error.code == ErrorCode.TEMPORARILY_UNAVAILABLE
                else "rejected",
            )
        except PolicyUnavailable:
            raise
        except PolicyDenied:
            raise ToolRejected("permission_denied") from None

    async def reconcile(
        self, invocation: Invocation[ListCalendarArguments], *, control: CallControl
    ) -> HandlerResult:
        # There is no stable external operation to reconcile for a read snapshot.
        return HandlerResult(status="failed", error_code="unavailable")

    def check_result(
        self,
        tx: Transaction,
        request: ToolExecution,
        args: ListCalendarArguments,
        result: ToolResult,
        *,
        consumer: ResultConsumer,
    ) -> ResultUse:
        try:
            job = self.memory.job(tx, consumer.context.tenant_id, consumer.run_id)
            if consumer != self.memory.consumer(job):
                raise PolicyDenied()
            record, scope = self.current(tx, job, args)
            if result.status == "succeeded":
                CalendarPage.model_validate(result.result["page"])
                if result.result.get("source") != {
                    "integration_id": args.integration_id,
                    "calendar_id": args.calendar_id,
                    "stamp": integration_stamp(record),
                }:
                    raise PolicyDenied()
            elif result.result:
                raise PolicyDenied()
            return ResultUse(
                receipt=fingerprint([result.model_dump(mode="json"), scope.binding()]),
                sources=(ResultSource(scope=CALENDAR_SCOPE, owner_user_id=scope.owner_user_id),),
            )
        except PolicyUnavailable:
            raise
        except (PolicyDenied, ValueError, KeyError):
            raise ToolRejected("permission_denied") from None
