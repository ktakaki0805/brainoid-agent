"""Run-derived proposal producer. Identity and checkpoint bindings are never model inputs."""

from typing import TYPE_CHECKING
from uuid import UUID, uuid5

from brainoid_agent_core.ports import CallControl
from brainoid_agent_core.types import ToolExecution, ToolResult
from brainoid_contracts import ResourceId
from brainoid_contracts.access import AuthorizedScope, fingerprint
from brainoid_contracts.deletion import purge_epoch
from brainoid_contracts.errors import APIError, ErrorCode
from brainoid_contracts.transactions import Document, Transaction
from brainoid_memory.calendar_access import CALENDAR_SCOPE
from brainoid_memory.calendar_oauth import WRITE
from brainoid_memory.calendar_proposal_access import check_origin
from brainoid_memory.calendar_proposals import CalendarProposals, proposal_path
from brainoid_memory.calendar_reader import CalendarReader
from brainoid_memory.sources import CanonicalSources
from brainoid_policy.scopes import PolicyDenied, PolicyUnavailable
from brainoid_runs.memory_sources import SavedMessages
from brainoid_runs.state import run_path
from brainoid_tools import Authority, ResultConsumer, ResultSource, ResultUse, ToolRejected
from brainoid_tools.calendar import ProposeCalendarArguments
from brainoid_tools.registry import Invocation
from brainoid_tools.results import HandlerResult

from brainoid_worker.calendar import CalendarRuntime
from brainoid_worker.memory_erasure import capture
from brainoid_worker.run_event_writer import memory_write

if TYPE_CHECKING:
    from brainoid_worker.memory import MemoryRuntime


