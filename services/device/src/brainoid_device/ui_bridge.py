"""Typed local operations, never a method/URL/header proxy. Public DTOs only."""

import asyncio
import hashlib
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any, Literal
from urllib.parse import urlencode

import httpx
from brainoid_contracts import ResourceId
from brainoid_runs.models import (
    Conversation,
    ConversationCreate,
    ConversationPage,
    MessagePage,
    Run,
    RunAccepted,
    TurnCreate,
)
from pydantic import ConfigDict, Field, TypeAdapter, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route
from starlette.types import Message, Receive, Scope, Send

from brainoid_device.cloud_client import CloudClient
from brainoid_device.cloud_models import CloudError, Cursor, Pending, Record
from brainoid_device.cloud_wire import SSEDecoder, decode, encode
from brainoid_device.device_transport import backoff
from brainoid_device.local_api import Sessions, error

if TYPE_CHECKING:
    from brainoid_device.audio_service import AudioService


class PairingStart(Record):
    device_name: Annotated[str, Field(min_length=1, max_length=200)]


class Context(Record):
    private_session_id: ResourceId | None = None


class ListConversations(Context):
    operation: Literal["list_conversations"]
    cursor: Annotated[str, Field(max_length=2048)] | None = None


class GetConversation(Context):
    operation: Literal["get_conversation", "list_messages"]
    conversation_id: ResourceId
    cursor: Annotated[str, Field(max_length=2048)] | None = None


class CreateConversation(Context):
    operation: Literal["create_conversation"]
    operation_id: ResourceId
    conversation: ConversationCreate


class CreateTurn(Context):
    operation: Literal["create_turn"]
    operation_id: ResourceId
    conversation_id: ResourceId
    turn: TurnCreate


class GetRun(Context):
    operation: Literal["get_run"]
    run_id: ResourceId


class CommandStatus(Record):
    """A public status projection; never forward unvalidated cloud response fields."""

    model_config = ConfigDict(extra="ignore", strict=True)
    id: ResourceId
    tenant_id: ResourceId
    status: Literal[
        "queued",
        "delivered",
        "accepted",
        "running",
        "succeeded",
        "failed",
        "expired",
        "cancelled",
        "outcome_unknown",
    ]


class CommandOperation(Context):
    operation: Literal["get_command", "cancel_command"]
    command_id: ResourceId
    operation_id: ResourceId | None = None


class SpeechListen(Context):
    mode: Literal["future", "waiting"]


class CancelRun(Context):
    operation: Literal["cancel_run"]
    operation_id: ResourceId
    run_id: ResourceId


Operation = TypeAdapter(
    Annotated[
        ListConversations
        | GetConversation
        | CreateConversation
        | CreateTurn
        | GetRun
        | CancelRun
        | CommandOperation,
        Field(discriminator="operation"),
    ]
)


def local_body(raw: bytes) -> dict[str, Any]:
    try:
        return decode(raw)
    except CloudError:
        raise ValueError("invalid_operation") from None


class GuardedResponse(Response):
    def __init__(
        self,
        response: Response,
        client: CloudClient,
        sessions: Sessions,
        request: Request,
        check_publication: Callable[[], None] | None = None,
    ) -> None:
        self.response, self.client, self.sessions, self.request = (
            response,
            client,
            sessions,
            request,
        )
        self.generation = client.generation
        self.output_generation = client.output_generation
        self.check_publication = check_publication

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.check_publication:
            try:
                self.check_publication()
            except CloudError as exc:
                await error(exc.code, exc.status)(scope, receive, send)
                return
        started = False

        async def guarded(message: Message) -> None:
            nonlocal started
            try:
                self.client.check(self.generation)
                self.client.check_output(generation=self.output_generation)
                if self.check_publication:
                    self.check_publication()
                if self.sessions.find(self.request) is None:
                    raise CloudError("session_required", 401)
            except CloudError as exc:
                if not started:
                    code = "snapshot_changed" if exc.code == "snapshot_changed" else "cloud_stopped"
                    await error(code, 409)(scope, receive, send)
                    started = True
                elif message["type"] == "http.response.body":
                    await send({"type": "http.response.body", "body": b"", "more_body": False})
                raise asyncio.CancelledError from None
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        await self.response(scope, receive, guarded)


