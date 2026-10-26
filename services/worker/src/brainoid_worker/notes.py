"""Run-bound note tools with exact direct-user intent and current result authority."""

import json
from collections.abc import Callable
from typing import TYPE_CHECKING
from uuid import UUID, uuid5

from brainoid_agent_core.ports import CallControl
from brainoid_agent_core.types import ToolExecution, ToolResult
from brainoid_contracts import ResourceId, Scope
from brainoid_contracts.reuse import direct_user
from brainoid_contracts.transactions import Document, Transaction
from brainoid_jobs import Job
from brainoid_memory.forget import DeleteNote, Operation, operation_path
from brainoid_memory.notes import Note, NoteCreate, NotePatch, Notes, effect_path, view
from brainoid_memory.repository import FactTransaction
from brainoid_policy.accounts import fingerprint
from brainoid_policy.scopes import PolicyDenied, PolicyUnavailable
from brainoid_runs.state import conversation_path, run_path
from brainoid_tools import (
    Arguments,
    Authority,
    ResultConsumer,
    ResultSource,
    ResultUse,
    ToolRejected,
)
from brainoid_tools.notes import (
    CreateNoteArguments,
    DeleteNoteArguments,
    ListNotesArguments,
    NoteArguments,
    UpdateNoteArguments,
)
from brainoid_tools.registry import Invocation
from brainoid_tools.results import HandlerResult
from brainoid_tools.reuse import receipt
from pydantic import TypeAdapter

from brainoid_worker.memory_erasure import NoteDependency, capture
from brainoid_worker.run_event_writer import memory_write

if TYPE_CHECKING:
    from brainoid_worker.memory import MemoryRuntime


