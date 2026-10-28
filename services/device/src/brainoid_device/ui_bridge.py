"""Typed local operations, never a method/URL/header proxy. Public DTOs only."""

import asyncio
import hashlib
from collections.abc import AsyncIterator, Callable
from typing import Annotated, Any, Literal
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
from pydantic import Field, TypeAdapter, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route
from starlette.types import Message, Receive, Scope, Send

from brainoid_device.cloud_client import CloudClient
from brainoid_device.cloud_models import CloudError, Cursor, Pending, Record
from brainoid_device.cloud_wire import SSEDecoder, decode, encode
from brainoid_device.device_transport import backoff
from brainoid_device.local_api import Sessions, error


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


class CancelRun(Context):
    operation: Literal["cancel_run"]
    operation_id: ResourceId
    run_id: ResourceId


Operation = TypeAdapter(
    Annotated[
        ListConversations | GetConversation | CreateConversation | CreateTurn | GetRun | CancelRun,
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
    def __init__(self, client: CloudClient, sessions: Sessions) -> None:
        self.client, self.sessions = client, sessions
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

    async def events(
        self, run_id: ResourceId, session: ResourceId | None, queue: asyncio.Queue[bytes | None]
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

        async def publish(kind: str, value: dict[str, Any]) -> None:
            client.check(generation)
            client.check_output(session, output_generation)
            async with asyncio.timeout(5):
                await queue.put(b"event: " + kind.encode() + b"\ndata: " + encode(value) + b"\n\n")
            client.check(generation)

        try:
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
                            if rejected is not None:
                                if response.status_code == 401:
                                    client.stop()
                                    raise CloudError("credential_refused", 401)
                                raise CloudError("cloud_stream_refused", response.status_code)
                            rejected = token
                            rejected_version = version
                            continue
                        if response.status_code in {409, 410}:
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
                                if event["type"] != "speech.ready":
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
                    history = await self.history(run_id, session)
                    if history["run"]["status"] in {"completed", "failed", "cancelled"}:
                        cursor.terminal = True
                        cursor.seq = history["run"]["last_event_seq"]
                        client.save()
                        await publish("history", history)
                        return
                except (httpx.HTTPError, OSError, TimeoutError):
                    pass
                client.check(generation)
                await client.sleep(backoff(attempt))
                attempt = min(attempt + 1, 5)
        finally:
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
                elif request.url.path.startswith("/local/cloud/notifications/"):
                    if self.client.recovery is None:
                        raise CloudError("snapshot_unavailable", 503)
                    identity = TypeAdapter(ResourceId).validate_python(
                        request.path_params["notification_id"]
                    )
                    await self.client.owned(self.client.recovery.displayed(identity))
                    value = {"status": "displayed"}
                elif request.url.path == "/local/cloud/operations":
                    operation = Operation.validate_python(local_body(await request.body()))
                    check_publication = self.operation_publication(operation.private_session_id)
                    value = await self.client.owned(self.operation(operation))
                else:
                    run_id = TypeAdapter(ResourceId).validate_python(request.path_params["run_id"])
                    session = (
                        TypeAdapter(ResourceId).validate_python(
                            request.headers["x-private-session"]
                        )
                        if "x-private-session" in request.headers
                        else None
                    )
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
                            await self.events(run_id, session, queue)
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
            Route("/local/cloud/snapshot", dispatch, methods=["GET"]),
            Route(
                "/local/cloud/notifications/{notification_id}/displayed", dispatch, methods=["POST"]
            ),
            Route("/local/cloud/pairing", dispatch, methods=["POST", "GET"]),
            Route("/local/cloud/operations", dispatch, methods=["POST"]),
            Route("/local/cloud/runs/{run_id}/events", dispatch, methods=["GET"]),
        ]