class ProposalRuntime:
    def __init__(self, memory: "MemoryRuntime", reader: CalendarReader | None) -> None:
        self.memory, self.reader, self.handler = memory, reader, memory.handler
        self.calendar = CalendarRuntime(memory, reader)
        self.current = self.calendar.current

    def check(
        self, tx: Transaction, request: ToolExecution, args: ProposeCalendarArguments
    ) -> Authority:
        authority = self.calendar.check(tx, request, args)
        job = self.memory.job(tx, request.context.tenant_id, authority.run_id)
        record, _ = self.current(tx, job, args)
        if WRITE not in record["granted_scopes"]:
            raise ToolRejected("permission_denied")
        return authority

    def bound(
        self, tx: Transaction, invocation: Invocation[ProposeCalendarArguments]
    ) -> tuple[Document, AuthorizedScope]:
        saved = tx.get(
            f"tenants/{invocation.context.tenant_id}/tool_executions/{invocation.tool_call_id}"
        )
        if (
            saved is None
            or saved.get("effect_id") != invocation.effect_id
            or saved.get("name") != "calendar.propose"
        ):
            raise PolicyDenied()
        authority = Authority.model_validate(saved["authority"])
        if authority.context != invocation.context:
            raise PolicyDenied()
        job = self.memory.job(tx, invocation.context.tenant_id, authority.run_id)
        # Canonical current/history/tool validators retain their time guards in tx.
        self.handler.state.check(tx, job)
        return self.current(tx, job, invocation.arguments)

    async def execute(
        self, invocation: Invocation[ProposeCalendarArguments], *, control: CallControl
    ) -> HandlerResult:
        if self.reader is None:
            return HandlerResult(status="failed", error_code="unavailable")

        def work() -> HandlerResult:
            assert self.reader is not None
            service = CalendarProposals(
                CalendarReader(
                    self.handler.jobs.store,
                    self.reader.provider,
                    self.reader.secrets,
                    self.reader.cursors,
                    now=self.reader.now,
                )
            )

            def origin(tx: Transaction) -> Document:
                self.bound(tx, invocation)
                saved = tx.get(
                    f"tenants/{invocation.context.tenant_id}/tool_executions/{invocation.tool_call_id}"
                )
                assert saved is not None
                job = self.memory.job(
                    tx, invocation.context.tenant_id, ResourceId(saved["authority"]["run_id"])
                )
                run, _, _, _ = self.handler.state.check(tx, job, check_results=False)
                version = run.get("checkpoint_version", 0) + 1
                # bound() has validated every current/history/tool input. Strict capture
                # records that verified inventory; it is not an authorization substitute.
                dependencies = [capture(run, complete=True).model_dump(mode="json")]
                for source_id in run.get("history_run_ids", []):
                    source_run = tx.get(run_path(job.tenant_id, ResourceId(source_id)))
                    if source_run is None:
                        raise PolicyDenied()
                    dependencies.append(capture(source_run, complete=True).model_dump(mode="json"))
                return {
                    "run_id": job.operation_id,
                    "tool_call_id": invocation.tool_call_id,
                    "checkpoint_id": str(uuid5(UUID(job.operation_id), f"confirmation:{version}")),
                    "checkpoint_version": version,
                    "origin": {
                        "subject": job.grant.principal.subject.model_dump(mode="json"),
                        "credential": run.get("credential"),
                        "output": job.output.model_dump(mode="json") if job.output else None,
                        "purge_epoch": purge_epoch(tx, job.tenant_id),
                        "dependencies": dependencies,
                        "calendar_sources": run.get("calendar_sources", []),
                    },
                }

            def retain(tx: Transaction, record: Document) -> None:
                path = run_path(invocation.context.tenant_id, record["run_id"])
                run = tx.get(path)
                assert run is not None
                sources = run.get("calendar_sources", [])
                source = record["calendar_source"]
                tx.set(
                    path,
                    {
                        **run,
                        "calendar_sources": sources if source in sources else [*sources, source],
                    },
                )

            record = service.create(
                lambda tx: self.bound(tx, invocation),
                invocation.arguments.change,
                "calendar-propose-" + invocation.effect_id,
                identity=invocation.effect_id,
                provenance=origin,
                verify_origin=lambda tx, record: check_origin(
                    tx,
                    self.handler.jobs._policy(tx),
                    record,
                    now=self.handler.jobs.now,
                    credential_epoch=self.handler.state.credential_epoch,
                    sources=CanonicalSources(SavedMessages()),
                    for_model=True,
                ),
                retain=retain,
            )
            return HandlerResult(
                status="waiting_confirmation", proposal_id=ResourceId(record["id"])
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
        self, invocation: Invocation[ProposeCalendarArguments], *, control: CallControl
    ) -> HandlerResult:
        def read(tx: Transaction) -> HandlerResult:
            self.bound(tx, invocation)
            record = tx.get(proposal_path(invocation.context.tenant_id, invocation.effect_id))
            if record is None:
                return HandlerResult(status="failed", error_code="unavailable")
            check_origin(
                tx,
                self.handler.jobs._policy(tx),
                record,
                now=self.handler.jobs.now,
                credential_epoch=self.handler.state.credential_epoch,
                sources=CanonicalSources(SavedMessages()),
                for_model=True,
            )
            return HandlerResult(status="waiting_confirmation", proposal_id=invocation.effect_id)

        return await memory_write(
            self.handler.writer,
            lambda: self.handler.jobs.store.run(read),
            stopped=lambda: self.memory.stopped(control),
        )

    def check_result(
        self,
        tx: Transaction,
        request: ToolExecution,
        args: ProposeCalendarArguments,
        result: ToolResult,
        *,
        consumer: ResultConsumer,
    ) -> ResultUse:
        try:
            job = self.memory.job(tx, consumer.context.tenant_id, consumer.run_id)
            if consumer != self.memory.consumer(job) or result.result:
                raise PolicyDenied()
            _, scope = self.current(tx, job, args, waiting=job.status == "waiting")
            if result.status == "waiting_confirmation":
                assert result.proposal_id is not None
                record = tx.get(proposal_path(job.tenant_id, result.proposal_id))
                if (
                    record is None
                    or record.get("tool_call_id") != request.tool_call_id
                    or record.get("run_id") != job.operation_id
                    or record["normalized_request"] != args.change.model_dump(mode="json")
                ):
                    raise PolicyDenied()
                check_origin(
                    tx,
                    self.handler.jobs._policy(tx),
                    record,
                    now=self.handler.jobs.now,
                    credential_epoch=self.handler.state.credential_epoch,
                    sources=CanonicalSources(SavedMessages()),
                    for_model=True,
                )
            elif result.status in ("succeeded", "outcome_unknown"):
                from brainoid_memory.forget import operation_path

                if result.operation_id is None:
                    raise PolicyDenied()
                record = tx.get(proposal_path(job.tenant_id, result.operation_id))
                operation = tx.get(operation_path(job.tenant_id, result.operation_id))
                if (
                    record is None
                    or operation is None
                    or record.get("tool_call_id") != request.tool_call_id
                    or record.get("run_id") != job.operation_id
                    or record.get("operation_id") != result.operation_id
                    or record["normalized_request"] != args.change.model_dump(mode="json")
                    or operation.get("args_hash") != record["args_hash"]
                    or operation.get("operation", {}).get("status")
                    not in (
                        ("outcome_unknown", "succeeded")
                        if result.status == "outcome_unknown"
                        else ("succeeded",)
                    )
                ):
                    raise PolicyDenied()
                check_origin(
                    tx,
                    self.handler.jobs._policy(tx),
                    record,
                    now=self.handler.jobs.now,
                    credential_epoch=self.handler.state.credential_epoch,
                    sources=CanonicalSources(SavedMessages()),
                    for_model=True,
                )
            elif result.status != "failed":
                raise PolicyDenied()
            return ResultUse(
                receipt=fingerprint([result.model_dump(mode="json"), scope.binding()]),
                sources=(ResultSource(scope=CALENDAR_SCOPE, owner_user_id=scope.owner_user_id),),
            )
        except PolicyUnavailable:
            raise
        except (PolicyDenied, ValueError, KeyError, APIError):
            raise ToolRejected("permission_denied") from None
