"""Production-path regressions for DEV10 review: recovery, ownership and bounds."""

import asyncio
import json
import socket
import unittest
from dataclasses import replace
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import httpx
import uvicorn
from brainoid_contracts import ResourceId
from brainoid_device.app import DeviceApplication
from brainoid_device.cloud_client import JOURNAL_NAME, CloudClient
from brainoid_device.cloud_models import CloudError, Cursor
from brainoid_device.cloud_wire import MAX_BYTES, SSEDecoder, encode
from brainoid_device.config import CloudSettings, Settings
from brainoid_device.ledger import DeviceStorage
from brainoid_device.local_api import Sessions
from brainoid_device.runtime import Runtime
from brainoid_device.storage import CredentialStore
from brainoid_device.ui_bridge import CreateTurn, GetConversation, GetRun, UIBridge
from brainoid_runs.models import TurnCreate
from test_device_cloud import CloudFixture


class RecoveryTests(CloudFixture):
    async def test_committed_loss_then_denial_stays_unresolved_across_restart(self) -> None:
        await self.paired()
        for denial in (403, 404, 409):
            with self.subTest(denial=denial):
                state = {"count": 0, "deny": True}
                committed: dict[str, Any] = {}

                async def handler(
                    request: httpx.Request,
                    state: dict[str, Any] = state,
                    committed: dict[str, Any] = committed,
                    denial: int = denial,
                ) -> httpx.Response:
                    if request.url.path.endswith("/turns"):
                        state["count"] += 1
                        if state["count"] == 1:
                            response = await self.proxy.inner.handle_async_request(request)
                            self.assertEqual(response.status_code, 202)
                            committed.update(json.loads(await response.aread()))
                            await response.aclose()
                            raise httpx.ReadError("synthetic response loss after commit")
                        if state["deny"]:
                            return httpx.Response(denial)
                    return await self.proxy.inner.handle_async_request(request)

                transport = httpx.MockTransport(handler)
                await self.cloud.http.aclose()
                self.cloud.http = httpx.AsyncClient(transport=transport)
                operation = CreateTurn(
                    operation="create_turn",
                    operation_id=ResourceId(str(uuid4())),
                    conversation_id=self.conversation_id,
                    turn=TurnCreate(text="Private transient request", output_mode="text"),
                )
                with self.assertRaisesRegex(CloudError, "reconciliation_required"):
                    await self.bridge.operation(operation)
                self.assertIsNone(self.cloud.journal.pending_mutation.result)  # type: ignore[union-attr]
                self.assertNotIn(
                    b"Private transient request", self.private_store.read(JOURNAL_NAME)
                )
                await self.cloud.close()
                self.cloud = self.new_cloud(transport=transport)
                self.bridge = UIBridge(self.cloud, Sessions())
                before = state["count"]
                with self.assertRaisesRegex(CloudError, "reconciliation_required"):
                    await self.bridge.operation(
                        operation.model_copy(update={"operation_id": ResourceId(str(uuid4()))})
                    )
                self.assertEqual(state["count"], before)
                state["deny"] = False
                result = await self.bridge.operation(operation)
                self.assertEqual(result["run_id"], committed["run_id"])
                self.assertIsNotNone(self.cloud.journal.pending_mutation.result)  # type: ignore[union-attr]
                await asyncio.to_thread(self.execute, ResourceId(result["run_id"]))
                # Only the authoritative same-key receipt opens admission again.
                accepted = await self.bridge.operation(
                    operation.model_copy(update={"operation_id": ResourceId(str(uuid4()))})
                )
                self.assertNotEqual(accepted["run_id"], committed["run_id"])
                await asyncio.to_thread(self.execute, ResourceId(accepted["run_id"]))

    async def test_first_attempt_definite_denial_allows_a_new_operation(self) -> None:
        await self.paired()
        operation = CreateTurn(
            operation="create_turn",
            operation_id=ResourceId(str(uuid4())),
            conversation_id=ResourceId(str(uuid4())),
            turn=TurnCreate(text="Missing conversation", output_mode="text"),
        )
        with self.assertRaisesRegex(CloudError, "cloud_operation_refused"):
            await self.bridge.operation(operation)
        self.assertIsNotNone(self.cloud.journal.pending_mutation.result)  # type: ignore[union-attr]
        result = await self.bridge.operation(
            operation.model_copy(
                update={
                    "operation_id": ResourceId(str(uuid4())),
                    "conversation_id": self.conversation_id,
                }
            )
        )
        self.assertEqual(result["status"], "queued")

    async def test_maximum_unicode_pages_and_410_history_reach_final_message(self) -> None:
        await self.paired()
        self.model.text = "🙂" * 8000
        for _ in range(3):
            accepted = await self.bridge.operation(
                CreateTurn(
                    operation="create_turn",
                    operation_id=ResourceId(str(uuid4())),
                    conversation_id=self.conversation_id,
                    turn=TurnCreate(text="🙂" * 8000, output_mode="text"),
                )
            )
            await asyncio.to_thread(self.execute, ResourceId(accepted["run_id"]))
        run_id = ResourceId(accepted["run_id"])
        cursor = None
        ids = []
        while True:
            page = await self.bridge.operation(
                GetConversation(
                    operation="list_messages", conversation_id=self.conversation_id, cursor=cursor
                )
            )
            self.assertLess(len(encode(page)), MAX_BYTES)
            self.assertEqual(len(page["items"]), 1)
            self.assertEqual(page["items"][0]["text"], "🙂" * 8000)
            ids.append(page["items"][0]["id"])
            cursor = page.get("next_cursor")
            if cursor is None:
                break
        self.assertEqual(len(set(ids)), 6)
        history = await self.bridge.history(run_id, None)
        self.assertEqual(history["messages"]["items"][0]["id"], history["run"]["final_message_id"])
        self.assertFalse(history["final_message_pending"])
        self.assertLess(len(encode(history)), MAX_BYTES)
        self.cloud.journal.cursors.append(Cursor(run_id=run_id, seq=1))
        self.cloud.save()
        self.clock.advance(86400)
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=8)
        await self.bridge.events(run_id, None, queue)
        output = b"".join(queue.get_nowait() or b"" for _ in range(queue.qsize()))
        self.assertIn(b'"final_message_pending":false', output)
        self.assertIn(ids[-1].encode(), output)
        self.assertNotIn(b"text.delta", output)

    async def test_history_scan_is_bounded_and_checks_authority_each_page(self) -> None:
        run_id = ResourceId(str(uuid4()))
        calls = []

        async def operation(value: Any) -> dict[str, Any]:
            calls.append(value)
            if isinstance(value, GetRun):
                return {"conversation_id": self.conversation_id, "final_message_id": str(uuid4())}
            return {"items": [], "next_cursor": "continuation"}

        with patch.object(self.bridge, "operation", side_effect=operation):
            history = await self.bridge.history(run_id, None)
        self.assertEqual(len(calls), 33)
        self.assertTrue(history["final_message_pending"])
        self.assertEqual(history["messages"]["next_cursor"], "continuation")

        async def denied(value: Any) -> dict[str, Any]:
            if isinstance(value, GetConversation) and value.cursor:
                raise CloudError("cloud_operation_refused", 403)
            return await operation(value)

        with (
            patch.object(self.bridge, "operation", side_effect=denied),
            self.assertRaises(CloudError),
        ):
            await self.bridge.history(run_id, None)


