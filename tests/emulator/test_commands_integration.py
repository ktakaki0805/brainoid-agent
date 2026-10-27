"""APP-06 real Firestore cancellation/claim contention and durable default home tool."""

import json
from datetime import timedelta
from functools import partial
from typing import Any
from unittest.mock import patch
from uuid import uuid4

from brainoid_agent_core.types import ToolCall
from brainoid_api.app import create_app
from brainoid_api.auth.runtime import AuthServices, get_auth_services
from brainoid_api.common.pagination import Cursors
from brainoid_api.errors import APIError, ErrorCode
from brainoid_api.repositories.commands import CommandRequests
from brainoid_api.repositories.memories import Memories
from brainoid_api.routes.agents import reader_identity
from brainoid_contracts import ResourceId
from brainoid_contracts.transactions import Transaction
from brainoid_memory.commands import PRE_START, Command, command_path
from brainoid_persistence.firestore import PersistenceUnavailable
from brainoid_policy.scopes import PolicyDenied
from brainoid_runs.models import TurnCreate
from brainoid_worker.entry import Worker
from brainoid_worker.home import HomeRuntime
from brainoid_worker.local import LocalCaller
from brainoid_worker.run_turn import RunTurn
from fakes.home import HomePreambleModel, home_fixture
from google.api_core.exceptions import Aborted, ServiceUnavailable
from starlette.testclient import TestClient
from test_memories_integration import MemoryIntegrationFixture
from test_turn_run_integration import TurnRunIntegrationFixture


