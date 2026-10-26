"""T-023/T-032: durable batches, resume, delivery fences and terminal atomicity."""

import asyncio
import json
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any
from unittest.mock import patch

from brainoid_agent_core.core import RunBudget
from brainoid_agent_core.ports import CallControl
from brainoid_agent_core.types import Completed, ModelEvent, ModelRequest, TextDelta
from brainoid_api.errors import APIError, ErrorCode
from brainoid_api.routes.run_events import next_batch, stream
from brainoid_contracts import ResourceId
from brainoid_jobs.models import job_id
from brainoid_persistence.firestore import PersistenceUnavailable
from brainoid_runs.events import chunks, event_path
from brainoid_runs.state import KIND, conversation_path, run_path
from brainoid_worker.run_event_writer import RunEventWriter
from fakes.runtime import FakeClock
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from test_turn_run import TurnRunFixture


class RunEventTests(TurnRunFixture):
    def events(self, run: ResourceId) -> list[dict[str, Any]]:
        return sorted(
            [
                d
                for p, d in self.store.data.items()
                if p.startswith(run_path(self.tenant, run) + "/events/")
            ],
            key=lambda d: d["seq"],
        )

    def completed(self) -> tuple[ResourceId, ResourceId]:
        conversation = self.conversation()
        run = ResourceId(self.turn(conversation).json()["run_id"])
        self.execute(run)
        return conversation, run

    def test_schema_replay_and_terminal_message_match(self) -> None:
        self.model.text = "日🙂a" * 900
        conversation, run = self.completed()
        events = self.events(run)
        self.assertEqual(events[0]["type"], "run.started")
        self.assertEqual(events[-1]["type"], "run.completed")
        self.assertEqual([e["seq"] for e in events], list(range(1, len(events) + 1)))
        validator = Draft202012Validator(
            json.loads(Path("specs/run-events.schema.json").read_text())
        )
        for event in events:
            validator.validate({k: v for k, v in event.items() if k != "expires_at"})
        text = "".join(e["payload"]["text"] for e in events if e["type"] == "text.delta")
        messages = self.conversations.messages(self.identity(), self.tenant, conversation).items
        self.assertEqual(text, messages[-1].text)
        self.assertEqual(text, self.model.text)
        self.assertTrue(
            all(
                len(e["payload"]["text"].encode()) <= 1024
                for e in events
                if e["type"] == "text.delta"
            )
        )
        url = f"/v1/tenants/{self.tenant}/runs/{run}/events"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertNotIn("PRIVATE_SIGNATURE", response.text)
        self.assertNotIn("expires_at", response.text)
        resumed = self.client.get(url, headers={"Last-Event-ID": "2"})
        ids = [int(line[4:]) for line in resumed.text.splitlines() if line.startswith("id: ")]
        self.assertEqual(ids, list(range(3, len(events) + 1)))
        self.assertEqual(self.client.get(url, headers={"Last-Event-ID": str(len(events))}).text, "")

    def test_bad_cursor_missing_history_and_fixed_expiry(self) -> None:
        _, run = self.completed()
        url = f"/v1/tenants/{self.tenant}/runs/{run}/events"
        for cursor in ("-1", "1.0", "1 ", "1" * 17):
            self.assertEqual(
                self.client.get(url, headers={"Last-Event-ID": cursor}).status_code, 400
            )
        self.assertEqual(self.client.get(url, headers={"Last-Event-ID": "999"}).status_code, 409)
        old = self.store.data[event_path(self.store.data[run_path(self.tenant, run)], 2)]
        self.clock.advance(100)
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(
            old, self.store.data[event_path(self.store.data[run_path(self.tenant, run)], 2)]
        )
        del self.store.data[event_path(self.store.data[run_path(self.tenant, run)], 2)]
        self.assertEqual(self.client.get(url, headers={"Last-Event-ID": "1"}).status_code, 410)
        self.clock.advance(24 * 3600 - 100)
        # Use freshly issued identity; token expiry must not mask history expiry.
        self.assertEqual(self.client.get(url).status_code, 410)
        self.assertEqual(self.get_run(run).status_code, 200)

    def test_disconnect_and_each_next_batch_rechecks_deletion(self) -> None:
        conversation, run = self.completed()

        async def check() -> None:
            iterator = stream(self.conversations, self.identity(), self.tenant, run, None, 0)
            self.assertIn("id: 1", await anext(iterator))
            await iterator.aclose()
            iterator = stream(self.conversations, self.identity(), self.tenant, run, None, 1)
            self.assertIn("id: 2", await anext(iterator))
            self.store.data[conversation_path(self.tenant, conversation)]["deleted"] = True
            self.assertEqual([frame async for frame in iterator], [])

        asyncio.run(check())
        self.assertEqual(
            self.client.get(f"/v1/tenants/{self.tenant}/runs/{run}/events").status_code, 404
        )

    def test_membership_revocation_and_wrong_tenant_never_deliver(self) -> None:
        _, run = self.completed()

        async def check() -> None:
            iterator = stream(self.conversations, self.identity(), self.tenant, run, None, 0)
            await anext(iterator)
            self.store.data[f"tenants/{self.tenant}/memberships/{self.uid}"]["status"] = "revoked"
            self.assertEqual([frame async for frame in iterator], [])

        asyncio.run(check())
        self.assertEqual(
            self.client.get(
                f"/v1/tenants/00000000-0000-4000-8000-000000000000/runs/{run}/events"
            ).status_code,
            404,
        )

    def test_terminal_checkpoint_replay_and_ambiguous_commit_never_duplicate(self) -> None:
        conversation = self.conversation()
        run = ResourceId(self.turn(conversation).json()["run_id"])
        with patch.object(self.jobs, "complete", side_effect=PersistenceUnavailable()):
            with self.assertRaises(PersistenceUnavailable):
                self.execute(run)
        self.assertEqual([e["type"] for e in self.events(run)], ["run.started"])
        self.clock.advance(6)
        complete = self.jobs.complete

        def lose(*args: Any, **kwargs: Any) -> bool:
            complete(*args, **kwargs)
            raise PersistenceUnavailable()

        with patch.object(self.jobs, "complete", side_effect=lose):
            with self.assertRaises(PersistenceUnavailable):
                self.execute(run)
        self.assertEqual(
            [e["type"] for e in self.events(run)], ["run.started", "text.delta", "run.completed"]
        )
        self.assertEqual(self.get_run(run).json()["last_event_seq"], 3)
        self.assertEqual(len(self.model.requests), 1)
        self.assertEqual(
            len(self.conversations.messages(self.identity(), self.tenant, conversation).items), 2
        )

    def test_failed_projection_has_one_terminal_event(self) -> None:
        conversation = self.conversation()
        run = ResourceId(self.turn(conversation).json()["run_id"])
        job_path = f"tenants/{self.tenant}/jobs/{job_id(self.tenant, KIND, run)}"
        self.store.data[job_path].update(status="failed", reason="outcome_unknown")
        for _ in range(2):
            observed = self.get_run(run).json()
            self.assertEqual(observed["status"], "failed")
            self.assertEqual(observed["last_event_seq"], 1)
        self.assertEqual([e["type"] for e in self.events(run)], ["run.failed"])
        batch = next_batch(self.conversations, self.identity(), self.tenant, run, None, 0)
        assert batch.event is not None
        self.assertEqual(batch.event.type, "run.failed")
        self.assertEqual(batch.event.payload.model_dump()["error_code"], "outcome_unknown")

    def test_fixed_token_stream_is_batched_and_length_matches_final(self) -> None:
        class Tokens:
            async def stream(
                self, request: ModelRequest, *, control: CallControl
            ) -> AsyncGenerator[ModelEvent]:
                for _ in range(300):
                    yield TextDelta(text="日")
                yield Completed(finish_reason="stop")

        self.handler.model = Tokens()
        self.handler.budget = RunBudget(max_output_characters=250)
        from test_turn_run import StrictTransaction

        writes: list[str] = []
        original_set = StrictTransaction.set

        def count(tx: StrictTransaction, path: str, document: Any) -> None:
            if "/runs/" in path:
                writes.append(path)
            original_set(tx, path, document)

        with patch.object(StrictTransaction, "set", count):
            conversation, run = self.completed()
        self.assertLess(len(writes), 40)  # Includes run metadata and event documents.
        events = self.events(run)
        deltas = [e for e in events if e["type"] == "text.delta"]
        self.assertLess(len(deltas), 20)
        text = "".join(e["payload"]["text"] for e in deltas)
        self.assertEqual(text, "日" * 250)
        self.assertEqual(
            self.conversations.messages(self.identity(), self.tenant, conversation).items[-1].text,
            text,
        )
        self.assertEqual(chunks("🙂" * 300), ["🙂" * 256, "🙂" * 44])

    def test_timer_flushes_while_provider_is_paused_and_failure_has_no_message(self) -> None:
        seen: list[str] = []
        test = self
        conversation = self.conversation()
        run = ResourceId(self.turn(conversation).json()["run_id"])

        class Paused:
            async def stream(
                self, request: ModelRequest, *, control: CallControl
            ) -> AsyncGenerator[ModelEvent]:
                yield TextDelta(text="Fixture partial")
                await asyncio.sleep(0.4)
                seen.extend(e["type"] for e in test.events(run))
                raise RuntimeError("PRIVATE_FAILURE")

        self.handler.model = Paused()
        self.execute(run)
        self.assertIn("text.delta", seen)
        self.assertEqual(self.get_run(run).json()["status"], "failed")
        self.assertEqual(self.events(run)[-1]["type"], "run.failed")
        self.assertEqual(
            len(self.conversations.messages(self.identity(), self.tenant, conversation).items), 1
        )

    def test_heartbeat_has_no_seq_or_activity_write(self) -> None:
        conversation = self.conversation()
        run = ResourceId(self.turn(conversation).json()["run_id"])
        before = self.store.data.copy()

        async def check() -> None:
            with patch("brainoid_api.routes.run_events.HEARTBEAT_SECONDS", 0):
                iterator = stream(self.conversations, self.identity(), self.tenant, run, None, 0)
                self.assertEqual(await anext(iterator), ": heartbeat\n\n")
                await iterator.aclose()

        asyncio.run(check())
        self.assertEqual(self.store.data, before)

    def test_manual_clock_timer_and_size_window(self) -> None:
        class ManualClock(FakeClock):
            async def sleep(self, seconds: float) -> None:
                deadline = self.monotonic() + seconds
                while self.monotonic() < deadline:
                    await asyncio.sleep(0)

        class Bridge:
            def __init__(self) -> None:
                self.text: list[str] = []

            async def append(self, *args: Any) -> None:
                self.text.append(args[4])

        async def check() -> None:
            clock, bridge = ManualClock(), Bridge()
            # Writer batching is independent of storage; real lease TX is covered above.
            writer = RunEventWriter(self.jobs, self.handler.state, None, clock, bridge)  # type: ignore[arg-type]
            writer.start()
            await writer.add("a")
            await asyncio.sleep(0)
            clock.advance(0.249)
            await asyncio.sleep(0)
            self.assertEqual(bridge.text, [])
            clock.advance(0.001)
            for _ in range(3):
                await asyncio.sleep(0)
            self.assertEqual(bridge.text, ["a"])
            await writer.add("🙂" * 256)
            self.assertEqual(bridge.text, ["a", "🙂" * 256])
            await writer.close()
            self.assertTrue(writer.timer is not None and writer.timer.done())

        asyncio.run(check())

    def test_delivery_rechecks_bearer_expiry_after_event_read(self) -> None:
        _, run = self.completed()
        identity = self.identity()
        from test_turn_run import StrictTransaction

        original = StrictTransaction.get

        def delayed(tx: StrictTransaction, path: str) -> Any:
            result = original(tx, path)
            if "/events/" in path:
                self.clock.advance(3600)
            return result

        with patch.object(StrictTransaction, "get", delayed):
            with self.assertRaises(APIError) as caught:
                next_batch(self.conversations, identity, self.tenant, run, None, 0)
        self.assertEqual(caught.exception.code, ErrorCode.AUTHENTICATION_REQUIRED)

    def test_revocation_during_event_io_is_rechecked_in_fresh_transaction(self) -> None:
        conversation, run = self.completed()
        from test_turn_run import StrictTransaction

        original = StrictTransaction.get

        def delayed(tx: StrictTransaction, path: str) -> Any:
            result = original(tx, path)
            if "/events/" in path:
                tx.data[conversation_path(self.tenant, conversation)]["deleted"] = True
            return result

        with patch.object(StrictTransaction, "get", delayed):
            with self.assertRaises(APIError) as caught:
                next_batch(self.conversations, self.identity(), self.tenant, run, None, 0)
        self.assertEqual(caught.exception.code, ErrorCode.RESOURCE_NOT_FOUND)

    def test_private_output_session_and_family_revocation_stop_next_batch(self) -> None:
        import copy

        from brainoid_api.routes.agents import reader_identity
        from brainoid_api.services.private_sessions import PrivateSessionCreate
        from brainoid_contracts import DisplayPolicy

        credentials = self.credentials()
        device_path = f"tenants/{self.tenant}/devices/{credentials.device_id}"
        self.store.data[device_path]["capabilities"] = ["display"]
        identity = self.repo.authenticate(credentials.access_token)
        session = self.sessions.start(
            self.identity(),
            self.tenant,
            identity.sub,
            PrivateSessionCreate(acknowledge_shared_space=True, allowed_scopes=["conversation"]),
            "fixture-sse-session",
            "fixture-request",
        )
        conversation = self.conversation(private=True, display=DisplayPolicy.AUTHENTICATED_SESSION)
        accepted = self.client.post(
            self.url + f"/{conversation}/turns",
            json={
                "text": "Fixture private output",
                "output_mode": "text",
                "output_device_id": identity.sub,
            },
            headers={"Idempotency-Key": "fixture-sse-output"},
        )
        self.assertEqual(accepted.status_code, 202)
        run = ResourceId(accepted.json()["run_id"])
        self.execute(run)
        self.app.dependency_overrides[reader_identity] = lambda: identity
        url = f"/v1/tenants/{self.tenant}/runs/{run}/events"
        headers = {"X-Private-Session": session.id}
        saved = copy.deepcopy(self.store.data)
        self.assertEqual(self.client.get(url, headers=headers).status_code, 200)
        self.assertEqual(self.store.data, saved)
        for path, field, value in (
            (device_path, "status", "revoked"),
            (device_path, "connection_generation", 2),
            ("device_credential_families/" + identity.family_id, "status", "revoked"),
            (f"tenants/{self.tenant}/private_sessions/{session.id}", "status", "revoked"),
            (
                f"tenants/{self.tenant}/private_session_slots/{identity.sub}",
                "session_id",
                "00000000-0000-4000-8000-000000000000",
            ),
        ):
            with self.subTest(field=field, path=path):
                self.store.data = copy.deepcopy(saved)
                self.store.data[path][field] = value
                self.assertIn(self.client.get(url, headers=headers).status_code, (401, 404))
        self.store.data = copy.deepcopy(saved)
        self.app.dependency_overrides[reader_identity] = lambda: self.identity()
        self.store.data[f"tenants/{self.tenant}/private_sessions/{session.id}"]["status"] = (
            "revoked"
        )
        # The smartphone's valid user token cannot override a revoked output session.
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_write_blocked_until_after_deadline_never_appends_late_text(self) -> None:
        from threading import Event

        from brainoid_worker.run_event_writer import WriteBridge

        entered, release, finished = Event(), Event(), Event()
        original_run = self.jobs.store.run
        conversation = self.conversation()
        run = ResourceId(self.turn(conversation).json()["run_id"])
        delivery = self.delivery(run)
        lease = self.jobs.acquire(delivery, known_handler=lambda _: True)
        assert lease is not None
        job = self.jobs.check(lease)
        bridge = WriteBridge()
        stopped = Event()

        def delayed(work: Any) -> Any:
            entered.set()
            release.wait(2)
            try:
                return original_run(work)
            finally:
                finished.set()

        async def check() -> None:
            task = asyncio.create_task(
                bridge.append(self.jobs, self.handler.state, job, "", "late", stopped)
            )
            while not entered.is_set():
                await asyncio.sleep(0.001)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        with patch.object(self.jobs.store, "run", delayed):
            asyncio.run(check())
        self.clock.advance(61)
        release.set()
        self.assertTrue(finished.wait(2))
        self.assertEqual(self.events(run), [])
        self.assertTrue(stopped.is_set())

    def test_shared_subscriber_is_independent_of_original_user_output(self) -> None:
        from brainoid_api.routes.agents import reader_identity
        from brainoid_contracts import FirebaseUid

        _, run = self.completed()
        member = self.identity().model_copy(update={"user_id": FirebaseUid("demo-a-member")})
        self.app.dependency_overrides[reader_identity] = lambda: member
        response = self.client.get(f"/v1/tenants/{self.tenant}/runs/{run}/events")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("run.completed", response.text)
        self.assertIn(self.model.text, response.text)

    def test_device_bearer_expiry_during_event_read(self) -> None:
        from test_turn_run import StrictTransaction

        credentials = self.credentials()
        identity = self.repo.authenticate(credentials.access_token)
        _, run = self.completed()
        original = StrictTransaction.get

        def delayed(tx: StrictTransaction, path: str) -> Any:
            result = original(tx, path)
            if "/events/" in path:
                self.clock.advance(identity.exp - self.clock.now().timestamp())
            return result

        with patch.object(StrictTransaction, "get", delayed):
            with self.assertRaises(APIError) as caught:
                next_batch(self.conversations, identity, self.tenant, run, None, 0)
        self.assertEqual(caught.exception.code, ErrorCode.AUTHENTICATION_REQUIRED)

    def test_writer_rechecks_revocation_and_stale_lease_before_append(self) -> None:
        import copy
        from threading import Event

        from brainoid_policy.scopes import PolicyDenied
        from brainoid_worker.run_event_writer import WriteBridge

        conversation = self.conversation()
        run = ResourceId(self.turn(conversation).json()["run_id"])
        delivery = self.delivery(run)
        lease = self.jobs.acquire(delivery, known_handler=lambda _: True)
        assert lease is not None
        job = self.jobs.check(lease)
        saved = copy.deepcopy(self.store.data)
        for path, field, value in (
            (conversation_path(self.tenant, conversation), "deleted", True),
            (f"tenants/{self.tenant}/jobs/{job.id}", "lease_generation", job.lease_generation + 1),
            (f"tenants/{self.tenant}/memberships/{self.uid}", "status", "revoked"),
        ):
            with self.subTest(field=field):
                self.store.data = copy.deepcopy(saved)
                self.store.data[path][field] = value
                with self.assertRaises(PolicyDenied):
                    asyncio.run(
                        WriteBridge().append(
                            self.jobs, self.handler.state, job, "", "late", Event()
                        )
                    )
                self.assertEqual(self.events(run), [])

    def test_other_member_cannot_receive_after_output_user_account_revocation(self) -> None:
        import copy

        from brainoid_contracts import FirebaseUid

        _, run = self.completed()
        member = self.identity().model_copy(update={"user_id": FirebaseUid("demo-a-member")})
        saved = copy.deepcopy(self.store.data)
        profile = next(
            (
                path
                for path, doc in saved.items()
                if path.startswith("users/") and doc.get("user_id") == self.uid
            ),
            None,
        )
        for change in ("deleting", "version"):
            with self.subTest(change=change):
                self.store.data = copy.deepcopy(saved)
                if profile is None:
                    # Provisioning a formerly unprovisioned output changes its saved stamp.
                    path = "users/synthetic-output-profile"
                    self.store.data[path] = {
                        "user_id": self.uid,
                        "display_name": "Fixture",
                        "timezone": "Asia/Tokyo",
                        "version": 1,
                        "status": "active",
                    }
                else:
                    path = profile
                if change == "deleting":
                    self.store.data[path]["status"] = "deleting"
                else:
                    self.store.data[path]["version"] += 1
                with self.assertRaises(APIError) as caught:
                    next_batch(self.conversations, member, self.tenant, run, None, 0)
                self.assertEqual(caught.exception.code, ErrorCode.RESOURCE_NOT_FOUND)
