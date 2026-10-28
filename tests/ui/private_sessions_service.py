"""Explicit browser integration: actual loopback DeviceApplication, synthetic upstream API."""

import asyncio
import json
import os
import socket
import subprocess
import unittest
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import httpx
import uvicorn
from brainoid_agent_core.ports import CallControl
from brainoid_agent_core.types import (
    Completed,
    ModelEvent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextDelta,
    TextPart,
)
from brainoid_api.services.private_sessions import PrivateSessionCreate
from brainoid_contracts import DisplayPolicy, ResourceId, Visibility
from brainoid_device.app import DeviceApplication
from brainoid_device.cloud_client import JOURNAL_NAME, CloudClient
from brainoid_device.cloud_models import Cursor
from brainoid_device.config import CloudSettings, Settings
from brainoid_device.ledger import DeviceStorage
from brainoid_device.runtime import Runtime
from brainoid_device.storage import CredentialStore
from brainoid_runs.models import ConversationCreate, TurnCreate
from test_device_snapshot import SnapshotFixture


@asynccontextmanager
async def streaming_transport(
    app: Any, proxy: httpx.AsyncBaseTransport
) -> AsyncIterator[httpx.AsyncBaseTransport]:
    """ASGITransport buffers SSE; use real loopback API HTTP for live events."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server = uvicorn.Server(
            uvicorn.Config(app, lifespan="off", access_log=False, log_level="error")
        )
        task = asyncio.create_task(server.serve(sockets=[listener]))
        observations: list[dict[str, Any]] = []
        async with httpx.AsyncClient(base_url=origin, trust_env=False, timeout=30) as client:

            class Trace(httpx.AsyncByteStream):
                def __init__(self, response: httpx.Response, record: dict[str, Any]) -> None:
                    self.response, self.record = response, record

                async def __aiter__(self) -> AsyncIterator[bytes]:
                    async for chunk in self.response.aiter_bytes():
                        self.record["wire"].append(chunk.decode())
                        yield chunk

                async def aclose(self) -> None:
                    await self.response.aclose()

            class Transport(httpx.AsyncBaseTransport):
                async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                    if request.url.path.endswith("/events"):
                        response = await client.send(
                            client.build_request(
                                request.method, request.url.path, headers=request.headers
                            ),
                            stream=True,
                        )
                        record = {
                            "run": request.url.path,
                            "cursor": request.headers.get("last-event-id"),
                            "status": response.status_code,
                            "wire": [],
                        }
                        observations.append(record)
                        return httpx.Response(
                            response.status_code,
                            headers=response.headers,
                            stream=Trace(response, record),
                        )
                    return await proxy.handle_async_request(request)

            try:
                for _ in range(200):
                    if server.started:
                        break
                    await asyncio.sleep(0.01)
                assert server.started
                yield Transport()
            finally:
                Path(os.environ["UI_ARTIFACTS"]).mkdir(parents=True, exist_ok=True)
                Path(os.environ["UI_ARTIFACTS"], "synthetic-upstream-sse.json").write_text(
                    json.dumps(observations, indent=2)
                )
                server.should_exit = True
                await asyncio.wait_for(task, 15)


class HeldModel:
    def __init__(self) -> None:
        self.entered, self.live, self.finish = Event(), Event(), Event()

    async def stream(
        self, request: ModelRequest, *, control: CallControl
    ) -> AsyncGenerator[ModelEvent]:
        yield TextDelta(text="UI10 SYNTHETIC ")
        self.entered.set()
        while not self.live.is_set():
            await asyncio.sleep(0.01)
        yield TextDelta(text="PRIVATE FINAL")
        while not self.finish.is_set():
            await asyncio.sleep(0.01)
        yield ModelResponse(
            message=ModelMessage(
                role="assistant", parts=(TextPart(text="UI10 SYNTHETIC PRIVATE FINAL"),)
            )
        )
        yield Completed(finish_reason="stop")


@unittest.skipUnless(
    os.environ.get("UI_CHROMIUM") and os.environ.get("UI_ARTIFACTS"),
    "Explicit Chromium/evidence required",
)
class EquivalentDisplayBrowser(SnapshotFixture):
    async def test_mobile_consent_and_production_display_over_actual_local_http(self) -> None:
        # Keep the real browser clock untouched so its navigation/bfcache timing
        # remains observable. Virtual deadline boundaries live in the unit suite.
        self.clock.advance((datetime.now(UTC) - self.clock.now()).total_seconds())
        root = self.root / "ui10"
        root.mkdir(mode=0o700)
        for name in ("credentials", "state"):
            (root / name).mkdir(mode=0o700)
        built = await asyncio.to_thread(
            subprocess.run,
            [
                "node",
                "node_modules/vite/bin/vite.js",
                "build",
                "apps/display",
                "--outDir",
                str(root / "assets"),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(built.returncode, 0, built.stderr)
        async with streaming_transport(self.app, self.proxy) as transport:
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                listener.listen()
                settings = Settings(
                    "127.0.0.1",
                    listener.getsockname()[1],
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
                        transport=transport,
                    )

                with patch("brainoid_device.app.CloudClient", side_effect=factory):
                    application = DeviceApplication(settings)
                server = uvicorn.Server(
                    uvicorn.Config(application.app, access_log=False, log_level="error")
                )
                task = asyncio.create_task(server.serve(sockets=[listener]))
                try:
                    for _ in range(200):
                        if server.started:
                            break
                        self.assertFalse(task.done())
                        await asyncio.sleep(0.01)
                    self.assertTrue(server.started)
                    async with httpx.AsyncClient(
                        base_url=settings.origin, trust_env=False
                    ) as local:
                        bootstrap = await local.post(
                            "/local/session",
                            headers={"Origin": settings.origin, "X-Brainoid-Local": "1"},
                        )
                        headers = {
                            "Origin": settings.origin,
                            "X-CSRF-Token": bootstrap.json()["csrf_token"],
                        }
                        paired = await local.post(
                            "/local/cloud/pairing",
                            headers=headers,
                            json={"device_name": "UI10 equivalent Pi"},
                        )
                        self.assertEqual(paired.status_code, 200)
                        cloud = application.cloud
                        assert cloud is not None
                        self.confirm(cloud.journal.pairing)  # type: ignore[arg-type]
                        await cloud.exchange()
                        assert cloud.journal.credentials is not None
                        device = cloud.journal.credentials.device_id
                        self.store.data[f"tenants/{self.tenant}/devices/{device}"][
                            "capabilities"
                        ] = ["display"]
                        self.uid = "demo-a-member"
                        session = self.sessions.start(
                            self.identity(),
                            self.tenant,
                            device,
                            PrivateSessionCreate(
                                allowed_scopes=["conversation"], acknowledge_shared_space=True
                            ),
                            str(uuid4()),
                            "ui10-browser",
                        )
                        conversation = self.conversations.create(
                            self.identity(),
                            self.tenant,
                            ConversationCreate(
                                agent_id=self.agent_id,
                                visibility=Visibility.PRIVATE,
                                display_policy=DisplayPolicy.AUTHENTICATED_SESSION,
                            ),
                            str(uuid4()),
                        )
                        run = self.conversations.turn(
                            self.identity(),
                            self.tenant,
                            conversation.id,
                            TurnCreate(
                                text="UI10 SYNTHETIC INPUT",
                                output_mode="text",
                                output_device_id=device,
                                expected_output_private_session_id=session.id,
                            ),
                            str(uuid4()),
                        )
                        self.model.text = "UI10 SYNTHETIC PRIVATE FINAL"
                        await asyncio.to_thread(self.execute, run.run_id)
                        before = await local.get("/local/cloud/snapshot")
                        self.assertEqual(before.status_code, 200)
                        self.assertEqual(
                            before.json()["snapshot"]["active_private_session"]["id"], session.id
                        )
                        # Actual local security envelope: no bearer and CSRF mandatory.
                        refused = await local.post(
                            f"/local/cloud/private-sessions/{session.id}/stop",
                            headers={"Origin": settings.origin},
                        )
                        self.assertEqual(refused.status_code, 403)
                        renewed: list[ResourceId] = []
                        live_evidence: dict[str, Any] = {}

                        async def new_consent() -> None:
                            while not (root / "live-run.request").exists():
                                await asyncio.sleep(0.02)
                            first_conversation = self.conversations.create(
                                self.identity(),
                                self.tenant,
                                ConversationCreate(
                                    agent_id=self.agent_id,
                                    visibility=Visibility.PRIVATE,
                                    display_policy=DisplayPolicy.AUTHENTICATED_SESSION,
                                ),
                                str(uuid4()),
                            )
                            live_run = self.conversations.turn(
                                self.identity(),
                                self.tenant,
                                first_conversation.id,
                                TurnCreate(
                                    text="UI10 LIVE RECONNECT",
                                    output_mode="text",
                                    output_device_id=device,
                                    expected_output_private_session_id=session.id,
                                ),
                                str(uuid4()),
                            )
                            model = HeldModel()
                            self.handler.model = model
                            worker = asyncio.create_task(
                                asyncio.to_thread(self.execute, live_run.run_id)
                            )
                            try:
                                while not model.entered.is_set():
                                    self.assertFalse(worker.done())
                                    await asyncio.sleep(0.01)
                                while (
                                    self.conversations.get_run(
                                        self.identity(), self.tenant, live_run.run_id
                                    ).last_event_seq
                                    < 2
                                ):
                                    self.assertFalse(worker.done())
                                    await asyncio.sleep(0.01)
                                # Seed a prior successfully processed run.started cursor. The
                                # actual service must send history before its real HTTP live stream.
                                if not any(
                                    cursor.run_id == live_run.run_id
                                    for cursor in cloud.journal.cursors
                                ):
                                    cloud.journal.cursors.append(
                                        Cursor(run_id=live_run.run_id, session=session.id, seq=1)
                                    )
                                cloud.save()
                                live_evidence.update(
                                    run=live_run.run_id,
                                    initial_cursor=next(
                                        cursor.seq
                                        for cursor in cloud.journal.cursors
                                        if cursor.run_id == live_run.run_id
                                    ),
                                    running_seq=self.conversations.get_run(
                                        self.identity(), self.tenant, live_run.run_id
                                    ).last_event_seq,
                                )
                                self.assertIn(
                                    live_run.run_id.encode(), cloud.store.read(JOURNAL_NAME)
                                )
                                (root / "live-run.ready").write_text(live_run.run_id)
                                while not (root / "live-run.resume").exists():
                                    await asyncio.sleep(0.02)
                                model.live.set()
                                while not (root / "live-run.finish").exists():
                                    await asyncio.sleep(0.02)
                                model.finish.set()
                                await worker
                                live_evidence["final_status"] = self.conversations.get_run(
                                    self.identity(), self.tenant, live_run.run_id
                                ).status
                                terminal_wire = await original_history(live_run.run_id, session.id)
                                self.assertEqual(terminal_wire["run"]["status"], "completed")
                                self.assertNotIn("next_cursor", terminal_wire["messages"])
                                Path(
                                    os.environ["UI_ARTIFACTS"],
                                    "synthetic-first-terminal-history.json",
                                ).write_text(json.dumps(terminal_wire, indent=2))
                                self.assertTrue(
                                    next(
                                        cursor
                                        for cursor in cloud.journal.cursors
                                        if cursor.run_id == live_run.run_id
                                    ).seq
                                    > 1
                                )
                                (root / "live-run.done").write_text("synthetic")
                            finally:
                                model.live.set()
                                model.finish.set()
                                await asyncio.wait_for(worker, 15)
                                self.handler.model = self.model
                            while not (root / "new-session.request").exists():
                                await asyncio.sleep(0.05)
                            self.assertEqual(
                                self.sessions.get(self.identity(), self.tenant, session.id).status,
                                "active",
                            )
                            self.sessions.end(
                                self.identity(),
                                self.tenant,
                                session.id,
                                str(uuid4()),
                                '"1"',
                                "browser-end",
                            )
                            next_session = self.sessions.start(
                                self.identity(),
                                self.tenant,
                                device,
                                PrivateSessionCreate(
                                    allowed_scopes=["conversation"], acknowledge_shared_space=True
                                ),
                                str(uuid4()),
                                "browser-new",
                            )
                            renewed.append(next_session.id)
                            next_run = self.conversations.turn(
                                self.identity(),
                                self.tenant,
                                conversation.id,
                                TurnCreate(
                                    text="UI10 SECOND SYNTHETIC INPUT",
                                    output_mode="text",
                                    output_device_id=device,
                                    expected_output_private_session_id=next_session.id,
                                ),
                                str(uuid4()),
                            )
                            await asyncio.to_thread(self.execute, next_run.run_id)
                            (root / "new-session.ready").write_text("synthetic")

                        assert application.bridge is not None
                        original_history = application.bridge.history
                        histories: list[dict[str, Any]] = []

                        async def observed_history(
                            run_id: ResourceId, session: ResourceId | None
                        ) -> dict[str, Any]:
                            value = await original_history(run_id, session)
                            if value["run"]["status"] == "running":
                                Path(
                                    os.environ["UI_ARTIFACTS"],
                                    "synthetic-first-running-history.json",
                                ).write_text(json.dumps(value, indent=2))
                            histories.append(
                                {
                                    "run": value["run"]["run_id"],
                                    "status": value["run"]["status"],
                                    "seq": value["run"]["last_event_seq"],
                                    "message_count": len(value["messages"]["items"]),
                                    "has_next_cursor": "next_cursor" in value["messages"],
                                }
                            )
                            Path(
                                os.environ["UI_ARTIFACTS"], "synthetic-local-history.json"
                            ).write_text(json.dumps(histories, indent=2))
                            return value

                        application.bridge.history = observed_history  # type: ignore[method-assign]
                        control = asyncio.create_task(new_consent())
                        try:
                            result = await asyncio.to_thread(
                                subprocess.run,
                                ["node", "tests/ui/private_sessions_browser.mjs"],
                                env={
                                    **os.environ,
                                    "UI_DEVICE_URL": settings.origin,
                                    "UI_DEVICE_NOW": self.clock.now().isoformat(),
                                    "UI_FIXTURE_CONTROL": str(root),
                                },
                                capture_output=True,
                                text=True,
                                timeout=180,
                            )
                        finally:
                            control.cancel()
                            await asyncio.gather(control, return_exceptions=True)
                        print(result.stdout)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        final_cursor = next(
                            cursor
                            for cursor in cloud.journal.cursors
                            if cursor.run_id == live_evidence["run"]
                        )
                        self.assertTrue(final_cursor.terminal)
                        self.assertEqual(live_evidence["final_status"], "completed")
                        live_evidence.update(
                            final_cursor=final_cursor.seq,
                            terminal=final_cursor.terminal,
                            histories=histories,
                        )
                        self.assertTrue(
                            any(
                                value["status"] == "running"
                                and value["seq"] >= 2
                                and value["message_count"] == 1
                                and not value["has_next_cursor"]
                                for value in histories
                            )
                        )
                        Path(os.environ["UI_ARTIFACTS"], "synthetic-resume-result.json").write_text(
                            json.dumps(live_evidence, indent=2)
                        )
                        self.assertEqual(
                            self.sessions.get(self.identity(), self.tenant, renewed[0]).status,
                            "active",
                        )
                        after = await local.get("/local/cloud/snapshot")
                        self.assertIsNone(after.json()["snapshot"]["active_private_session"])
                        self.assertEqual(after.json()["snapshot"]["assigned_runs"], [])
                        self.assertTrue(cloud.active)
                        self.assertIsNotNone(cloud.journal.credentials)
                        print(
                            "UI10 actual loopback DeviceApplication, CSRF, API authorization, private run, durable local stop and current snapshot passed; cloud session remains active until owner end/deadline."
                        )
                finally:
                    server.should_exit = True
                    await asyncio.wait_for(task, 15)
                    self.assertFalse(application.shutdown_failed)
                    self.assertFalse(application.runtime.running)


if __name__ == "__main__":
    unittest.main(verbosity=2)
