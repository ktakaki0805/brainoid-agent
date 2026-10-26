"""RUN-04: durable acceptance, current parent/child authority and bounded text results."""

import asyncio
import copy
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import yaml  # type: ignore[import-untyped]
from brainoid_agent_core.core import RunBudget
from brainoid_api.auth.runtime import AuthServices, get_auth_services
from brainoid_api.common.pagination import Cursors
from brainoid_api.repositories.conversations import Conversations
from brainoid_api.routes.agents import reader_identity
from brainoid_api.services.private_sessions import PrivateSessionCreate, PrivateSessions
from brainoid_contracts import DisplayPolicy, ResourceId, Visibility
from brainoid_contracts.transactions import Document, Transaction
from brainoid_jobs import AuthorizationUnavailable, Jobs
from brainoid_jobs.models import job_id
from brainoid_persistence.firestore import PersistenceUnavailable
from brainoid_policy.scopes import PolicyUnavailable
from brainoid_runs.models import ConversationCreate, ConversationPatch
from brainoid_runs.state import KIND, conversation_path
from brainoid_worker.entry import Worker
from brainoid_worker.local import LOCAL_AUTHORIZATION, LocalCaller
from brainoid_worker.run_turn import ReadBridge, RunTurn, SystemClock
from fakes.turn_model import Model
from fastapi import FastAPI
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from test_common_mutations import MemoryTransaction
from test_device_pairing import DevicePairingFixture
from test_jobs import JobStore


class StrictTransaction(MemoryTransaction):
    """Make Firestore's read-before-write rule visible to the fast tests."""

    wrote = False

    def get(self, path: str) -> Document | None:
        assert not self.wrote, "read after write"
        return super().get(path)

    def collection(self, path: str) -> list[tuple[str, Document]]:
        assert not self.wrote, "read after write"
        return super().collection(path)

    def find(self, collection: str, field: str, value: str) -> list[Document]:
        assert not self.wrote, "read after write"
        return super().find(collection, field, value)

    def set(self, path: str, document: Document) -> None:
        self.wrote = True
        super().set(path, document)


class StrictStore(JobStore):
    def run[T](self, work: Callable[[Transaction], T]) -> T:
        with self.lock:
            if self.retry_once:
                self.retry_once = False
                work(StrictTransaction(self.data))
            tx = StrictTransaction(self.data)
            result = work(tx)
            self.data = tx.data
            return result


