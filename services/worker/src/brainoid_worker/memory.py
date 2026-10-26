"""Current run-bound memory retrieval, mutation, result reuse and history fencing."""

import re
from typing import TYPE_CHECKING
from uuid import UUID, uuid5

from brainoid_agent_core.context import ContextInput
from brainoid_agent_core.ports import CallControl
from brainoid_agent_core.types import (
    Failed,
    MemoryContent,
    MemoryForget,
    MemoryForgetResult,
    MemoryMatches,
    MemoryRecord,
    MemoryRemember,
    MemoryRevise,
    MemorySearch,
    MemorySearchResult,
    MemoryWriteResult,
    ToolExecution,
    ToolResult,
)
from brainoid_contracts import DeviceActorContext, ResourceId, Scope, TenantContext
from brainoid_contracts.transactions import Document, Transaction
from brainoid_jobs import AuthorizationUnavailable, Job
from brainoid_jobs.models import job_id
from brainoid_jobs.repository import path as job_path
from brainoid_memory import CanonicalSources, MemoryRepository, VerifiedCaller
from brainoid_memory.dto import MemoryCreate
from brainoid_memory.models import Fact
from brainoid_memory.mutations import MemoryMutations, effect_path
from brainoid_memory.repository import FactTransaction
from brainoid_memory.retrieval import SearchInput, retrieve
from brainoid_persistence.firestore import PersistenceUnavailable
from brainoid_policy.accounts import fingerprint
from brainoid_policy.models import DeviceSubject
from brainoid_policy.scopes import PolicyDenied, PolicyUnavailable
from brainoid_runs.memory_sources import SavedMessages
from brainoid_runs.state import KIND, run_path
from brainoid_tools import (
    Arguments,
    Authority,
    ResultAuthorizer,
    ResultConsumer,
    ResultSource,
    ResultUse,
    ToolRejected,
)
from brainoid_tools.memory import (
    MemoryArguments,
    RememberArguments,
    ReviseArguments,
    SearchArguments,
)
from brainoid_tools.registry import Invocation
from brainoid_tools.results import HandlerResult
from brainoid_tools.reuse import receipt

from brainoid_worker.run_event_writer import memory_write

if TYPE_CHECKING:
    from brainoid_worker.run_turn import RunTurn


def model_record(record: Fact) -> MemoryRecord:
    content = record.model_dump(exclude_none=True)
    content = {key: value for key, value in content.items() if key in MemoryContent.model_fields}
    content["scope"] = Scope(visibility=record.visibility, display_policy=record.display_policy)
    return MemoryRecord(
        memory_id=record.id,
        tenant_id=record.tenant_id,
        owner_user_id=record.owner_user_id,
        content=MemoryContent.model_validate(content),
        version=record.version,
        recorded_at=record.recorded_at,
        status=record.status,
        deletion_epoch=record.deletion_epoch,
        origin=record.origin,
        created_by=record.created_by,
    )


def audience(record: MemoryRecord, consumer: ResultConsumer) -> None:
    # Reuse the same audience rule as committed tool results. Fact's canonical
    # dependency checks prove its disclosure is no wider than every underlying source.
    try:
        receipt(
            ResultUse(
                receipt="0" * 64,
                sources=(
                    ResultSource(
                        scope=record.content.scope,
                        owner_user_id=record.owner_user_id,
                    ),
                ),
            ),
            consumer,
        )
    except ToolRejected:
        raise PolicyDenied() from None


class RunCaller:
    def __init__(self, runtime: "MemoryRuntime", job: Job) -> None:
        self.runtime, self.job = runtime, job

    def authorize(self, tx: Transaction, context: TenantContext) -> VerifiedCaller:
        handler = self.runtime.handler
        # The immutable canonical job is a bounded delegation. RunState verifies its
        # live lease, account/member/ledger, credential family, session and output.
        _, _, request, _ = handler.state.check(tx, self.job, check_results=False)
        live = Job.model_validate(tx.get(job_path(self.job.tenant_id, self.job.id)))
        if context != request.context or live.lease_expires_at is None:
            raise PolicyDenied()
        deadline = live.lease_expires_at
        if isinstance(self.job.output, DeviceSubject):
            destination = handler.jobs._policy(tx).principal(self.job.tenant_id, self.job.output)
            actor = destination.context.actor
            if isinstance(actor, DeviceActorContext) and actor.private_session is not None:
                deadline = min(
                    deadline,
                    actor.private_session.expires_at,
                    actor.private_session.idle_expires_at,
                )
        run = tx.get(run_path(self.job.tenant_id, self.job.operation_id))
        if run is not None and run.get("credential") is not None:
            family = tx.get("device_credential_families/" + run["credential"]["family_id"])
            if family is None:
                raise PolicyDenied()
            deadline = min(deadline, family["expires_at"])
        return VerifiedCaller(
            subject=self.job.grant.principal.subject, valid_from=live.created_at, valid_to=deadline
        )