class CommandsIntegrationTests(MemoryIntegrationFixture):
    def setUp(self) -> None:
        super().setUp()
        self.command_body, documents = home_fixture(self.tenant, self.identity().user_id, self.now)
        for path, document in documents.items():
            self.db.document(path).set(document)
        self.commands = CommandRequests(self.memories)
        app = create_app()
        app.dependency_overrides[get_auth_services] = lambda: AuthServices(memories=self.memories)
        app.dependency_overrides[reader_identity] = self.identity
        self.client = self.enterContext(TestClient(app))
        self.url = f"/v1/tenants/{self.tenant}/commands"

    def create_command(self, key: str = "emulator-home-create") -> Command:
        return self.commands.create(self.identity(), self.tenant, self.command_body, key)

    def claim_equivalent(self, identity: ResourceId) -> bool:
        """Test-only counterpart to DEV-07: no public start/grant implementation here."""

        def claim(tx: Transaction) -> bool:
            access = self.memories._open(tx, self.identity(), self.tenant, None)
            record = self.commands.service.get(access, identity)
            if (
                record.status not in PRE_START
                or record.execution_id is not None
                or self.now >= record.expires_at
            ):
                return False
            updated = record.model_copy(
                update={
                    "status": "running",
                    "version": record.version + 1,
                    "execution_id": ResourceId(str(uuid4())),
                    "updated_at": self.now,
                }
            )
            access.finish()
            tx.set(command_path(self.tenant, identity), updated.model_dump(mode="json"))
            return True

        return self.store.run(claim)

    def test_http_same_key_concurrent_and_commit_response_loss(self) -> None:
        first, second = self.concurrent(self.create_command, self.create_command)
        self.assertEqual(first, second)
        assert isinstance(first, Command)
        response = self.client.post(
            self.url,
            json=self.command_body.model_dump(mode="json"),
            headers={"Idempotency-Key": "emulator-home-create"},
        )
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["id"], first.id)
        commit = self.db._firestore_api.commit

        def lost(*args: Any, **kwargs: Any) -> Any:
            commit(*args, **kwargs)
            raise ServiceUnavailable("synthetic command commit response loss")  # type: ignore[no-untyped-call]

        with patch.object(self.db._firestore_api, "commit", side_effect=lost):
            self.fails(
                partial(self.create_command, "emulator-home-lost"),
                ErrorCode.TEMPORARILY_UNAVAILABLE,
            )
        recovered = self.create_command("emulator-home-lost")
        self.assertEqual(recovered.status, "queued")
        self.assertEqual(
            len(list(self.db.collection(f"tenants/{self.tenant}/commands").stream())), 2
        )

    def test_t036_cancel_first_claim_first_and_simultaneous_have_one_winner(self) -> None:
        cancelled = self.create_command()
        response = self.client.post(
            self.url + "/" + cancelled.id + ":cancel",
            headers={"Idempotency-Key": "emulator-cancel-first"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(self.claim_equivalent(cancelled.id))
        started = self.create_command("emulator-start-first")
        self.assertTrue(self.claim_equivalent(started.id))
        response = self.client.post(
            self.url + "/" + started.id + ":cancel",
            headers={"Idempotency-Key": "emulator-cancel-late", "If-Match": '"1"'},
        )
        self.assertEqual(response.status_code, 409, response.text)
        for number in range(3):
            record = self.create_command("emulator-home-race-" + str(number))
            cancel, claimed = self.concurrent(
                partial(
                    self.commands.cancel,
                    self.identity(),
                    self.tenant,
                    record.id,
                    "emulator-cancel-race-" + str(number),
                    None,
                ),
                partial(self.claim_equivalent, record.id),
            )
            current = self.commands.get(self.identity(), self.tenant, record.id)
            self.assertEqual(current.version, 2)
            if claimed:
                self.assertEqual(cancel, ErrorCode.CONFLICT)
                self.assertEqual(current.status, "running")
                self.assertIsNotNone(current.execution_id)
            else:
                self.assertIsInstance(cancel, Command)
                self.assertEqual(current.status, "cancelled")
                self.assertIsNone(current.execution_id)

    def test_t018_exact_expiry_and_current_inventory_allowlist_conflict(self) -> None:
        record = self.create_command()
        self.now += timedelta(seconds=30)
        self.assertFalse(self.claim_equivalent(record.id))
        response = self.client.get(self.url + "/" + record.id)
        self.assertEqual((response.json()["status"], response.headers["etag"]), ("expired", '"2"'))
        self.assertEqual(self.create_command().version, 2)
        for match in (None, '"1"', '"2"'):
            response = self.client.post(
                self.url + "/" + record.id + ":cancel",
                headers={
                    "Idempotency-Key": "emulator-expired-" + str(match),
                    **({} if match is None else {"If-Match": match}),
                },
            )
            self.assertEqual(response.status_code, 410, response.text)
        device = self.db.document(f"tenants/{self.tenant}/devices/{self.command_body.device_id}")
        device.update({"connection_generation": 2})
        with self.assertRaises(APIError):
            self.commands.get(self.identity(), self.tenant, record.id)

    def test_concurrent_expiry_read_and_cancel_always_returns_expired(self) -> None:
        for number in range(3):
            record = self.create_command("emulator-expiry-race-" + str(number))
            self.now += timedelta(seconds=30)
            read, cancelled = self.concurrent(
                partial(self.commands.get, self.identity(), self.tenant, record.id),
                partial(
                    self.commands.cancel,
                    self.identity(),
                    self.tenant,
                    record.id,
                    "emulator-expired-cancel-" + str(number),
                    None,
                ),
            )
            self.assertIsInstance(read, Command)
            assert isinstance(read, Command)
            self.assertEqual((read.status, read.version), ("expired", 2))
            self.assertEqual(cancelled, ErrorCode.RESOURCE_EXPIRED)
            self.assertFalse(self.claim_equivalent(record.id))

    def test_device_family_revocation_conflicts_with_owner_command_transition(self) -> None:
        # Synthetic family; real signed bearer and transaction reads/writes.
        family = ResourceId(str(uuid4()))
        family_path = "device_credential_families/" + family
        self.db.document(family_path).set(
            {
                "tenant_id": self.tenant,
                "device_id": self.command_body.device_id,
                "epoch": self.tokens.epoch,
                "authorization_version": 1,
                "connection_generation": 1,
                "status": "active",
                "expires_at": self.now + timedelta(days=30),
            }
        )
        identity = self.repo.authenticate(
            self.tokens.issue(
                tenant_id=self.tenant,
                device_id=self.command_body.device_id,
                family_id=family,
                authorization_version=1,
                connection_generation=1,
            )
        )
        record = self.commands.create(
            identity, self.tenant, self.command_body, "emulator-origin-family"
        )
        self.assertEqual(
            self.commands.get(self.identity(), self.tenant, record.id).status, "queued"
        )
        # Inject one aborted SDK attempt, release its real locks, then revoke.
        # The retry must reread the original family before writing command state.
        commit = self.db._firestore_api.commit
        revoked = False

        def abort_then_revoke(*args: Any, **kwargs: Any) -> Any:
            nonlocal revoked
            request = kwargs.get("request", {})
            if not revoked and request.get("transaction"):
                revoked = True
                self.db._firestore_api.rollback(
                    request={
                        "database": self.db._database_string,
                        "transaction": request["transaction"],
                    }
                )
                self.db.document(family_path).update({"status": "revoked"})
                raise Aborted("synthetic family revocation retry boundary")  # type: ignore[no-untyped-call]
            return commit(*args, **kwargs)

        with patch.object(self.db._firestore_api, "commit", side_effect=abort_then_revoke):
            with self.assertRaises(PolicyDenied):
                self.claim_equivalent(record.id)
        self.assertTrue(revoked)
        stored = self.db.document(command_path(self.tenant, record.id)).get().to_dict()
        assert stored is not None
        self.assertEqual(stored["status"], "queued")
        self.assertIsNone(stored.get("execution_id"))
        self.fails(
            partial(self.commands.get, self.identity(), self.tenant, record.id),
            ErrorCode.RESOURCE_NOT_FOUND,
        )


class CommandsDefaultToolIntegrationTests(TurnRunIntegrationFixture):
    def test_no_home_admission_read_and_completed_checkpoint_restarts_keep_fixed_result(
        self,
    ) -> None:
        body, documents = home_fixture(self.tenant, self.identity().user_id, self.now)
        for path, document in documents.items():
            self.db.document(path).set(document)
        self.db.document(f"tenants/{self.tenant}/agents/{self.agent}").update(
            {"allowed_tools": ["notes.list", "home.set_state"]}
        )
        model = HomePreambleModel(
            (ToolCall(call_key="read", name="notes.list", arguments_json="{}"),)
        )
        model.final_text = "照明をつけました。家電の操作は成功しました。"
        self.handler = RunTurn(self.jobs, model, credential_epoch=self.tokens.epoch)
        self.worker = Worker(self.jobs, LocalCaller(), {"run_turn": self.handler})
        conversation = self.conversation()
        body_turn = TurnCreate(
            text="Set home state "
            + json.dumps(body.model_dump(mode="json"), ensure_ascii=False, sort_keys=True),
            output_mode="text",
        )
        accepted = self.conversations.turn(
            self.identity(), self.tenant, conversation, body_turn, "emulator-no-home-admission"
        )
        run = accepted.run_id
        generate = self.handler._generate

        def stop_after_read(*args: Any, **kwargs: Any) -> Any:
            if args[3].get("model_step") == 1:
                raise PersistenceUnavailable()
            return generate(*args, **kwargs)

        with patch.object(self.handler, "_generate", side_effect=stop_after_read):
            with self.assertRaises(PersistenceUnavailable):
                self.execute(run)
        self.now += timedelta(seconds=6)
        with patch.object(self.jobs, "complete", side_effect=PersistenceUnavailable):
            with self.assertRaises(PersistenceUnavailable):
                self.execute(run)
        self.assertEqual(len(model.requests), 2)
        pending = self.db.document(f"tenants/{self.tenant}/runs/{run}").get().to_dict()
        assert pending is not None
        self.assertEqual(pending["model_step"], 1)
        self.assertNotIn("home_command", pending)
        for doc in self.db.collection(f"tenants/{self.tenant}/runs/{run}/events").stream():
            event = doc.to_dict()
            assert event is not None
            self.assertNotEqual(event["type"], "text.delta")
        self.now += timedelta(seconds=6)
        self.execute(run)
        self.assertEqual(len(model.requests), 2)
        self.assertEqual(
            self.conversations.turn(
                self.identity(), self.tenant, conversation, body_turn, "emulator-no-home-admission"
            ),
            accepted,
        )
        saved = self.db.document(f"tenants/{self.tenant}/runs/{run}").get().to_dict()
        assert saved is not None
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(saved["continuation"], [])
        final = (
            self.db.document(
                f"tenants/{self.tenant}/conversations/{conversation}/messages/{saved['final_message_id']}"
            )
            .get()
            .to_dict()
        )
        assert final is not None
        self.assertEqual(final["text"], HomeRuntime.NOT_ACCEPTED)
        self.assertIsNone(final["model_messages"])
        self.assertIsNone(final["model_message"])
        events = [
            doc.to_dict()
            for doc in self.db.collection(f"tenants/{self.tenant}/runs/{run}/events").stream()
        ]
        self.assertNotIn("UNOBSERVED_PHYSICAL_SUCCESS", json.dumps(events, default=str))
        self.assertNotIn("照明をつけました", json.dumps(events, ensure_ascii=False, default=str))
        self.assertFalse(list(self.db.collection(f"tenants/{self.tenant}/commands").stream()))

    def test_actual_default_tool_run_commits_acceptance_once_and_never_calls_model_for_success(
        self,
    ) -> None:
        body, documents = home_fixture(self.tenant, self.identity().user_id, self.now)
        for path, document in documents.items():
            self.db.document(path).set(document)
        memories = Memories(
            self.store,
            self.tenants,
            Cursors(b"h" * 32, now=lambda: self.now),
            ledger=self.ledger,
            now=lambda: self.now,
            allow_untracked_bootstrap=True,
        )
        self.db.document(f"tenants/{self.tenant}/agents/{self.agent}").update(
            {"allowed_tools": ["notes.list", "home.set_state"]}
        )
        model = HomePreambleModel(
            (
                ToolCall(call_key="read", name="notes.list", arguments_json="{}"),
                ToolCall(
                    call_key="home", name="home.set_state", arguments_json=body.model_dump_json()
                ),
            )
        )
        self.handler = RunTurn(self.jobs, model, credential_epoch=self.tokens.epoch)
        self.worker = Worker(self.jobs, LocalCaller(), {"run_turn": self.handler})
        conversation = self.conversation()
        run = self.conversations.turn(
            self.identity(),
            self.tenant,
            conversation,
            TurnCreate(
                text="Set home state "
                + json.dumps(body.model_dump(mode="json"), ensure_ascii=False, sort_keys=True),
                output_mode="text",
            ),
            "emulator-home-turn",
        ).run_id
        self.execute(run)
        self.assertEqual(
            self.conversations.get_run(self.identity(), self.tenant, run).status, "completed"
        )
        self.assertEqual(len(model.requests), 2)
        saved = self.db.document(f"tenants/{self.tenant}/runs/{run}").get().to_dict()
        assert saved is not None
        command = CommandRequests(memories).get(
            self.identity(), self.tenant, ResourceId(saved["home_command"]["effect_id"])
        )
        self.assertEqual(command.status, "queued")
        final = (
            self.db.document(
                f"tenants/{self.tenant}/conversations/{conversation}/messages/{saved['final_message_id']}"
            )
            .get()
            .to_dict()
        )
        assert final is not None
        self.assertEqual(final["text"], HomeRuntime.UPDATE_ACK)

        events = [
            doc.to_dict()
            for doc in self.db.collection(f"tenants/{self.tenant}/runs/{run}/events").stream()
        ]
        self.assertNotIn("UNOBSERVED_PHYSICAL_SUCCESS", json.dumps(events, default=str))