class LocalOwnershipTests(CloudFixture):
    async def application(self, port: int) -> DeviceApplication:
        root = self.root / "application"
        root.mkdir(mode=0o700)
        for name in ("credentials", "state", "assets"):
            (root / name).mkdir(mode=0o700)
        (root / "assets/index.html").write_text("<!doctype html><title>Fixture</title>")
        settings = Settings(
            "127.0.0.1",
            port,
            root / "assets",
            root / "credentials",
            root / "state",
            self.cloud_settings,
        )

        def factory(
            config: CloudSettings,
            runtime: Runtime,
            storage: DeviceStorage,
            credentials: CredentialStore,
        ) -> CloudClient:
            return CloudClient(
                config,
                runtime,
                storage,
                credentials,
                now=lambda: self.clock.now().timestamp(),
                transport=self.proxy,
            )

        with patch("brainoid_device.app.CloudClient", side_effect=factory):
            app = DeviceApplication(settings)
        app.runtime.running = True
        assert app.cloud is not None

        async def close() -> None:
            await app.runtime.close()
            await app.cloud.close()  # type: ignore[union-attr]
            app.storage.close()
            app.credentials.close()

        self.addAsyncCleanup(close)
        await app.cloud.begin_pairing("Fixture")
        self.confirm(app.cloud.journal.pairing)  # type: ignore[arg-type]
        await app.cloud.exchange()
        assert app.cloud.journal.credentials is not None
        self.store.data[f"tenants/{self.tenant}/devices/{app.cloud.journal.credentials.device_id}"][
            "capabilities"
        ] = ["display"]
        return app

    async def test_local_expected_errors_keep_health_and_unexpected_fault_fails_it(self) -> None:
        app = await self.application(18765)
        assert app.cloud is not None
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app.app, client=("127.0.0.1", 5500)),
            base_url="http://127.0.0.1:18765",
        ) as browser:
            boot = await browser.post(
                "/local/session",
                headers={"Origin": "http://127.0.0.1:18765", "X-Brainoid-Local": "1"},
            )
            headers = {
                "Origin": "http://127.0.0.1:18765",
                "X-CSRF-Token": boot.json()["csrf_token"],
            }
            self.assertEqual((await browser.get("/healthz")).status_code, 200)
            for code in (403, 404, 409):

                async def refused(value: Any, code: int = code) -> dict[str, Any]:
                    raise CloudError(
                        "reconciliation_required" if code == 409 else "cloud_operation_refused",
                        code,
                    )

                # Route/Runtime boundary exercised with each ordinary application refusal.
                assert app.bridge is not None
                with patch.object(app.bridge, "operation", side_effect=refused):
                    response = await browser.post(
                        "/local/cloud/operations",
                        headers=headers,
                        json={"operation": "get_conversation", "conversation_id": str(uuid4())},
                    )
                self.assertEqual(response.status_code, code)
                self.assertEqual((await browser.get("/healthz")).status_code, 200)
            response = await browser.post(
                "/local/cloud/operations",
                headers=headers,
                json={"operation": "get_conversation", "conversation_id": str(uuid4())},
            )
            self.assertEqual(response.status_code, 404)
            response = await browser.post(
                "/local/cloud/operations",
                headers=headers,
                json={"operation": "get_conversation", "conversation_id": self.conversation_id},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual((await browser.get("/healthz")).status_code, 200)

            async def crashed() -> None:
                raise RuntimeError("synthetic unexpected failure")

            with self.assertRaises(RuntimeError):
                await app.cloud.owned(crashed())
            self.assertEqual((await browser.get("/healthz")).status_code, 503)

    async def test_real_local_idle_sse_closes_on_logout_expiry_and_invalidation(self) -> None:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        app = await self.application(port)
        assert app.cloud is not None and app.bridge is not None
        server = uvicorn.Server(
            uvicorn.Config(app.app, lifespan="off", access_log=False, log_level="critical")
        )
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    await asyncio.sleep(0.01)
            await app.cloud.http.aclose()
            origin = f"http://127.0.0.1:{port}"
            for mode in ("logout", "expiry", "clear"):
                with self.subTest(mode=mode):
                    opened, closed = asyncio.Event(), asyncio.Event()

                    class Idle(httpx.AsyncByteStream):
                        def __init__(self, opened: asyncio.Event, closed: asyncio.Event) -> None:
                            self.opened, self.closed = opened, closed

                        async def __aiter__(self) -> Any:
                            self.opened.set()
                            yield b": heartbeat\n\n"
                            await asyncio.Event().wait()

                        async def aclose(self) -> None:
                            self.closed.set()

                    upstream = Idle(opened, closed)

                    async def idle(
                        request: httpx.Request, stream: Idle = upstream
                    ) -> httpx.Response:
                        return httpx.Response(
                            200, headers={"Content-Type": "text/event-stream"}, stream=stream
                        )

                    app.cloud.http = httpx.AsyncClient(transport=httpx.MockTransport(idle))
                    async with httpx.AsyncClient(base_url=origin, trust_env=False) as browser:
                        boot = await browser.post(
                            "/local/session", headers={"Origin": origin, "X-Brainoid-Local": "1"}
                        )
                        headers = {"Origin": origin, "X-CSRF-Token": boot.json()["csrf_token"]}
                        async with browser.stream(
                            "GET", f"/local/cloud/runs/{uuid4()}/events"
                        ) as response:
                            self.assertEqual(response.status_code, 200)
                            await asyncio.wait_for(opened.wait(), 1)
                            if mode == "logout":
                                self.assertEqual(
                                    (
                                        await browser.delete("/local/session", headers=headers)
                                    ).status_code,
                                    204,
                                )
                            elif mode == "expiry":
                                sessions = app.bridge.sessions._sessions
                                for key, value in list(sessions.items()):
                                    sessions[key] = replace(value, expires=0)
                            else:
                                app.bridge.sessions.clear()
                            await asyncio.wait_for(closed.wait(), 1)
                            self.assertEqual(await asyncio.wait_for(response.aread(), 1), b"")
                        await asyncio.sleep(0)
                        self.assertFalse(app.bridge._streams)
                        self.assertFalse(app.cloud._tasks)
                        self.assertFalse(app.runtime._tasks)
                        self.assertTrue(app.cloud.active)
                    await app.cloud.http.aclose()
        finally:
            server.should_exit = True
            async with asyncio.timeout(5):
                await serving
            listener.close()


class SSEFramingTests(unittest.TestCase):
    def test_chunk_invariant_multiline_and_strict_per_frame_limits(self) -> None:
        event = {
            "run_id": str(uuid4()),
            "seq": 1,
            "type": "text.delta",
            "occurred_at": "2026-10-28T00:00:00Z",
            "payload": {"text": "日🙂"},
        }
        compact = b"id: 1\nevent: text.delta\ndata: " + encode(event) + b"\n\n"
        multiline = (
            b"id: 1\r\nevent: text.delta\r\n"
            + b"".join(
                b"data: " + line + b"\r\n"
                for line in json.dumps(event, ensure_ascii=False, indent=2).encode().split(b"\n")
            )
            + b"\r\n"
        )
        raw = (compact + multiline + b": heartbeat\n\n") * 400
        for size in (1, 1024, len(raw)):
            decoder, result = SSEDecoder(), []
            for offset in range(0, len(raw), size):
                result.extend(decoder.feed(raw[offset : offset + size]))
            self.assertEqual(result, [event] * 800)
        for raw in (
            b":" + b"x" * MAX_BYTES,
            b": x\n" * (MAX_BYTES // 4 + 1) + b"\n",
            compact.replace(b"id: 1", b"id: 1\nid: 1"),
            compact.replace(b"id: 1", b"id: 2"),
            compact.replace(b"event: text.delta", b"event: run.completed"),
            multiline.replace(b'data:   "seq": 1,', b'data:   "seq": 1,\ndata:   "seq": 2,'),
            b"data: \xff\n\n",
        ):
            with self.subTest(raw=raw[:40]), self.assertRaises(CloudError):
                SSEDecoder().feed(raw)