class NotesRuntime:
    UPDATE_ACK = "ノートを更新しました。"

    def __init__(self, memory: "MemoryRuntime") -> None:
        self.memory = memory
        self.handler = memory.handler
        self.service = Notes(self.handler.jobs.store, now=self.handler.jobs.now)

    @staticmethod
    def owns(arguments: Arguments) -> bool:
        return isinstance(
            arguments,
            (CreateNoteArguments, ListNotesArguments, UpdateNoteArguments, DeleteNoteArguments),
        )

    def terminal_update(self, tx: Transaction, job: Job, run: Document) -> str:
        """Reauthorize an atomic update acknowledgement, never historical model input."""
        marker = run.get("note_update")
        if not isinstance(marker, dict) or any(
            run.get(key)
            for key in (
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
        proof = tx.get(effect_path(job.tenant_id, effect))
        if (
            marker.get("effect_id") != effect
            or saved is None
            or proof is None
            or saved.get("name") != "notes.update"
            or saved.get("effect_id") != effect
            or saved.get("tool_call_id") != call
            or saved.get("authority", {}).get("run_id") != job.operation_id
            or Authority.model_validate(saved["authority"]).context != job.grant.context
        ):
            raise PolicyDenied()
        args = UpdateNoteArguments(
            note_id=marker["target_id"],
            expected_version=marker["expected_version"],
            changes=NotePatch.model_validate(proof["request"]),
        )
        if not self.intent(run["input_text"], args):
            raise PolicyDenied()
        access = self.memory.open(tx, job)
        note = self.service.proof(access, effect, args.changes, args.note_id, args.expected_version)
        self.service.scope(
            tx, access, Scope(visibility=note.visibility, display_policy=note.display_policy)
        )
        self.audience(note, self.memory.consumer(job))
        access.finish()
        return fingerprint([marker, proof["record"], job.grant.context.model_dump(mode="json")])

    @staticmethod
    def delete_body(args: DeleteNoteArguments) -> DeleteNote:
        return DeleteNote(
            kind="delete_resource",
            target_type="note",
            target_id=args.note_id,
            expected_version=args.expected_version,
        )

    @staticmethod
    def intent(text: str, args: Arguments) -> bool:
        # Match the full direct user instruction, including actual title/body and
        # requested scope. Retrieved/model text cannot establish these bindings.
        if isinstance(args, CreateNoteArguments):
            return (
                text == f"Save {args.visibility} note {args.title}: {args.body}"
                or text == f"{args.visibility}ノートを保存 {args.title}: {args.body}"
            )
        if isinstance(args, UpdateNoteArguments):
            payload = json.dumps(
                args.changes.model_dump(mode="json", exclude_unset=True),
                ensure_ascii=False,
                sort_keys=True,
            )
            return (
                text == f"Update note {args.note_id}: {payload}"
                or text == f"ノート {args.note_id} を更新: {payload}"
            )
        if isinstance(args, DeleteNoteArguments):
            return (
                text.casefold() == f"Delete note {args.note_id}".casefold()
                or text == f"ノート {args.note_id} を削除"
            )
        return isinstance(args, ListNotesArguments)

    def check(self, tx: Transaction, request: ToolExecution, arguments: Arguments) -> Authority:
        try:
            job, run, expected = self.memory._target(tx, request)
            access = self.memory.open(tx, job)
            if not isinstance(arguments, ListNotesArguments):
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
            effect = ResourceId(str(uuid5(UUID(request.tool_call_id), "brainoid-tool-effect")))
            scope = expected.conversation.scope
            if isinstance(arguments, ListNotesArguments):
                self.service.scopes(access)
                execution = tx.get(
                    f"tenants/{job.tenant_id}/tool_executions/{request.tool_call_id}"
                )
                saved_result = None if execution is None else execution.get("result")
                if saved_result is not None:
                    result = ToolResult.model_validate(saved_result)
                    if result.status == "succeeded":
                        raw = result.result.get("notes")
                        if not isinstance(raw, list):
                            raise PolicyDenied()
                        for item in raw:
                            if not isinstance(item, dict):
                                raise PolicyDenied()
                            note, _ = self.service.get(
                                access,
                                TypeAdapter(ResourceId).validate_python(item.get("id")),
                                for_model=True,
                            )
                            if view(note).model_dump(mode="json") != item:
                                raise PolicyDenied()
                            self.audience(note, self.memory.consumer(job))
            elif isinstance(arguments, DeleteNoteArguments):
                saved = tx.get(operation_path(job.tenant_id, effect))
                if saved is not None:
                    self.memory.forgetting().operation(access, saved)
                    scope = Scope.model_validate(saved["scope"])
                else:
                    _, grant, _, _ = self.memory.forgetting().prepare(
                        tx, access, self.delete_body(arguments)
                    )
                    scope = grant.scope
            elif isinstance(arguments, (CreateNoteArguments, UpdateNoteArguments)):
                body = (
                    NoteCreate.model_validate(arguments.model_dump())
                    if isinstance(arguments, CreateNoteArguments)
                    else arguments.changes
                )
                identity = arguments.note_id if isinstance(arguments, UpdateNoteArguments) else None
                version = (
                    arguments.expected_version
                    if isinstance(arguments, UpdateNoteArguments)
                    else None
                )
                if tx.get(effect_path(job.tenant_id, effect)) is not None:
                    note = self.service.proof(access, effect, body, identity, version)
                    scope = Scope(visibility=note.visibility, display_policy=note.display_policy)
                elif isinstance(arguments, UpdateNoteArguments):
                    note, _ = self.service.get(
                        access, arguments.note_id, write=True, for_model=True
                    )
                    if note.version != arguments.expected_version:
                        raise ToolRejected("conflict")
                    scope = Scope(
                        visibility=note.visibility,
                        display_policy=arguments.changes.display_policy or note.display_policy,
                    )
                else:
                    scope = Scope(
                        visibility=arguments.visibility, display_policy=arguments.display_policy
                    )
                self.service.scope(tx, access, scope)
            else:
                raise PolicyDenied()
            if scope != expected.conversation.scope:
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

    async def execute_notes(
        self,
        name: str,
        invocation: Invocation[NoteArguments],
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
                    or saved.get("name") != name
                ):
                    raise PolicyDenied()
                job = self.memory.job(
                    tx, invocation.context.tenant_id, ResourceId(saved["authority"]["run_id"])
                )
                run, _, expected, _ = self.handler.state.check(tx, job)
                if expected.context != invocation.context or invocation.tool_call_id not in run.get(
                    "tool_call_ids", []
                ):
                    raise PolicyDenied()
                return self.memory.open(tx, job, control)

            args = invocation.arguments
            if isinstance(args, ListNotesArguments):
                if reconcile:
                    return HandlerResult(status="outcome_unknown")

                def read(tx: Transaction) -> tuple[list[Note], FactTransaction]:
                    access = opened(tx)
                    saved = tx.get(
                        f"tenants/{invocation.context.tenant_id}/tool_executions/{invocation.tool_call_id}"
                    )
                    assert saved is not None
                    job = self.memory.job(
                        tx, invocation.context.tenant_id, ResourceId(saved["authority"]["run_id"])
                    )
                    consumer = self.memory.consumer(job)
                    selected: list[Note] = []
                    for note in self.service.candidates(access, query=args.query, for_model=True):
                        try:
                            self.audience(note, consumer)
                        except ToolRejected:
                            continue
                        # Bound serialized results under Executor's 64KiB ceiling.
                        if (
                            len(
                                json.dumps(
                                    [view(n).model_dump(mode="json") for n in [*selected, note]],
                                    ensure_ascii=False,
                                ).encode()
                            )
                            > 48000
                        ):
                            break
                        selected.append(note)
                        if len(selected) >= args.limit:
                            break
                    return selected, access

                notes, access = self.handler.jobs.store.run(read)
                access.finish()
            elif isinstance(args, DeleteNoteArguments):
                if reconcile:

                    def lookup(tx: Transaction) -> Operation | None:
                        access = opened(tx)
                        saved = tx.get(
                            operation_path(invocation.context.tenant_id, invocation.effect_id)
                        )
                        return (
                            None
                            if saved is None
                            else self.memory.forgetting().operation(access, saved)
                        )

                    operation = self.handler.jobs.store.run(lookup)
                    return (
                        HandlerResult(status="outcome_unknown")
                        if operation is None
                        else HandlerResult(
                            status="succeeded",
                            result={"operation": operation.model_dump(mode="json")},
                        )
                    )

                def terminal(
                    tx: Transaction, operation: Operation, replacements: dict[str, Document]
                ) -> Callable[[], None]:
                    saved = tx.get(
                        f"tenants/{invocation.context.tenant_id}/tool_executions/{invocation.tool_call_id}"
                    )
                    assert saved is not None
                    run_id = ResourceId(saved["authority"]["run_id"])
                    path = run_path(invocation.context.tenant_id, run_id)
                    run = tx.get(path)
                    if run is None:
                        raise PolicyDenied()
                    proof_path = f"tenants/{invocation.context.tenant_id}/memory_forget_effects/{operation.operation_id}"
                    if tx.get(proof_path) is not None:
                        raise PolicyDenied()
                    proof = {
                        "operation_id": operation.operation_id,
                        "run_id": run_id,
                        "tool_call_id": invocation.tool_call_id,
                        "request": self.delete_body(args).model_dump(mode="json"),
                    }
                    updated = {
                        **run,
                        "purge_dependencies": capture(run, complete=True).model_dump(mode="json"),
                        "forget_operation_id": operation.operation_id,
                        "memory_records": [],
                        "tool_bindings": [],
                        "continuation": [],
                    }

                    def save() -> None:
                        tx.create(proof_path, proof)
                        tx.set(path, updated)

                    return save

                operation = self.memory.forgetting().forget(
                    opened,
                    self.delete_body(args),
                    str(invocation.effect_id),
                    effect=invocation.effect_id,
                    extra=terminal,
                )
                return HandlerResult(
                    status="succeeded", result={"operation": operation.model_dump(mode="json")}
                )
            else:
                body = (
                    NoteCreate.model_validate(args.model_dump())
                    if isinstance(args, CreateNoteArguments)
                    else args.changes
                )
                identity = args.note_id if isinstance(args, UpdateNoteArguments) else None
                version = args.expected_version if isinstance(args, UpdateNoteArguments) else None
                if reconcile:

                    def read_proof(tx: Transaction) -> tuple[Note, FactTransaction]:
                        access = opened(tx)
                        return self.service.proof(
                            access, invocation.effect_id, body, identity, version
                        ), access

                    note, access = self.handler.jobs.store.run(read_proof)
                    access.finish()
                else:

                    def retire(tx: Transaction, record: Note) -> Callable[[], None]:
                        # opened() already checked ALL current inputs in this TX.
                        # Commit the retirement with the canonical mutation proof.
                        saved = tx.get(
                            f"tenants/{invocation.context.tenant_id}/tool_executions/{invocation.tool_call_id}"
                        )
                        assert saved is not None and isinstance(args, UpdateNoteArguments)
                        path = run_path(
                            invocation.context.tenant_id, ResourceId(saved["authority"]["run_id"])
                        )
                        run = tx.get(path)
                        if run is None or run.get("note_update") is not None:
                            raise PolicyDenied()
                        updated = {
                            **run,
                            "purge_dependencies": capture(
                                run,
                                complete=True,
                                note_dependencies=(
                                    NoteDependency(note_id=record.id, version=record.version),
                                ),
                            ).model_dump(mode="json"),
                            "note_update": {
                                "tool_call_id": invocation.tool_call_id,
                                "effect_id": invocation.effect_id,
                                "target_id": record.id,
                                "expected_version": args.expected_version,
                            },
                            "memory_records": [],
                            "tool_bindings": [],
                            "continuation": [],
                            "completed_text": "",
                            "streamed_text": "",
                        }
                        return lambda: tx.set(path, updated)

                    note = self.service.mutate(
                        opened,
                        body,
                        str(invocation.effect_id),
                        identity=identity,
                        expected=version,
                        effect=invocation.effect_id,
                        extra=retire if isinstance(args, UpdateNoteArguments) else None,
                    )
                notes = [note]
            return HandlerResult(
                status="succeeded",
                result={"notes": [view(n).model_dump(mode="json") for n in notes]},
            )

        return await memory_write(
            self.handler.writer, work, stopped=lambda: self.memory.stopped(control)
        )

    @staticmethod
    def audience(note: Note, consumer: ResultConsumer) -> None:
        receipt(
            ResultUse(
                receipt="0" * 64,
                sources=(
                    ResultSource(
                        scope=Scope(visibility=note.visibility, display_policy=note.display_policy),
                        owner_user_id=note.owner_user_id,
                    ),
                ),
            ),
            consumer,
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
        try:
            job = self.memory.job(tx, consumer.context.tenant_id, consumer.run_id)
            self.handler.state.check(tx, job, check_results=False)
            if self.memory.consumer(job) != consumer:
                raise PolicyDenied()
            access = self.memory.open(tx, job)
            sources = []
            if result.status == "succeeded" and isinstance(arguments, DeleteNoteArguments):
                if result.operation_id is None:
                    raise PolicyDenied()
                operation = self.memory.forgetting().operation(
                    access, tx.get(operation_path(job.tenant_id, result.operation_id))
                )
                if result.result != {"operation": operation.model_dump(mode="json")}:
                    raise PolicyDenied()
            elif result.status == "succeeded":
                raw = result.result.get("notes")
                if not isinstance(raw, list) or len(raw) > 20 or set(result.result) != {"notes"}:
                    raise PolicyDenied()
                for item in raw:
                    if not isinstance(item, dict):
                        raise PolicyDenied()
                    note, _ = self.service.get(
                        access, TypeAdapter(ResourceId).validate_python(item["id"]), for_model=True
                    )
                    if view(note).model_dump(mode="json") != item:
                        raise PolicyDenied()
                    self.audience(note, consumer)
                    sources.append(
                        ResultSource(
                            scope=Scope(
                                visibility=note.visibility, display_policy=note.display_policy
                            ),
                            owner_user_id=note.owner_user_id,
                        )
                    )
            elif result.result:
                raise PolicyDenied()
            access.finish()
            return ResultUse(
                receipt=fingerprint(
                    [result.model_dump(mode="json"), consumer.model_dump(mode="json")]
                ),
                sources=tuple(sources)
                or (
                    ResultSource(
                        scope=consumer.conversation_scope,
                        owner_user_id=consumer.conversation_owner_user_id,
                    ),
                ),
            )
        except PolicyUnavailable:
            raise
        except (PolicyDenied, ValueError, KeyError):
            raise ToolRejected("permission_denied") from None