class UIBridge:
    def __init__(
        self, client: CloudClient, sessions: Sessions, audio: "AudioService | None" = None
    ) -> None:
        self.client, self.sessions = client, sessions
        self.audio = audio
        self._streams: set[tuple[str, str | None]] = set()

    def operation_publication(self, session: ResourceId | None) -> Callable[[], None]:
        # The completed owned task may yield to revoke/expiry/reset callbacks
        # before its awaiting route resumes. Preserve its originating authority;
        # a later Response constructor must not adopt a new epoch for old data.
        generation = self.client.generation
        output_generation = self.client.output_generation

        def check() -> None:
            self.client.check(generation)
            self.client.check_output(session, output_generation)

        return check

    async def operation(self, operation: Any) -> dict[str, Any]:
        client = self.client
        client.check_output(operation.private_session_id)
        output_generation = client.output_generation
        await client.token()
        credentials = client.journal.credentials
        assert credentials is not None
        root = f"/v1/tenants/{credentials.tenant_id}"
        headers = (
            {"X-Private-Session": operation.private_session_id}
            if operation.private_session_id
            else {}
        )
        body: dict[str, Any] | None = None
        kind: Literal["conversation", "turn", "cancel"] = "conversation"
        expected = 200
        model: Any
        if isinstance(operation, ListConversations):
            path, model = root + "/conversations", ConversationPage
        elif isinstance(operation, GetConversation):
            path = root + f"/conversations/{operation.conversation_id}"
            model = Conversation
            if operation.operation == "list_messages":
                path, model = path + "/messages", MessagePage
        elif isinstance(operation, CreateConversation):
            path, model, expected = root + "/conversations", Conversation, 201
            body = operation.conversation.model_dump(mode="json")
        elif isinstance(operation, CommandOperation):
            path, model = root + f"/commands/{operation.command_id}", CommandStatus
            if operation.operation == "cancel_command":
                if operation.operation_id is None:
                    raise CloudError("invalid_operation", 400)
                path, body, kind = path + ":cancel", {}, "cancel"
        elif isinstance(operation, CreateTurn):
            path, model, kind, expected = (
                root + f"/conversations/{operation.conversation_id}/turns",
                RunAccepted,
                "turn",
                202,
            )
            body = operation.turn.model_dump(mode="json", exclude_none=True)
        else:
            path, model = root + f"/runs/{operation.run_id}", Run
            if isinstance(operation, CancelRun):
                path, body, kind = path + ":cancel", {}, "cancel"
        query: dict[str, str | int] = {}
        if isinstance(operation, GetConversation) and operation.operation == "list_messages":
            # One 8,000-character message (including JSON escapes), 20 source
            # refs and a cursor fit the 64 KiB wire/response envelope.
            query["limit"] = 1
        if isinstance(operation, ListConversations | GetConversation) and operation.cursor:
            query["cursor"] = operation.cursor
        if query:
            path += "?" + urlencode(query)
        if body is None:
            status, value = await client.authorized("GET", path, headers=headers)
        else:
            async with client.mutation_lock:
                body_hash = hashlib.sha256(encode(body)).hexdigest()
                pending = client.journal.pending_mutation
                if pending and pending.key == operation.operation_id:
                    if (pending.path, pending.body_hash, pending.session) != (
                        path,
                        body_hash,
                        operation.private_session_id,
                    ):
                        raise CloudError("operation_conflict", 409)
                else:
                    if pending and pending.result is None:
                        raise CloudError("reconciliation_required", 409)
                    pending = Pending(
                        kind=kind,
                        key=operation.operation_id,
                        path=path,
                        body={},
                        body_hash=body_hash,
                        session=operation.private_session_id,
                        started=client.now(),
                        uncertain=False,
                    )
                    client.journal.pending_mutation = pending
                # Even saved public receipts must pass current cloud authorization;
                # do not release a private response from disk on reconnect.
                status, value = await client._reconcile(
                    pending, authenticated=True, body_override=body
                )
                if status == expected:
                    self._validate(model, value)
                    pending.result = {}  # durable completion only; no private cache
                    pending.uncertain = False
                    client.save()
                elif status < 500 and not pending.uncertain:
                    pending.result = {}
                    client.save()
                elif pending.result is None:
                    raise CloudError("reconciliation_required", 409)
        if status != expected:
            raise CloudError(
                "cloud_operation_refused",
                status if status in {400, 403, 404, 409, 410, 429} else 502,
            )
        client.check_output(operation.private_session_id, output_generation)
        result = self._validate(model, value)
        if isinstance(operation, GetRun | CancelRun) and result["run_id"] != operation.run_id:
            raise CloudError("invalid_cloud_response", 502)
        if (
            isinstance(operation, GetConversation)
            and operation.operation == "get_conversation"
            and result["id"] != operation.conversation_id
        ):
            raise CloudError("invalid_cloud_response", 502)
        if model is RunAccepted:
            # Ignore remote navigation paths, construct only canonical local/API paths.
            result.pop("events_path", None)
            result.pop("status_path", None)
        if isinstance(operation, CommandOperation) and result["id"] != operation.command_id:
            raise CloudError("invalid_cloud_response", 502)
        client.check()
        return result

    @staticmethod
    def _validate(model: Any, value: dict[str, Any]) -> dict[str, Any]:
        try:
            result: dict[str, Any] = model.model_validate(value).model_dump(
                mode="json", exclude_none=True
            )
            return result
        except ValidationError:
            raise CloudError("invalid_cloud_response", 502) from None

    async def history(self, run_id: ResourceId, session: ResourceId | None) -> dict[str, Any]:
        run = await self.operation(
            GetRun(operation="get_run", run_id=run_id, private_session_id=session)
        )
        if self.audio and run["status"] in {"failed", "cancelled"}:
            self.audio.stop_run(run_id)
        cursor = None
        final = run.get("final_message_id")
        found = final is None
        # Keep only one page, never an unbounded conversation buffer. A caller
        # can continue a very long history using the returned typed cursor.
        try:
            async with asyncio.timeout(12):
                for _ in range(32):
                    messages = await self.operation(
                        GetConversation(
                            operation="list_messages",
                            conversation_id=run["conversation_id"],
                            private_session_id=session,
                            cursor=cursor,
                        )
                    )
                    found = found or any(item["id"] == final for item in messages["items"])
                    cursor = messages.get("next_cursor")
                    if found or cursor is None:
                        break
        except TimeoutError:
            raise CloudError("history_unavailable", 503) from None
        return {
            "run": run,
            "messages": messages,
            "final_message_pending": not found,
            "replace": True,
            "play_speech": False,
        }

    async def wait_for_speech(self, owner: str, session: ResourceId | None) -> dict[str, Any]:
        audio, recovery = self.audio, self.client.recovery
        if audio is None or recovery is None or audio.output_device is None:
            raise CloudError("audio_device_required", 409)
        audio.clear()
        intent = audio.generation
        check = self.operation_publication(session)
        value, publication = await recovery.publication()
        publication()
        if not value["ready"] or not value["snapshot"]:
            raise CloudError("snapshot_unavailable", 503)
        baseline = frozenset(item["run_id"] for item in value["snapshot"]["assigned_runs"])
        # This timestamp belongs to this complete snapshot validation response.
        # Never compare cloud creation timestamps to an unsynchronised Pi clock.
        check()
        publication()
        observed = recovery.observed_at
        if audio.generation != intent or observed is None:
            raise CloudError("audio_wait_unavailable", 409)
        audio.bind(owner, session, 60)
        audio.waiting = (baseline, observed)
        return {"waiting": True}

    async def attach_speech(
        self, run_id: ResourceId, session: ResourceId | None, owner: str, intent: int, mode: str
    ) -> None:
        audio, recovery = self.audio, self.client.recovery
        if audio is None or recovery is None or audio.output_device is None:
            raise CloudError("audio_device_required", 409)
        check = self.operation_publication(session)
        value, publication = await recovery.publication()
        publication()
        state = value.get("snapshot")
        assignment = (
            next((item for item in state["assigned_runs"] if item["run_id"] == run_id), None)
            if state
            else None
        )
        if (
            not value["ready"]
            or assignment is None
            or assignment["output_mode"] != "speech"
            or assignment.get("private_session_id") != session
        ):
            raise CloudError("audio_assignment_required", 403)
        run = await self.operation(
            GetRun(operation="get_run", run_id=run_id, private_session_id=session)
        )
        conversation = await self.operation(
            GetConversation(
                operation="get_conversation",
                conversation_id=ResourceId(assignment["conversation_id"]),
                private_session_id=session,
            )
        )
        check()
        publication()
        if audio.generation != intent or not self.sessions.valid(owner):
            raise CloudError("audio_context_changed", 409)
        assert state is not None
        private = state.get("active_private_session")
        if (
            run["conversation_id"] != assignment["conversation_id"]
            or conversation["tenant_id"] != state["tenant_id"]
            or (
                conversation["visibility"] == "private"
                and (
                    not private
                    or private["id"] != session
                    or conversation.get("owner_user_id") != private["user_id"]
                    or conversation["display_policy"] != "authenticated_session"
                )
            )
            or (
                conversation["visibility"] == "shared"
                and conversation["display_policy"] != "shared_display"
            )
        ):
            raise CloudError("audio_assignment_required", 403)
        if mode == "waiting":
            binding, waiting = audio.binding, audio.waiting
            if (
                binding is None
                or binding.owner != owner
                or binding.session != session
                or waiting is None
            ):
                raise CloudError("audio_context_changed", 409)
            audio.check(binding)
            if (
                run_id in waiting[0]
                or datetime.fromisoformat(run["created_at"]).timestamp() <= waiting[1]
            ):
                raise CloudError("audio_old_assignment", 409)
            if run["status"] in {"failed", "cancelled"}:
                raise CloudError("audio_run_ended", 409)
            audio.begin_run(run_id, session, owner)
        else:
            if run["status"] not in {"queued", "running", "waiting_confirmation"}:
                raise CloudError("audio_run_ended", 409)
            audio.begin_run(
                run_id, session, owner, after_seq=run["last_event_seq"], first_sentence=True
            )

    async def events(
        self,
        run_id: ResourceId,
        session: ResourceId | None,
        queue: asyncio.Queue[bytes | None],
        speech: tuple[str, int, str] | None = None,
    ) -> None:
        client = self.client
        generation = client.generation
        client.check_output(session)
        output_generation = client.output_generation
        key = (str(run_id), str(session) if session else None)
        if key in self._streams:
            raise CloudError("stream_already_open", 409)
        cursor = next(
            (
                item
                for item in client.journal.cursors
                if (item.run_id, item.session) == (run_id, session)
            ),
            None,
        )
        fresh_stream = cursor is None
        if speech is not None and speech[2] == "waiting" and not fresh_stream:
            raise CloudError("audio_old_stream", 409)
        if cursor is None:
            if len(client.journal.cursors) >= 32:
                retired = next(
                    (
                        value
                        for value in client.journal.cursors
                        if value.terminal and (value.run_id, value.session) not in self._streams
                    ),
                    None,
                )
                if retired is None:
                    raise CloudError("cursor_limit", 409)
                client.journal.cursors.remove(retired)
            cursor = Cursor(run_id=run_id, session=session)
            client.journal.cursors.append(cursor)
            client.save()
        self._streams.add(key)
        audio_generation = self.audio.generation if self.audio else None

        def stop_audio() -> None:
            nonlocal audio_generation
            # This stream may never terminate or rearm a newer audio intention.
            if self.audio and self.audio.generation == audio_generation:
                self.audio.stop_run(run_id)
            audio_generation = None

        async def publish(kind: str, value: dict[str, Any]) -> None:
            client.check(generation)
            client.check_output(session, output_generation)
            async with asyncio.timeout(5):
                await queue.put(b"event: " + kind.encode() + b"\ndata: " + encode(value) + b"\n\n")
            client.check(generation)

        try:
            if speech is not None:
                await self.attach_speech(run_id, session, *speech)
                audio_generation = self.audio.generation if self.audio else None
            if not fresh_stream and speech is None:
                stop_audio()  # Persisted cursor/reload is text recovery, never live audio.
            if cursor.seq:
                history = await self.history(run_id, session)
                await publish("history", history)
                if (
                    history["run"]["status"] in {"completed", "failed", "cancelled"}
                    and cursor.seq >= history["run"]["last_event_seq"]
                ):
                    cursor.terminal = True
                    client.save()
                    return
            attempt = 0
            rejected: str | None = None
            rejected_version: int | None = None
            while client.active:
                token = await client.token(rejected=rejected, rejected_version=rejected_version)
                version = client.journal.credential_version
                credentials = client.journal.credentials
                assert credentials is not None
                headers = {
                    "Accept": "text/event-stream",
                    "Accept-Encoding": "identity",
                    "Authorization": "Bearer " + token,
                    "Last-Event-ID": str(cursor.seq),
                }
                if session:
                    headers["X-Private-Session"] = session
                request = httpx.Request(
                    "GET",
                    client.settings.api_url
                    + f"/v1/tenants/{credentials.tenant_id}/runs/{run_id}/events",
                    headers=headers,
                )
                try:
                    response = await client.http.send(request, stream=True)
                    try:
                        client.check(generation)
                        if response.headers.get("content-encoding", "identity") != "identity":
                            raise CloudError("invalid_cloud_response", 502)
                        if response.status_code in {401, 403, 404}:
                            stop_audio()
                            if rejected is not None:
                                if response.status_code == 401:
                                    client.stop()
                                    raise CloudError("credential_refused", 401)
                                raise CloudError("cloud_stream_refused", response.status_code)
                            rejected = token
                            rejected_version = version
                            continue
                        if response.status_code in {409, 410}:
                            stop_audio()
                            history = await self.history(run_id, session)
                            client.check(generation)
                            cursor.seq = history["run"]["last_event_seq"]
                            cursor.terminal = history["run"]["status"] in {
                                "completed",
                                "failed",
                                "cancelled",
                            }
                            client.save()
                            await publish("history", history)
                            return
                        if (
                            response.status_code != 200
                            or response.headers.get("content-type", "").split(";")[0]
                            != "text/event-stream"
                        ):
                            raise CloudError("cloud_stream_refused", 502)
                        rejected, rejected_version = None, None
                        decoder = SSEDecoder()
                        async for chunk in response.aiter_bytes(chunk_size=None):
                            client.check(generation)
                            client.check_output(session, output_generation)
                            for event in decoder.feed(chunk):
                                if event["run_id"] != run_id:
                                    raise CloudError("invalid_cloud_response", 502)
                                if event["seq"] <= cursor.seq:
                                    continue
                                if event["seq"] != cursor.seq + 1:
                                    raise CloudError("invalid_cloud_response", 502)
                                cursor.seq = event["seq"]
                                cursor.terminal = event["type"] in {
                                    "run.completed",
                                    "run.failed",
                                    "run.cancelled",
                                }
                                client.save()  # never replay a committed speech/event after crash
                                if self.audio and self.audio.generation == audio_generation:
                                    self.audio.event(event, session)
                                # Speech metadata preserves the contiguous UI cursor.
                                # Only the daemon consumes it for playback; no PCM is exposed.
                                await publish("run", event)
                                attempt, rejected = 0, None
                                if event["type"] in {
                                    "run.completed",
                                    "run.failed",
                                    "run.cancelled",
                                }:
                                    return
                    finally:
                        await response.aclose()
                    stop_audio()  # EOF before a terminal event also ends the live connection.
                    history = await self.history(run_id, session)
                    if history["run"]["status"] in {"completed", "failed", "cancelled"}:
                        cursor.terminal = True
                        cursor.seq = history["run"]["last_event_seq"]
                        client.save()
                        await publish("history", history)
                        return
                except (httpx.HTTPError, OSError, TimeoutError):
                    stop_audio()
                client.check(generation)
                await client.sleep(backoff(attempt))
                attempt = min(attempt + 1, 5)
        except CloudError:
            stop_audio()
            raise
        finally:
            if not cursor.terminal:
                stop_audio()  # UI disconnect/cancellation cannot leave live playback behind.
            self._streams.discard(key)

    def routes(self) -> list[Route]:
        async def dispatch(request: Request) -> Response:
            current = (
                self.sessions.authorize_mutation(request)
                if request.method == "POST"
                else self.sessions.find(request)
            )
            if isinstance(current, Response):
                return current
            if current is None:
                return error("session_required", 401)
            check_publication: Callable[[], None] | None = None
            try:
                if request.url.path == "/local/cloud/pairing":
                    if request.method == "GET":
                        value: dict[str, Any] = self.client.public_pairing()
                    else:
                        body = PairingStart.model_validate(local_body(await request.body()))
                        value = await self.client.owned(self.client.begin_pairing(body.device_name))
                elif request.url.path == "/local/cloud/status":
                    return JSONResponse({"status": self.client.status})
                elif request.url.path == "/local/cloud/snapshot":
                    if self.client.recovery is None:
                        raise CloudError("snapshot_unavailable", 503)
                    recovery = self.client.recovery
                    value, check_publication = await self.client.owned(recovery.publication())
                elif request.url.path.startswith("/local/cloud/private-sessions/"):
                    if self.client.recovery is None:
                        raise CloudError("snapshot_unavailable", 503)
                    identity = TypeAdapter(ResourceId).validate_python(
                        request.path_params["private_session_id"]
                    )
                    self.client.recovery.stop_output(identity)
                    value = {"status": "stopped", "cloud_session_ended": False}
                elif request.url.path.startswith("/local/cloud/notifications/"):
                    if self.client.recovery is None:
                        raise CloudError("snapshot_unavailable", 503)
                    identity = TypeAdapter(ResourceId).validate_python(
                        request.path_params["notification_id"]
                    )
                    await self.client.owned(self.client.recovery.displayed(identity))
                    value = {"status": "displayed"}
                elif request.url.path == "/local/cloud/audio-wait":
                    context = Context.model_validate(local_body(await request.body()))
                    check_publication = self.operation_publication(context.private_session_id)
                    value = await self.client.owned(
                        self.wait_for_speech(current[0], context.private_session_id)
                    )
                elif request.url.path == "/local/cloud/operations":
                    operation = Operation.validate_python(local_body(await request.body()))
                    check_publication = self.operation_publication(operation.private_session_id)
                    audio_generation = None
                    if self.audio and isinstance(operation, CreateTurn):
                        # Establish intent at submission, including two concurrent turns.
                        # A later stop, selection, capture or turn invalidates this receipt.
                        check_publication()
                        self.audio.clear()
                        audio_generation = self.audio.generation
                    if self.audio and isinstance(operation, CancelRun):
                        self.audio.stop_run(operation.run_id)
                    value = await self.client.owned(self.operation(operation))
                    check_publication()
                    if (
                        self.audio
                        and self.audio.generation == audio_generation
                        and isinstance(operation, CreateTurn)
                        and operation.turn.output_mode == "speech"
                    ):
                        self.audio.begin_run(
                            ResourceId(value["run_id"]), operation.private_session_id, current[0]
                        )
                else:
                    run_id = TypeAdapter(ResourceId).validate_python(request.path_params["run_id"])
                    session = (
                        TypeAdapter(ResourceId).validate_python(
                            request.headers["x-private-session"]
                        )
                        if "x-private-session" in request.headers
                        else None
                    )
                    speech = None
                    if request.method == "POST":
                        listen = SpeechListen.model_validate(local_body(await request.body()))
                        if listen.private_session_id != session or self.audio is None:
                            raise CloudError("audio_context_changed", 409)
                        if listen.mode == "future":
                            self.audio.clear()
                        speech = (current[0], self.audio.generation, listen.mode)
                    queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=8)

                    async def pump() -> None:
                        owner = asyncio.current_task()
                        assert owner is not None

                        output_generation = self.client.output_generation

                        async def watch_session() -> None:
                            while (
                                self.sessions.find(request) == current
                                and self.client.output_generation == output_generation
                            ):
                                await asyncio.sleep(0.1)
                            owner.cancel()

                        watcher = asyncio.create_task(watch_session())
                        try:
                            await self.events(run_id, session, queue, speech)
                        except CloudError:
                            while not queue.empty():
                                queue.get_nowait()
                            queue.put_nowait(b'event: clear\ndata: {"reason":"stream_stopped"}\n\n')
                        finally:
                            watcher.cancel()
                            await asyncio.gather(watcher, return_exceptions=True)
                            if (
                                self.sessions.find(request) != current
                                or self.client.output_generation != output_generation
                            ):
                                while not queue.empty():
                                    queue.get_nowait()
                            # Do not block shutdown behind an abandoned consumer.
                            try:
                                queue.put_nowait(None)
                            except asyncio.QueueFull:
                                while not queue.empty():
                                    queue.get_nowait()
                                queue.put_nowait(None)

                    async def stream() -> AsyncIterator[bytes]:
                        task = self.client.start(pump())
                        try:
                            while (item := await queue.get()) is not None:
                                self.client.check()
                                yield item
                        finally:
                            task.cancel()
                            await asyncio.gather(task, return_exceptions=True)
                            while not queue.empty():
                                queue.get_nowait()

                    response = StreamingResponse(stream(), media_type="text/event-stream")
                    return GuardedResponse(response, self.client, self.sessions, request)
                return GuardedResponse(
                    JSONResponse(value), self.client, self.sessions, request, check_publication
                )
            except (ValidationError, ValueError):
                return error("invalid_operation", 400)
            except CloudError as exc:
                return error(exc.code, exc.status)
            except asyncio.CancelledError:
                return error("cloud_stopped", 409)

        return [
            Route("/local/cloud/status", dispatch, methods=["GET"]),
            Route("/local/cloud/audio-wait", dispatch, methods=["POST"]),
            Route("/local/cloud/snapshot", dispatch, methods=["GET"]),
            Route(
                "/local/cloud/private-sessions/{private_session_id}/stop",
                dispatch,
                methods=["POST"],
            ),
            Route(
                "/local/cloud/notifications/{notification_id}/displayed", dispatch, methods=["POST"]
            ),
            Route("/local/cloud/pairing", dispatch, methods=["POST", "GET"]),
            Route("/local/cloud/operations", dispatch, methods=["POST"]),
            Route("/local/cloud/runs/{run_id}/events", dispatch, methods=["GET", "POST"]),
        ]