class BoundMemory:
    def __init__(self, runtime: "MemoryRuntime", job: Job) -> None:
        self.runtime, self.job = runtime, job
        self.unavailable: Exception | None = None

    async def search(self, request: MemorySearch, *, control: CallControl) -> MemorySearchResult:
        runtime = self.runtime

        def work() -> MemoryMatches:
            def read(tx: Transaction) -> tuple[MemoryMatches, FactTransaction, tuple[Fact, ...]]:
                run, _, expected, _ = runtime.handler.state.check(tx, self.job)
                if request.context != expected.context or request.scopes != expected.memory_scopes:
                    raise PolicyDenied()
                access = runtime.open(tx, self.job, control)
                found = retrieve(
                    access,
                    access.entities(),
                    SearchInput(query=request.query[:2000], limit=request.limit),
                    scopes=request.scopes,
                )
                records = tuple(model_record(f) for f in found.records)
                consumer = runtime.consumer(self.job)
                for record in records:
                    audience(record, consumer)
                known = {
                    MemoryRecord.model_validate(r).memory_id: r
                    for r in run.get("memory_records", [])
                }
                known.update({r.memory_id: r.model_dump(mode="json") for r in records})
                access.finish_records(found.records)
                tx.set(
                    run_path(self.job.tenant_id, self.job.operation_id),
                    {
                        **run,
                        "memory_records": list(known.values()),
                        "memory_trace": found.trace.model_dump(mode="json"),
                    },
                )
                return MemoryMatches(records=records), access, found.records

            result, access, records = runtime.handler.jobs.store.run(read)
            access.finish_records(records)
            return result

        try:
            return await memory_write(
                runtime.handler.writer, work, stopped=lambda: runtime.stopped(control)
            )
        except PolicyUnavailable:
            self.unavailable = AuthorizationUnavailable()
            raise
        except PersistenceUnavailable as error:
            self.unavailable = error
            raise
        except (PolicyDenied, ToolRejected):
            return Failed(code="permission_denied")

    async def remember(self, request: MemoryRemember, *, control: CallControl) -> MemoryWriteResult:
        # Writes require a durable typed tool invocation/effect identity and explicit
        # intent. The Core's automatic retrieval port cannot create those bindings.
        return Failed(code="permission_denied")

    async def revise(self, request: MemoryRevise, *, control: CallControl) -> MemoryWriteResult:
        return Failed(code="permission_denied")

    async def forget(self, request: MemoryForget, *, control: CallControl) -> MemoryForgetResult:
        return Failed(code="permission_denied")


