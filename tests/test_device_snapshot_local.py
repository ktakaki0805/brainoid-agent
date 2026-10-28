"""Display deduplication through the actual application and concurrent local routes."""

import asyncio
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any, cast
from unittest.mock import patch
from uuid import uuid4

import httpx
from brainoid_api.repositories.reminders import ReminderRequests
from brainoid_api.services.private_sessions import PrivateSessionCreate
from brainoid_contracts import DisplayPolicy, IanaTimezone, ResourceId, Visibility
from brainoid_device.app import DeviceApplication
from brainoid_device.cloud_client import CloudClient
from brainoid_device.config import CloudSettings, Settings
from brainoid_device.ledger import DeviceStorage
from brainoid_device.local_api import LocalBoundary
from brainoid_device.runtime import Runtime
from brainoid_device.storage import CredentialStore
from brainoid_device.ui_bridge import GuardedResponse
from brainoid_memory.reminders import DeliveryTarget, ReminderCreate
from brainoid_runs.models import ConversationCreate, TurnCreate
from brainoid_worker.notifications import DueReminders
from fakes.purge import FakePurgePages
from starlette.applications import Starlette
from starlette.types import Receive, Scope, Send
from test_device_snapshot import SnapshotFixture


class SnapshotLocalTests(SnapshotFixture):
    @asynccontextmanager
    async def local(
        self,
    ) -> AsyncIterator[tuple[DeviceApplication, httpx.AsyncClient, dict[str, str], ResourceId]]:
        directory = self.root / "snapshot-application"
        directory.mkdir(mode=0o700)
        for name in ("credentials", "state", "assets"):
            (directory / name).mkdir(mode=0o700)
        (directory / "assets/index.html").write_text(
            "<!doctype html><title>Synthetic snapshot</title>"
        )
        settings = Settings(
            "127.0.0.1",
            18766,
            directory / "assets",
            directory / "credentials",
            directory / "state",
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
            application = DeviceApplication(settings)
        cloud = application.cloud
        assert cloud is not None
        assert isinstance(application.app, LocalBoundary)
        assert isinstance(application.app.app, Starlette)
        async with application.app.app.router.lifespan_context(application.app.app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=application.app, client=("127.0.0.1", 5510)),
                base_url=settings.origin,
            ) as browser:
                boot = await browser.post(
                    "/local/session", headers={"Origin": settings.origin, "X-Brainoid-Local": "1"}
                )
                self.assertEqual(boot.status_code, 200)
                headers = {"Origin": settings.origin, "X-CSRF-Token": boot.json()["csrf_token"]}
                paired = await browser.post(
                    "/local/cloud/pairing",
                    headers=headers,
                    json={"device_name": "Synthetic snapshot device"},
                )
                self.assertEqual(paired.status_code, 200)
                self.confirm(cloud.journal.pairing)  # type: ignore[arg-type]
                await cloud.exchange()
                credentials = cloud.journal.credentials
                assert credentials is not None
                self.store.data[f"tenants/{self.tenant}/devices/{credentials.device_id}"][
                    "capabilities"
                ] = ["display"]
                ReminderRequests(self.memories).create(
                    self.identity(),
                    self.tenant,
                    ReminderCreate(
                        title="CONCURRENT_DISPLAY_SYNTHETIC",
                        due_at=self.clock.now() + timedelta(seconds=1),
                        timezone=IanaTimezone("UTC"),
                        visibility=Visibility.SHARED,
                        display_policy=DisplayPolicy.SHARED_DISPLAY,
                        delivery_target=DeliveryTarget(kind="device", id=credentials.device_id),
                    ),
                    "local-concurrent-display-reminder",
                )
                self.clock.advance(1)
                DueReminders(self.memories.jobs, FakePurgePages(cast(Any, self.store))).scan(
                    self.tenant
                )
                current = await browser.get("/local/cloud/snapshot")
                self.assertEqual(current.status_code, 200, current.text)
                notification = ResourceId(current.json()["notifications"][0]["id"])
                yield application, browser, headers, notification
        self.assertFalse(application.runtime.running)
        self.assertTrue(cloud.http.is_closed)
        self.assertFalse(cloud._tasks)
        self.assertFalse(application.runtime.resource_failed)

    async def delayed_display(self, stage: str) -> None:
        async with self.local() as (application, browser, headers, notification):
            cloud, sync = application.cloud, application.snapshot_sync
            assert cloud is not None and sync is not None
            reached, release = asyncio.Event(), asyncio.Event()
            authorized, publication, respond = (
                cloud.authorized,
                sync.publication,
                GuardedResponse.__call__,
            )
            output_generation = cloud.output_generation

            async def held_get(method: str, path: str, **kwargs: Any) -> tuple[int, dict[str, Any]]:
                result = await authorized(method, path, **kwargs)
                if (
                    stage == "read"
                    and method == "GET"
                    and path.endswith("/notifications/" + notification)
                    and result[0] == 200
                ):
                    self.assertEqual(result[1]["status"], "pending")
                    reached.set()
                    await release.wait()
                return result

            async def held_publication() -> tuple[dict[str, Any], Callable[[], None]]:
                result = await publication()
                if stage == "handoff":
                    reached.set()
                    await release.wait()
                return result

            async def held_response(
                response: GuardedResponse, scope: Scope, receive: Receive, send: Send
            ) -> None:
                if stage == "send" and scope["path"] == "/local/cloud/snapshot":
                    reached.set()
                    await release.wait()
                await respond(response, scope, receive, send)

            with (
                patch.object(cloud, "authorized", side_effect=held_get),
                patch.object(sync, "publication", side_effect=held_publication),
                patch.object(GuardedResponse, "__call__", held_response),
            ):
                pending = asyncio.create_task(browser.get("/local/cloud/snapshot"))
                try:
                    await asyncio.wait_for(reached.wait(), 3)
                    displayed = await browser.post(
                        f"/local/cloud/notifications/{notification}/displayed", headers=headers
                    )
                    self.assertEqual(displayed.status_code, 200, displayed.text)
                    delivery = next(
                        d
                        for p, d in self.store.data.items()
                        if "/deliveries/" in p and d.get("notification_id") == notification
                    )
                    self.assertIsNotNone(delivery["displayed_at"])
                    self.assertIsNone(delivery["acknowledged_at"])
                    self.assertFalse(sync.load().displayed)
                    self.assertEqual(cloud.output_generation, output_generation)
                    release.set()
                    delayed = await asyncio.wait_for(pending, 3)
                    self.assertEqual(
                        delayed.status_code, 200 if stage == "read" else 409, delayed.text
                    )
                    if stage == "read":
                        self.assertTrue(delayed.json()["ready"])
                        self.assertEqual(delayed.json()["notifications"], [])
                    else:
                        self.assertNotIn("CONCURRENT_DISPLAY_SYNTHETIC", delayed.text)
                    self.assertIsNone(sync.notification_read)
                    self.assertTrue(cloud.active)
                    self.assertIsNotNone(cloud.journal.credentials)
                finally:
                    release.set()
                    await asyncio.gather(pending, return_exceptions=True)
            self.assertEqual(
                (await browser.get("/local/cloud/snapshot")).json()["notifications"], []
            )
            self.assertNotIn(
                b"CONCURRENT_DISPLAY_SYNTHETIC",
                application.credentials.read("snapshot-recovery-v1"),
            )

    async def test_display_ack_success_suppresses_older_pending_get(self) -> None:
        await self.delayed_display("read")

    async def test_display_after_snapshot_build_before_owned_handoff_suppresses_publication(
        self,
    ) -> None:
        await self.delayed_display("handoff")

    async def test_display_after_response_build_before_asgi_send_suppresses_publication(
        self,
    ) -> None:
        await self.delayed_display("send")

    async def test_scope_clear_between_publication_build_and_owned_handoff_is_fenced(self) -> None:
        async with self.local() as (application, browser, _headers, _notification):
            cloud, sync = application.cloud, application.snapshot_sync
            assert cloud is not None and sync is not None
            reached, release = asyncio.Event(), asyncio.Event()
            original = sync.publication

            async def held() -> tuple[dict[str, Any], Callable[[], None]]:
                result = await original()
                reached.set()
                await release.wait()
                return result

            with patch.object(sync, "publication", side_effect=held):
                pending = asyncio.create_task(browser.get("/local/cloud/snapshot"))
                try:
                    await asyncio.wait_for(reached.wait(), 3)
                    sync.clear()
                    release.set()
                    response = await asyncio.wait_for(pending, 3)
                    self.assertEqual(response.status_code, 409)
                    self.assertNotIn("CONCURRENT_DISPLAY_SYNTHETIC", response.text)
                    self.assertTrue(cloud.active)
                finally:
                    release.set()
                    await asyncio.gather(pending, return_exceptions=True)

    async def private_operation(
        self, application: DeviceApplication, browser: httpx.AsyncClient
    ) -> tuple[ResourceId, dict[str, str]]:
        cloud, sync = application.cloud, application.snapshot_sync
        assert cloud is not None and sync is not None and cloud.journal.credentials is not None
        device = cloud.journal.credentials.device_id
        session = self.sessions.start(
            self.identity(),
            self.tenant,
            device,
            PrivateSessionCreate(allowed_scopes=["conversation"], acknowledge_shared_space=True),
            "local-operation-session",
            "local-operation-session-request",
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
        self.conversations.turn(
            self.identity(),
            self.tenant,
            conversation.id,
            TurnCreate(
                text="PRIVATE_OPERATION_SYNTHETIC", output_mode="text", output_device_id=device
            ),
            str(uuid4()),
        )
        response = await browser.get("/local/cloud/snapshot")
        self.assertEqual(response.status_code, 200, response.text)
        current = sync.session()
        assert current is not None
        self.assertEqual(current.id, session.id)
        return session.id, {
            "operation": "list_messages",
            "conversation_id": conversation.id,
            "private_session_id": session.id,
        }

    async def test_natural_owned_task_done_revoke_callback_cannot_reauthorize_old_body(
        self,
    ) -> None:
        async with self.local() as (application, browser, headers, _notification):
            cloud, sync = application.cloud, application.snapshot_sync
            assert cloud is not None and sync is not None
            session, operation = await self.private_operation(application, browser)
            original_start = cloud.start
            observations: list[tuple[bool, bool, int]] = []
            generation = cloud.output_generation

            def start(work: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
                task = original_start(work)

                def end(completed: asyncio.Task[None]) -> None:
                    self.sessions.end(
                        self.identity(),
                        self.tenant,
                        session,
                        "natural-operation-end",
                        '"1"',
                        "natural-operation-end-request",
                    )
                    sync.session_hint("session_revoked", session)
                    assert cloud.journal.credentials is not None
                    canonical = self.snapshots.page(
                        self.repo.authenticate(cloud.journal.credentials.access_token)
                    ).release()
                    observations.append(
                        (
                            completed.done(),
                            canonical.active_private_session is None,
                            cloud.output_generation,
                        )
                    )

                # Registered before owned() awaits the real task: no await is
                # inserted into the operation, publication, or owned coroutine.
                task.add_done_callback(end)
                return task

            with patch.object(cloud, "start", side_effect=start):
                response = await browser.post(
                    "/local/cloud/operations", headers=headers, json=operation
                )
            self.assertEqual(observations, [(True, True, generation + 1)])
            self.assertEqual(response.status_code, 409, response.text)
            self.assertIn("private_state_changed", response.text)
            self.assertNotIn("PRIVATE_OPERATION_SYNTHETIC", response.text)
            self.assertIsNone(sync.session())
            self.assertTrue(cloud.active)
            self.assertIsNotNone(cloud.journal.credentials)
            self.assertFalse(application.runtime.resource_failed)

    async def operation_handoff(self, mode: str) -> None:
        async with self.local() as (application, browser, headers, notification):
            cloud, sync, bridge = application.cloud, application.snapshot_sync, application.bridge
            assert cloud is not None and sync is not None and bridge is not None
            session, operation = await self.private_operation(application, browser)
            original, respond = bridge.operation, GuardedResponse.__call__
            reached, release = asyncio.Event(), asyncio.Event()
            generation = cloud.output_generation

            async def held(operation: Any) -> dict[str, Any]:
                result = await original(operation)
                self.assertIn("PRIVATE_OPERATION_SYNTHETIC", str(result))
                if mode != "send_expiry":
                    reached.set()
                    await release.wait()
                return result

            async def held_response(
                response: GuardedResponse, scope: Scope, receive: Receive, send: Send
            ) -> None:
                if mode == "send_expiry" and scope["path"] == "/local/cloud/operations":
                    reached.set()
                    await release.wait()
                await respond(response, scope, receive, send)

            with (
                patch.object(bridge, "operation", side_effect=held),
                patch.object(GuardedResponse, "__call__", held_response),
            ):
                pending = asyncio.create_task(
                    browser.post("/local/cloud/operations", headers=headers, json=operation)
                )
                try:
                    await asyncio.wait_for(reached.wait(), 3)
                    if mode == "display":
                        displayed = await browser.post(
                            f"/local/cloud/notifications/{notification}/displayed", headers=headers
                        )
                        self.assertEqual(displayed.status_code, 200, displayed.text)
                        self.assertEqual(cloud.output_generation, generation)
                    elif mode == "send_expiry":
                        self.clock.advance(61)
                        self.assertIsNone(sync.session())
                    else:
                        self.sessions.end(
                            self.identity(),
                            self.tenant,
                            session,
                            "held-operation-end",
                            '"1"',
                            "held-operation-end-request",
                        )
                        if mode == "replacement":
                            assert cloud.journal.credentials is not None
                            replacement = self.sessions.start(
                                self.identity(),
                                self.tenant,
                                cloud.journal.credentials.device_id,
                                PrivateSessionCreate(
                                    allowed_scopes=["conversation"], acknowledge_shared_space=True
                                ),
                                "held-operation-new-session",
                                "held-operation-new-request",
                            )
                            await sync.sync()
                            current = sync.session()
                            assert current is not None
                            self.assertEqual(current.id, replacement.id)
                        else:
                            await sync.sync()
                            self.assertIsNone(sync.session())
                    release.set()
                    response = await asyncio.wait_for(pending, 3)
                    if mode == "display":
                        self.assertEqual(response.status_code, 200, response.text)
                        self.assertIn("PRIVATE_OPERATION_SYNTHETIC", response.text)
                    else:
                        self.assertEqual(response.status_code, 409, response.text)
                        self.assertNotIn("PRIVATE_OPERATION_SYNTHETIC", response.text)
                        self.assertNotIn("snapshot_changed", response.text)
                    self.assertTrue(cloud.active)
                    self.assertIsNotNone(cloud.journal.credentials)
                    self.assertFalse(application.runtime.resource_failed)
                finally:
                    release.set()
                    await asyncio.gather(pending, return_exceptions=True)

    async def test_completed_operation_handoff_is_fenced_after_canonical_end_and_sync(self) -> None:
        await self.operation_handoff("end")

    async def test_completed_operation_cannot_adopt_replacement_session(self) -> None:
        await self.operation_handoff("replacement")

    async def test_operation_expiry_after_response_build_blocks_asgi_send(self) -> None:
        await self.operation_handoff("send_expiry")

    async def test_notification_display_does_not_invalidate_authorized_operation(self) -> None:
        await self.operation_handoff("display")