class TurnRunTests(DevicePairingFixture):
    def setUp(self) -> None:
        with patch("test_device_pairing.MemoryStore", StrictStore):
            super().setUp()
        self.jobs = Jobs(
            self.store, self.ledger, now=self.clock.now, allow_untracked_bootstrap=True
        )
        self.sessions = PrivateSessions(
            self.store, self.tenants, self.ledger, self.repo, now=self.clock.now
        )
        self.conversations = Conversations(
            self.store,
            self.tenants,
            Cursors(b"r" * 32, now=self.clock.now),
            self.jobs,
            credentials=self.repo,
            sessions=self.sessions,
            now=self.clock.now,
        )
        self.app = cast(FastAPI, self.client.app)
        self.app.dependency_overrides[get_auth_services] = lambda: AuthServices(
            tenants=self.tenants, devices=self.repo, conversations=self.conversations
        )
        self.app.dependency_overrides[reader_identity] = lambda: self.identity()
        self.agent_id = ResourceId(
            next(
                d["id"]
                for p, d in self.store.data.items()
                if p.startswith(f"tenants/{self.tenant}/agents/")
            )
        )
        self.url = f"/v1/tenants/{self.tenant}/conversations"
        self.model = Model()
        self.handler = RunTurn(
            self.jobs, self.model, credential_epoch=self.tokens.epoch, clock=SystemClock()
        )
        self.worker = Worker(self.jobs, LocalCaller(), {KIND: self.handler})

    def conversation(
        self, *, private: bool = False, display: DisplayPolicy | None = None
    ) -> ResourceId:
        body = ConversationCreate(
            agent_id=self.agent_id,
            title="Fixture conversation",
            visibility=Visibility.PRIVATE if private else Visibility.SHARED,
            display_policy=display
            or (DisplayPolicy.NEVER if private else DisplayPolicy.SHARED_DISPLAY),
        )
        result = self.client.post(
            self.url,
            json=body.model_dump(mode="json"),
            headers={"Idempotency-Key": "conversation-key-" + str(len(self.store.data))},
        )
        self.assertEqual(result.status_code, 201, result.text)
        return ResourceId(result.json()["id"])

    def turn(
        self,
        conversation: ResourceId,
        *,
        key: str = "fixture-turn-key-001",
        text: str = "Fixture question",
    ) -> Any:
        return self.client.post(
            self.url + f"/{conversation}/turns",
            json={"text": text, "output_mode": "text"},
            headers={"Idempotency-Key": key},
        )

    def delivery(self, run_id: ResourceId) -> Any:
        delivery = self.jobs.prepare_dispatch(self.tenant, job_id(self.tenant, KIND, run_id))
        self.assertIsNotNone(delivery)
        return delivery

    def execute(self, run_id: ResourceId) -> None:
        delivery = self.delivery(run_id)
        self.worker.handle(LOCAL_AUTHORIZATION, delivery.model_dump(mode="json"))

    def get_run(self, run: ResourceId) -> Any:
        return self.client.get(f"/v1/tenants/{self.tenant}/runs/{run}")

    def test_accept_replay_completion_and_signed_history_are_separate(self) -> None:
        conversation = self.conversation()
        self.store.retry_once = True
        first = self.turn(conversation)
        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(self.turn(conversation).json(), first.json())
        self.assertEqual(self.turn(conversation, text="different").status_code, 409)
        self.assertEqual(self.turn(conversation, key="fixture-second-key").status_code, 409)
        run = ResourceId(first.json()["run_id"])
        delivery = self.delivery(run)
        self.worker.handle(LOCAL_AUTHORIZATION, delivery.model_dump(mode="json"))
        self.worker.handle(LOCAL_AUTHORIZATION, delivery.model_dump(mode="json"))
        result = self.get_run(run)
        self.assertEqual(result.json()["status"], "completed", result.text)
        self.assertEqual(len(self.model.requests), 1)
        self.assertEqual(self.turn(conversation).json(), first.json())
        messages = self.client.get(self.url + f"/{conversation}/messages")
        self.assertEqual([m["role"] for m in messages.json()["items"]], ["user", "assistant"])
        self.assertNotIn("PRIVATE_SIGNATURE", messages.text + result.text)
        stored = [
            d
            for p, d in self.store.data.items()
            if p.startswith(conversation_path(self.tenant, conversation) + "/messages/")
        ]
        self.assertEqual(len(stored), 2)
        self.assertEqual(
            stored[1]["model_message"]["provider_state"], {"signature": "PRIVATE_SIGNATURE"}
        )
        second = self.turn(conversation, key="fixture-next-turn-key")
        self.execute(ResourceId(second.json()["run_id"]))
        self.assertEqual(
            self.model.requests[1].messages[2].provider_state, {"signature": "PRIVATE_SIGNATURE"}
        )

    def test_seeded_shared_and_private_conversations_can_continue_through_http(self) -> None:
        for visibility, user in (("shared", "demo-a-owner"), ("private", "demo-a-member")):
            self.uid = user
            conversation = ResourceId(
                next(
                    d["id"]
                    for p, d in self.store.data.items()
                    if p.startswith(f"tenants/{self.tenant}/conversations/")
                    and p.count("/") == 3
                    and d["visibility"] == visibility
                )
            )
            page = self.client.get(self.url)
            self.assertEqual(page.status_code, 200)
            self.assertIn(conversation, [item["id"] for item in page.json()["items"]])
            before = self.client.get(self.url + f"/{conversation}/messages")
            self.assertEqual(before.status_code, 200, before.text)
            self.assertEqual(len(before.json()["items"]), 1)
            accepted = self.turn(conversation, key="fixture-seeded-" + visibility)
            self.assertEqual(accepted.status_code, 202, accepted.text)
            self.execute(ResourceId(accepted.json()["run_id"]))
            after = self.client.get(self.url + f"/{conversation}/messages")
            self.assertEqual(len(after.json()["items"]), 3)
            self.assertEqual(
                self.model.requests[-1].messages[1].parts[0].model_dump()["text"],
                before.json()["items"][0]["text"],
            )

    def test_public_contracts_and_safe_validation_errors(self) -> None:
        conversation = self.conversation(private=True)
        accepted = self.turn(conversation)
        self.assertEqual(accepted.status_code, 202)
        spec = yaml.safe_load(Path("specs/openapi.yaml").read_text())
        for schema, response in (
            ("Conversation", self.client.get(self.url + "/" + conversation)),
            ("ConversationPage", self.client.get(self.url)),
            ("RunAccepted", accepted),
            ("Run", self.get_run(ResourceId(accepted.json()["run_id"]))),
            ("MessagePage", self.client.get(self.url + f"/{conversation}/messages")),
        ):
            Draft202012Validator(
                {"$ref": f"#/components/schemas/{schema}", "components": spec["components"]}
            ).validate(response.json())
        for body in (
            {"text": "PRIVATE_BODY", "output_mode": "speech"},
            {"text": "PRIVATE_BODY", "output_mode": "text", "history": ["PRIVATE_BODY"]},
            {"text": "PRIVATE_BODY", "output_mode": "text", "provider_state": "PRIVATE_SIGNATURE"},
            {"text": "x" * 8001, "output_mode": "text"},
        ):
            response = self.client.post(self.url + f"/{conversation}/turns", json=body)
            self.assertEqual(response.status_code, 422)
            self.assertNotIn("PRIVATE_BODY", response.text)

    def test_two_tenants_private_admin_denials_and_revoked_replay(self) -> None:
        conversation = self.conversation(private=True)
        accepted = self.turn(conversation)
        run = ResourceId(accepted.json()["run_id"])
        for uid in ("demo-a-admin", "demo-a-member", "demo-b-owner"):
            self.uid = uid
            self.assertEqual(self.client.get(self.url + "/" + conversation).status_code, 404)
            self.assertEqual(
                self.client.get(self.url + f"/{conversation}/messages").status_code, 404
            )
            self.assertEqual(self.get_run(run).status_code, 404)
            self.assertEqual(self.turn(conversation).status_code, 404)
            page = self.client.get(self.url)
            if uid != "demo-b-owner":
                self.assertNotIn(conversation, [item["id"] for item in page.json()["items"]])
        self.uid = "demo-a-owner"
        self.store.data[f"tenants/{self.tenant}/memberships/{self.uid}"]["status"] = "revoked"
        self.assertEqual(self.get_run(run).status_code, 404)
        self.assertEqual(self.turn(conversation).status_code, 404)
        self.execute(run)
        self.assertEqual(len(self.model.requests), 0)
        saved_job = self.jobs.get(self.tenant, job_id(self.tenant, KIND, run))
        assert saved_job is not None
        self.assertEqual(saved_job.status, "obsolete")

    def test_provider_failure_tool_candidates_and_output_budget(self) -> None:
        for mode in ("failure", "tool", "length", "unsigned"):
            with self.subTest(mode=mode):
                self.model.failure, self.model.tool = mode == "failure", mode == "tool"
                self.model.signed = mode != "unsigned"
                self.model.text = "x" * 8001 if mode == "length" else "Fixture answer"
                conversation = self.conversation()
                accepted = self.turn(conversation, key="fixture-turn-" + mode)
                run = ResourceId(accepted.json()["run_id"])
                self.execute(run)
                result = self.get_run(run).json()
                self.assertEqual(
                    result["status"], "failed" if mode in ("failure", "tool") else "completed"
                )
                messages = self.client.get(self.url + f"/{conversation}/messages").json()["items"]
                self.assertEqual(len(messages), 1 if mode in ("failure", "tool") else 2)
                if mode == "length":
                    self.assertEqual(len(messages[-1]["text"]), 8000)

    def test_saved_agent_source_history_and_cleanup_revocation(self) -> None:
        for mode in ("agent", "settings", "history", "deleted", "cleanup"):
            with self.subTest(mode=mode):
                conversation = self.conversation()
                accepted = self.turn(conversation, key="fixture-check-" + mode)
                run = ResourceId(accepted.json()["run_id"])
                if mode == "agent":
                    self.store.data[f"tenants/{self.tenant}/agents/{self.agent_id}"]["persona"] = (
                        "Changed"
                    )
                elif mode == "settings":
                    self.conversations.update(
                        self.identity(),
                        self.tenant,
                        conversation,
                        ConversationPatch(title="Changed"),
                        "fixture-settings-key",
                        '"1"',
                    )
                elif mode == "history":
                    doc = next(
                        d
                        for p, d in self.store.data.items()
                        if p.startswith(conversation_path(self.tenant, conversation) + "/messages/")
                    )
                    doc["text"] = "tampered"
                elif mode == "deleted":
                    self.store.data[conversation_path(self.tenant, conversation)]["deleted"] = True
                else:
                    self.model.close_hook = lambda: self.store.data[
                        f"tenants/{self.tenant}/memberships/{self.uid}"
                    ].update(status="revoked")
                before = len(self.model.requests)
                self.execute(run)
                self.assertEqual(len(self.model.requests) - before, 1 if mode == "cleanup" else 0)
                messages = [
                    d
                    for p, d in self.store.data.items()
                    if p.startswith(conversation_path(self.tenant, conversation) + "/messages/")
                ]
                self.assertEqual(len(messages), 1)
                if mode not in ("deleted", "cleanup"):
                    self.assertEqual(self.get_run(run).json()["status"], "failed")
                if mode == "cleanup":
                    self.store.data[f"tenants/{self.tenant}/memberships/{self.uid}"]["status"] = (
                        "active"
                    )
                    self.model.close_hook = lambda: None

    def test_checkpoint_replay_unknown_start_and_concurrent_delivery(self) -> None:
        conversation = self.conversation()
        accepted = self.turn(conversation)
        run = ResourceId(accepted.json()["run_id"])
        delivery = self.delivery(run)
        with patch.object(self.jobs, "complete", side_effect=PersistenceUnavailable()):
            with self.assertRaises(PersistenceUnavailable):
                self.worker.handle(LOCAL_AUTHORIZATION, delivery.model_dump(mode="json"))
        self.clock.advance(6)
        delivery = self.delivery(run)
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(
                pool.map(
                    lambda _: self.worker.handle(
                        LOCAL_AUTHORIZATION, delivery.model_dump(mode="json")
                    ),
                    range(2),
                )
            )
        self.assertEqual(len(self.model.requests), 1)
        self.assertEqual(self.get_run(run).json()["status"], "completed")
        conversation = self.conversation()
        accepted = self.turn(conversation, key="fixture-unknown-key")
        run = ResourceId(accepted.json()["run_id"])
        delivery = self.delivery(run)
        lease = self.jobs.acquire(delivery, known_handler=lambda _: True)
        assert lease is not None
        self.jobs.start_effect(lease, "model")
        self.clock.advance(61)
        self.assertIsNone(self.jobs.prepare_dispatch(self.tenant, job_id(self.tenant, KIND, run)))
        self.assertEqual(self.get_run(run).json()["error_code"], "outcome_unknown")
        self.assertEqual(len(self.model.requests), 1)

    def test_conversation_only_session_device_and_phone_output_skip_memory(self) -> None:
        credentials = self.credentials()
        self.store.data[f"tenants/{self.tenant}/devices/{credentials.device_id}"][
            "capabilities"
        ] = ["display"]
        identity = self.repo.authenticate(credentials.access_token)
        session = self.sessions.start(
            self.identity(),
            self.tenant,
            identity.sub,
            PrivateSessionCreate(acknowledge_shared_space=True, allowed_scopes=["conversation"]),
            "fixture-session-key",
            "fixture-request",
        )
        conversation = self.conversation(private=True, display=DisplayPolicy.AUTHENTICATED_SESSION)
        self.app.dependency_overrides[reader_identity] = lambda: identity
        headers = {"X-Private-Session": session.id, "Idempotency-Key": "fixture-device-turn"}
        self.clock.advance(20)
        accepted = self.client.post(
            self.url + f"/{conversation}/turns",
            json={"text": "Device question", "output_mode": "text"},
            headers=headers,
        )
        self.assertEqual(accepted.status_code, 202, accepted.text)
        run_id = ResourceId(accepted.json()["run_id"])
        self.assertEqual(
            self.store.data[f"tenants/{self.tenant}/runs/{run_id}"]["memory_scopes"], []
        )
        saved = copy.deepcopy(
            self.store.data[f"tenants/{self.tenant}/private_sessions/{session.id}"]
        )
        self.clock.advance(10)
        replay = self.client.post(
            self.url + f"/{conversation}/turns",
            json={"text": "Device question", "output_mode": "text"},
            headers=headers,
        )
        self.assertEqual(replay.json(), accepted.json())
        self.assertEqual(
            self.store.data[f"tenants/{self.tenant}/private_sessions/{session.id}"], saved
        )
        with patch("brainoid_worker.run_turn.EmptyMemory.search", new_callable=AsyncMock) as search:
            self.execute(run_id)
            search.assert_not_called()
        self.assertEqual(
            self.client.get(f"/v1/tenants/{self.tenant}/runs/{run_id}", headers=headers).json()[
                "status"
            ],
            "completed",
        )
        self.assertEqual(
            self.store.data[f"tenants/{self.tenant}/private_sessions/{session.id}"], saved
        )
        self.assertEqual(len(self.model.requests), 1)
        self.app.dependency_overrides[reader_identity] = lambda: self.identity()
        phone = self.client.post(
            self.url + f"/{conversation}/turns",
            json={
                "text": "Phone question",
                "output_mode": "text",
                "output_device_id": identity.sub,
            },
            headers={"Idempotency-Key": "fixture-phone-conversation-only"},
        )
        self.assertEqual(phone.status_code, 202, phone.text)
        phone_run = ResourceId(phone.json()["run_id"])
        self.assertEqual(
            self.store.data[f"tenants/{self.tenant}/runs/{phone_run}"]["memory_scopes"], []
        )
        with patch("brainoid_worker.run_turn.EmptyMemory.search", new_callable=AsyncMock) as search:
            self.execute(phone_run)
            search.assert_not_called()
        self.assertEqual(self.get_run(phone_run).json()["status"], "completed")
        self.assertEqual(len(self.model.requests), 2)
        # Explicit memory requests are denied when either the actor or only the
        # destination lacks permission. Empty optional scopes do not fabricate grants.
        for from_phone in (True, False):
            body: dict[str, Any] = {"text": "Explicit memory attempt", "output_mode": "text"}
            request_headers = {"Idempotency-Key": "fixture-explicit-memory-" + str(from_phone)}
            if from_phone:
                self.app.dependency_overrides[reader_identity] = lambda: self.identity()
                body["output_device_id"] = identity.sub
            else:
                self.app.dependency_overrides[reader_identity] = lambda: identity
                request_headers["X-Private-Session"] = session.id
            explicit = self.client.post(
                self.url + f"/{conversation}/turns", json=body, headers=request_headers
            )
            self.assertEqual(explicit.status_code, 202)
            explicit_run = ResourceId(explicit.json()["run_id"])
            self.store.data[f"tenants/{self.tenant}/runs/{explicit_run}"]["memory_scopes"] = [
                {"visibility": "private", "display_policy": "authenticated_session"}
            ]
            with patch(
                "brainoid_worker.run_turn.EmptyMemory.search", new_callable=AsyncMock
            ) as search:
                self.execute(explicit_run)
                search.assert_not_called()
            self.assertEqual(
                self.client.get(
                    f"/v1/tenants/{self.tenant}/runs/{explicit_run}", headers=request_headers
                ).json()["status"],
                "failed",
            )
            self.assertEqual(len(self.model.requests), 2)
        other_owner = next(
            d["id"]
            for p, d in self.store.data.items()
            if p.startswith(f"tenants/{self.tenant}/conversations/")
            and p.count("/") == 3
            and d.get("owner_user_id") == "demo-a-member"
        )
        for url in (
            self.url + f"/{other_owner}/turns",
            f"/v1/tenants/{self.other}/conversations/{conversation}/turns",
        ):
            denied = self.client.post(
                url, json={"text": "Denied", "output_mode": "text"}, headers=headers
            )
            self.assertEqual(denied.status_code, 404)
        queued = self.client.post(
            self.url + f"/{conversation}/turns",
            json={"text": "Queued before deletion", "output_mode": "text"},
            headers={**headers, "Idempotency-Key": "fixture-before-delete"},
        )
        self.assertEqual(queued.status_code, 202)
        self.store.data[conversation_path(self.tenant, conversation)]["deleted"] = True
        with patch("brainoid_worker.run_turn.EmptyMemory.search", new_callable=AsyncMock) as search:
            self.execute(ResourceId(queued.json()["run_id"]))
            search.assert_not_called()
        self.assertEqual(len(self.model.requests), 2)
        self.assertEqual(
            self.client.get(self.url + "/" + conversation, headers=headers).status_code, 404
        )
        self.store.data["device_credential_families/" + identity.family_id]["status"] = "revoked"
        self.assertEqual(
            self.client.get(self.url + "/" + conversation, headers=headers).status_code, 401
        )

    def test_core_authorizer_infrastructure_outage_remains_unavailable(self) -> None:
        conversation = self.conversation()
        accepted = self.turn(conversation)
        run = ResourceId(accepted.json()["run_id"])
        original = self.handler.state.check
        calls = 0

        def fail(tx: Transaction, job: Any) -> Any:
            nonlocal calls
            calls += 1
            if calls > 1:
                raise PolicyUnavailable()
            return original(tx, job)

        with patch.object(self.handler.state, "check", side_effect=fail):
            with self.assertRaises(AuthorizationUnavailable):
                self.execute(run)
        self.assertEqual(len(self.model.requests), 0)
        self.assertEqual(self.get_run(run).json()["error_code"], "outcome_unknown")

    def test_cursor_and_patch_versions_follow_source_and_scope(self) -> None:
        first, second = self.conversation(), self.conversation()
        page = self.client.get(self.url, params={"limit": 1}).json()
        self.assertIsNotNone(page["next_cursor"])
        updated = self.client.patch(
            self.url + "/" + first,
            json={"title": "Changed"},
            headers={"Idempotency-Key": "fixture-update-key", "If-Match": '"1"'},
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.headers["etag"], '"2"')
        self.assertEqual(
            self.client.get(
                self.url, params={"limit": 1, "cursor": page["next_cursor"]}
            ).status_code,
            400,
        )
        self.assertNotEqual(first, second)
        body = {"display_policy": "never"}
        headers = {"Idempotency-Key": "fixture-display-update", "If-Match": '"2"'}
        changed = self.client.patch(self.url + "/" + first, json=body, headers=headers)
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(
            self.client.patch(self.url + "/" + first, json=body, headers=headers).json(),
            changed.json(),
        )

    def test_content_filter_and_empty_completion_do_not_create_final_message(self) -> None:
        for text, reason in (("buffered", "content_filter"), ("", "stop")):
            conversation = self.conversation()
            self.model.text, self.model.finish_reason = text, reason
            self.model.signed = False
            accepted = self.turn(conversation, key="fixture-terminal-" + reason)
            run = ResourceId(accepted.json()["run_id"])
            self.execute(run)
            self.assertEqual(self.get_run(run).json()["status"], "failed")
            self.assertEqual(
                len(self.client.get(self.url + f"/{conversation}/messages").json()["items"]), 1
            )

    def test_expired_lease_during_authorizer_does_not_start_model(self) -> None:
        conversation = self.conversation()
        accepted = self.turn(conversation)
        run = ResourceId(accepted.json()["run_id"])
        original = self.handler.state.check
        calls = 0

        def expire(tx: Transaction, job: Any) -> Any:
            nonlocal calls
            calls += 1
            if calls == 2:
                self.clock.advance(61)
            return original(tx, job)

        with patch.object(self.handler.state, "check", side_effect=expire):
            self.execute(run)
        self.assertEqual(len(self.model.requests), 0)
        self.assertIsNone(self.jobs.prepare_dispatch(self.tenant, job_id(self.tenant, KIND, run)))
        self.assertEqual(self.get_run(run).json()["error_code"], "outcome_unknown")

    def test_blocked_authority_read_has_finite_worker_return_and_no_late_writes(self) -> None:
        conversation = self.conversation()
        accepted = self.turn(conversation)
        run = ResourceId(accepted.json()["run_id"])
        self.handler.budget = RunBudget(timeout_seconds=0.05, max_output_characters=8000)
        release = Event()
        original = self.handler.state.check
        calls = 0

        def block(tx: Transaction, job: Any) -> Any:
            nonlocal calls
            calls += 1
            if calls > 1:
                release.wait(3)
            return original(tx, job)

        # The held read transaction must not hold the fake store's global mutex:
        # production read snapshots do not block the worker's control transaction.
        original_run = self.store.run

        def snapshot(work: Callable[[Transaction], Any]) -> Any:
            if calls >= 1 and not release.is_set():
                from threading import current_thread

                if current_thread().name == "run-authority-reader":
                    tx = StrictTransaction(self.store.data)
                    result = work(tx)
                    self.assertFalse(tx.wrote)
                    return result
            return original_run(work)

        started = time.monotonic()
        try:
            with (
                patch.object(self.handler.state, "check", side_effect=block),
                patch.object(self.store, "run", side_effect=snapshot),
            ):
                with self.assertRaises(PersistenceUnavailable):
                    self.execute(run)
            self.assertLess(time.monotonic() - started, 1.5)
            self.assertEqual(len(self.model.requests), 0)
            before = copy.deepcopy(self.store.data)
        finally:
            release.set()
        time.sleep(0.05)
        self.assertEqual(self.store.data, before)

    def test_authority_bridge_saturation_is_bounded_and_safe_after_loop_close(self) -> None:
        bridge, release = ReadBridge(), Event()

        async def fill() -> None:
            pending = [asyncio.create_task(bridge.read(lambda: release.wait(3))) for _ in range(4)]
            await asyncio.sleep(0.02)
            with self.assertRaises(PersistenceUnavailable):
                await bridge.read(lambda: True)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

        try:
            asyncio.run(fill())
        finally:
            release.set()

    def test_actor_family_revoked_after_acceptance_blocks_worker_and_new_shared_pi_reads(
        self,
    ) -> None:
        credentials = self.credentials()
        self.store.data[f"tenants/{self.tenant}/devices/{credentials.device_id}"][
            "capabilities"
        ] = ["display"]
        identity = self.repo.authenticate(credentials.access_token)
        private = self.conversation(private=True)
        shared = self.conversation()
        hidden_shared = self.conversation(display=DisplayPolicy.NEVER)
        self.app.dependency_overrides[reader_identity] = lambda: identity
        self.assertEqual(self.client.get(self.url + "/" + private).status_code, 404)
        self.assertEqual(self.client.get(self.url + "/" + hidden_shared).status_code, 404)
        accepted = self.turn(shared)
        self.assertEqual(accepted.status_code, 202, accepted.text)
        self.store.data["device_credential_families/" + identity.family_id]["status"] = "revoked"
        self.execute(ResourceId(accepted.json()["run_id"]))
        self.assertEqual(len(self.model.requests), 0)
        self.assertEqual(self.client.get(self.url + "/" + shared).status_code, 401)

    def test_output_destination_revoked_after_core_return_blocks_final_commit(self) -> None:
        credentials = self.credentials()
        device_path = f"tenants/{self.tenant}/devices/{credentials.device_id}"
        self.store.data[device_path]["capabilities"] = ["display"]
        conversation = self.conversation()
        accepted = self.client.post(
            self.url + f"/{conversation}/turns",
            json={
                "text": "Fixture output question",
                "output_mode": "text",
                "output_device_id": credentials.device_id,
            },
            headers={"Idempotency-Key": "fixture-output-turn"},
        )
        self.assertEqual(accepted.status_code, 202, accepted.text)
        run = ResourceId(accepted.json()["run_id"])
        complete = self.jobs.complete

        def revoke(*args: Any, **kwargs: Any) -> bool:
            self.store.data[device_path]["status"] = "revoked"
            return complete(*args, **kwargs)

        with patch.object(self.jobs, "complete", side_effect=revoke):
            self.execute(run)
        self.assertEqual(len(self.model.requests), 1)
        self.assertEqual(self.get_run(run).json()["status"], "failed")
        self.assertEqual(
            len(self.client.get(self.url + f"/{conversation}/messages").json()["items"]), 1
        )