class MemoryRuntime:
    def __init__(self, handler: "RunTurn") -> None:
        self.handler = handler
        self.delegate = handler.tool_authorizer

    def stopped(self, control: CallControl) -> bool:
        return control.cancellation.cancelled or (
            control.deadline is not None and self.handler.clock.monotonic() >= control.deadline
        )

    def open(
        self, tx: Transaction, job: Job, control: CallControl | None = None
    ) -> FactTransaction:
        def finish() -> None:
            if control is not None and self.stopped(control):
                raise PolicyDenied()

        repository = MemoryRepository(
            self.handler.jobs.store,
            authorizer=RunCaller(self, job),
            sources=CanonicalSources(SavedMessages()),
            ledger=self.handler.jobs.ledger,
            now=self.handler.jobs.now,
            allow_untracked_bootstrap=self.handler.jobs.allow_untracked,
            output=job.output,
            before_finish=finish,
        )
        return repository.facts(tx, job.grant.context)

    @staticmethod
    def consumer(job: Job) -> ResultConsumer:
        return ResultConsumer(
            run_id=job.operation_id,
            context=job.grant.context,
            conversation_id=job.grant.ref.id,
            conversation_scope=job.grant.scope,
            conversation_owner_user_id=job.grant.owner_user_id,
        )

    def job(self, tx: Transaction, tenant: ResourceId, run: ResourceId) -> Job:
        doc = tx.get(job_path(tenant, job_id(tenant, KIND, run)))
        if doc is None:
            raise PolicyDenied()
        return Job.model_validate(doc)

    def validate_records(
        self,
        tx: Transaction,
        job: Job,
        records: tuple[MemoryRecord, ...],
        *,
        origin: ResourceId,
    ) -> None:
        if not records:
            return
        access = self.open(tx, job)
        consumer = self.consumer(job)
        for record in records:
            audience(record, consumer)
            try:
                current = access.get(record.memory_id, for_model=True)
            except PolicyUnavailable:
                raise
            except PolicyDenied:
                self._superseded(tx, access, record, origin)
            else:
                if model_record(current) != record:
                    raise PolicyDenied()
        access.finish()

    def _superseded(
        self, tx: Transaction, access: FactTransaction, record: MemoryRecord, origin: ResourceId
    ) -> None:
        """Historical input of THIS run's atomic revision, never a current model fact."""
        current = access.revision_input(record.memory_id)
        if current.status != "superseded" or current.version != record.version + 1:
            raise PolicyDenied()
        origin_run = tx.get(run_path(record.tenant_id, origin))
        if origin_run is None:
            raise PolicyDenied()
        for call_id in origin_run.get("tool_call_ids", []):
            effect = ResourceId(str(uuid5(UUID(call_id), "brainoid-tool-effect")))
            proof = tx.get(effect_path(record.tenant_id, effect))
            execution = tx.get(f"tenants/{record.tenant_id}/tool_executions/{call_id}")
            if proof is None or execution is None or execution.get("name") != "memory.revise":
                continue
            if (
                execution["authority"]["run_id"] != origin
                or proof.get("predecessor") != record.memory_id
            ):
                continue
            if proof.get("previous") != current.model_dump(mode="json", exclude_none=True):
                continue
            if (
                model_record(current).model_copy(
                    update={"status": record.status, "version": record.version}
                )
                != record
            ):
                continue
            replacement = access.get(effect, for_model=True)
            if proof.get("record") != {
                **replacement.model_dump(mode="json", exclude_none=True),
                "owner_user_id": replacement.owner_user_id,
            }:
                continue
            access.finish_records((replacement,))
            return
        raise PolicyDenied()

    def check_sources(
        self, tx: Transaction, job: Job, run: Document, history: list[Document]
    ) -> None:
        self.validate_records(
            tx,
            job,
            tuple(MemoryRecord.model_validate(r) for r in run.get("memory_records", [])),
            origin=job.operation_id,
        )
        for message in history:
            if message.get("role") != "assistant":
                continue
            origin = ResourceId(message["run_id"])
            origin_run = tx.get(run_path(job.tenant_id, origin))
            snapshots = message.get("memory_records", [])
            if snapshots or (origin_run is not None and origin_run.get("memory_records")):
                if (
                    origin_run is None
                    or origin_run.get("status") != "completed"
                    or origin_run.get("final_message_id") != message.get("id")
                    or origin_run.get("memory_records", []) != snapshots
                ):
                    raise PolicyDenied()
                self.validate_records(
                    tx, job, tuple(MemoryRecord.model_validate(r) for r in snapshots), origin=origin
                )

    def _target(
        self, tx: Transaction, request: ToolExecution
    ) -> tuple[Job, Document, ContextInput]:
        saved = tx.get(
            f"tenants/{request.context.tenant_id}/tool_executions/{request.tool_call_id}"
        )
        # First reservation has no tool execution yet; canonical run owns this ID.
        matches = []
        for _, run in tx.collection(f"tenants/{request.context.tenant_id}/runs"):
            if request.tool_call_id in run.get("tool_call_ids", []):
                matches.append(run)
        if len(matches) != 1:
            raise PolicyDenied()
        run = matches[0]
        job = self.job(tx, request.context.tenant_id, ResourceId(run["run_id"]))
        current, _, expected, _ = self.handler.state.check(tx, job, check_results=False)
        if expected.context != request.context or (
            saved is not None and saved["authority"]["run_id"] != job.operation_id
        ):
            raise PolicyDenied()
        return job, current, expected

    @staticmethod
    def intent(text: str, revise: bool) -> bool:
        """Conservative explicit-command grammar from the canonical USER message.

        A keyword in a question, quote, negation or reference data proves nothing.
        Broader natural-language intent needs a separately confirmed user workflow.
        """
        if re.search(
            r"[\n\r\"'“”‘’「」『』?？]|\b(?:not|never|don't|meaning|word|explain|definition)\b|しない|するな|ないで|意味|単語|という|教えて",
            text,
            re.I,
        ):
            return False
        pattern = (
            r"(?:please )?(?:correct|revise|update|change) the memory: .+|記憶を(?:訂正|修正|更新)して[:：] ?.+"
            if revise
            else r"(?:please )?(?:remember|save) that .+|(?:記憶|保存)して[:：] ?.+"
        )
        return re.fullmatch(pattern, text.strip(), re.I) is not None

    def check(self, tx: Transaction, request: ToolExecution, arguments: Arguments) -> Authority:
        if not request.call.name.startswith("memory."):
            if self.delegate is None:
                raise ToolRejected("permission_denied")
            return self.delegate.check(tx, request, arguments)
        try:
            job, run, expected = self._target(tx, request)
            access = self.open(tx, job)
            if isinstance(arguments, SearchArguments):
                access.scopes()
            elif isinstance(arguments, (RememberArguments, ReviseArguments)):
                revise = isinstance(arguments, ReviseArguments)
                if not self.intent(expected.user_text, revise):
                    raise PolicyDenied()
                body = (
                    arguments.replacement if isinstance(arguments, ReviseArguments) else arguments
                )
                scope = Scope(visibility=body.visibility, display_policy=body.display_policy)
                if scope != expected.conversation.scope:
                    raise PolicyDenied()
                if (
                    access.principal.user_id is None
                    or "memory.write" not in access.principal.context.actor.allowed_scopes
                ):
                    raise PolicyDenied()
                effect = ResourceId(str(uuid5(UUID(request.tool_call_id), "brainoid-tool-effect")))
                proof = tx.get(effect_path(job.tenant_id, effect))
                if proof is not None:
                    self._proof(access, proof, arguments, effect)
                elif isinstance(arguments, ReviseArguments):
                    previous = access.get(arguments.memory_id, write=True, for_model=True)
                    if (
                        previous.version != arguments.expected_version
                        or previous.status != "confirmed"
                    ):
                        raise ToolRejected("conflict")
            else:
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

    def _proof(
        self, access: FactTransaction, proof: Document, arguments: Arguments, effect: ResourceId
    ) -> Fact:
        if not isinstance(arguments, (RememberArguments, ReviseArguments)):
            raise PolicyDenied()
        body = arguments.replacement if isinstance(arguments, ReviseArguments) else arguments
        current = access.get(effect, for_model=True)
        if (
            proof.get("effect_id") != effect
            or proof.get("request") != body.model_dump(mode="json", exclude_none=True)
            or proof.get("record")
            != {
                **current.model_dump(mode="json", exclude_none=True),
                "owner_user_id": current.owner_user_id,
            }
        ):
            raise PolicyDenied()
        if proof.get("predecessor") != (
            arguments.memory_id if isinstance(arguments, ReviseArguments) else None
        ) or proof.get("expected_version") != (
            arguments.expected_version if isinstance(arguments, ReviseArguments) else None
        ):
            raise PolicyDenied()
        if isinstance(arguments, ReviseArguments):
            old = access.get(arguments.memory_id)
            if (
                old.status != "superseded"
                or old.version != arguments.expected_version + 1
                or proof.get("previous") != old.model_dump(mode="json", exclude_none=True)
            ):
                raise PolicyDenied()
        access.finish_records((current,))
        return current

    async def execute_memory(
        self,
        name: str,
        invocation: Invocation[MemoryArguments],
        *,
        control: CallControl,
        reconcile: bool = False,
    ) -> HandlerResult:
        def work() -> HandlerResult:
            # The execution marker was written by Executor using its current run check.
            def opened(tx: Transaction) -> FactTransaction:
                saved = tx.get(
                    f"tenants/{invocation.context.tenant_id}/tool_executions/{invocation.tool_call_id}"
                )
                if (
                    saved is None
                    or saved.get("effect_id") != invocation.effect_id
                    or saved.get("name") != name
                ):
                    raise PolicyDenied()
                job = self.job(
                    tx, invocation.context.tenant_id, ResourceId(saved["authority"]["run_id"])
                )
                run, _, expected, _ = self.handler.state.check(tx, job)
                if invocation.context != expected.context or invocation.tool_call_id not in run.get(
                    "tool_call_ids", []
                ):
                    raise PolicyDenied()
                return self.open(tx, job, control)

            args = invocation.arguments
            if isinstance(args, SearchArguments):
                if reconcile:
                    return HandlerResult(status="outcome_unknown")

                def search(
                    tx: Transaction,
                ) -> tuple[HandlerResult, FactTransaction, tuple[Fact, ...]]:
                    access = opened(tx)
                    execution = tx.get(
                        f"tenants/{invocation.context.tenant_id}/tool_executions/{invocation.tool_call_id}"
                    )
                    assert execution is not None
                    job = self.job(
                        tx,
                        invocation.context.tenant_id,
                        ResourceId(execution["authority"]["run_id"]),
                    )
                    _, _, expected, _ = self.handler.state.check(tx, job)
                    found = retrieve(
                        access,
                        access.entities(),
                        SearchInput.model_validate(args.model_dump()),
                        scopes=expected.memory_scopes,
                    )
                    records = tuple(model_record(f) for f in found.records)
                    for record in records:
                        audience(record, self.consumer(job))
                    return (
                        HandlerResult(
                            status="succeeded",
                            result={
                                "memories": [r.model_dump(mode="json") for r in records],
                                "trace": found.trace.model_dump(mode="json"),
                            },
                        ),
                        access,
                        found.records,
                    )

                result, access, records = self.handler.jobs.store.run(search)
                access.finish_records(records)
                return result
            if reconcile:

                def read(tx: Transaction) -> Fact:
                    access = opened(tx)
                    proof = tx.get(effect_path(invocation.context.tenant_id, invocation.effect_id))
                    if proof is None:
                        raise PolicyDenied()
                    return self._proof(access, proof, args, invocation.effect_id)

                record = self.handler.jobs.store.run(read)
            else:
                body = args.replacement if isinstance(args, ReviseArguments) else args
                record = MemoryMutations(
                    self.handler.jobs.store, self.handler.jobs, now=self.handler.jobs.now
                ).mutate(
                    opened,
                    MemoryCreate.model_validate(body.model_dump(exclude_none=True)),
                    str(invocation.effect_id),
                    predecessor=args.memory_id if isinstance(args, ReviseArguments) else None,
                    expected=args.expected_version if isinstance(args, ReviseArguments) else None,
                    effect=invocation.effect_id,
                )
            return HandlerResult(
                status="succeeded",
                result={"memories": [model_record(record).model_dump(mode="json")]},
            )

        return await memory_write(self.handler.writer, work, stopped=lambda: self.stopped(control))

    def check_result(
        self,
        tx: Transaction,
        request: ToolExecution,
        arguments: Arguments,
        result: ToolResult,
        *,
        consumer: ResultConsumer,
    ) -> ResultUse:
        if not request.call.name.startswith("memory."):
            if not isinstance(self.delegate, ResultAuthorizer):
                raise ToolRejected("permission_denied")
            return self.delegate.check_result(tx, request, arguments, result, consumer=consumer)
        try:
            job = self.job(tx, consumer.context.tenant_id, consumer.run_id)
            self.handler.state.check(tx, job, check_results=False)
            if self.consumer(job) != consumer:
                raise PolicyDenied()
            access = self.open(tx, job)
            raw_records = result.result.get("memories", [])
            if not isinstance(raw_records, list):
                raise PolicyDenied()
            records = tuple(MemoryRecord.model_validate(r) for r in raw_records)
            if result.status == "succeeded":
                if isinstance(arguments, (RememberArguments, ReviseArguments)):
                    effect = result.operation_id
                    if effect is None:
                        raise PolicyDenied()
                    proof = tx.get(effect_path(job.tenant_id, effect))
                    if proof is None or records != (
                        model_record(self._proof(access, proof, arguments, effect)),
                    ):
                        raise PolicyDenied()
                else:
                    saved = tx.get(
                        f"tenants/{job.tenant_id}/tool_executions/{request.tool_call_id}"
                    )
                    if saved is None:
                        raise PolicyDenied()
                    self.validate_records(
                        tx, job, records, origin=ResourceId(saved["authority"]["run_id"])
                    )
            elif result.result:
                raise PolicyDenied()
            for record in records:
                audience(record, consumer)
            access.finish()
            return ResultUse(
                receipt=fingerprint(
                    [result.model_dump(mode="json"), consumer.model_dump(mode="json")]
                ),
                sources=tuple(
                    ResultSource(scope=r.content.scope, owner_user_id=r.owner_user_id)
                    for r in records
                )
                or (
                    ResultSource(
                        scope=consumer.conversation_scope,
                        owner_user_id=consumer.conversation_owner_user_id,
                    ),
                ),
            )
        except PolicyUnavailable:
            raise
        except (PolicyDenied, ValueError):
            raise ToolRejected("permission_denied") from None
