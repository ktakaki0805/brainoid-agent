"""T-001/T-005/T-031 with the official SDK and independent API/worker processes."""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from brainoid_api.common.pagination import Cursors
from brainoid_api.errors import ErrorCode
from brainoid_api.repositories.conversations import Conversations
from brainoid_api.services.private_sessions import PrivateSessionCreate, PrivateSessions
from brainoid_contracts import DisplayPolicy, FirebaseUid, ResourceId, Visibility
from brainoid_jobs import Jobs
from brainoid_jobs.models import Job, job_id
from brainoid_persistence.firestore import FirestoreTransactions, PersistenceUnavailable
from brainoid_runs.models import ConversationCreate, RunAccepted, TurnCreate
from brainoid_runs.state import KIND, conversation_path
from brainoid_worker.entry import Worker
from brainoid_worker.local import LOCAL_AUTHORIZATION, LocalCaller
from brainoid_worker.run_turn import RunTurn
from fakes.turn_model import Model
from google.api_core.exceptions import ServiceUnavailable
from test_device_pairing_integration import DevicePairingIntegrationFixture


class TurnRunIntegrationFixture(DevicePairingIntegrationFixture):
    def setUp(self) -> None:
        super().setUp()
        self.persistence = FirestoreTransactions(self.db)
        self.jobs = Jobs(
            self.persistence, self.ledger, now=lambda: self.now, allow_untracked_bootstrap=True
        )
        self.sessions = PrivateSessions(
            self.store, self.tenants, self.ledger, self.repo, now=lambda: self.now
        )
        self.conversations = Conversations(
            self.store,
            self.tenants,
            Cursors(b"r" * 32, now=lambda: self.now),
            self.jobs,
            credentials=self.repo,
            sessions=self.sessions,
            now=lambda: self.now,
        )
        self.agent = ResourceId(
            next(self.db.collection(f"tenants/{self.tenant}/agents").stream()).id
        )
        self.model = Model()
        self.handler = RunTurn(self.jobs, self.model, credential_epoch=self.tokens.epoch)
        self.worker = Worker(self.jobs, LocalCaller(), {KIND: self.handler})
        self.initial_runs = len(self.persistence.page(f"tenants/{self.tenant}/runs"))

    def conversation(self, *, private: bool = False) -> ResourceId:
        return self.conversations.create(
            self.identity(),
            self.tenant,
            ConversationCreate(
                agent_id=self.agent,
                visibility=Visibility.PRIVATE if private else Visibility.SHARED,
                display_policy=DisplayPolicy.AUTHENTICATED_SESSION
                if private
                else DisplayPolicy.SHARED_DISPLAY,
            ),
            str(uuid4()),
        ).id

    def accept(self, conversation: ResourceId, key: str = "emulator-turn-request") -> RunAccepted:
        return self.conversations.turn(
            self.identity(),
            self.tenant,
            conversation,
            TurnCreate(text="Fixture question", output_mode="text"),
            key,
        )

    def saved(self, run: ResourceId) -> Job:
        job = self.jobs.get(self.tenant, job_id(self.tenant, KIND, run))
        assert job is not None
        return job

    def execute(self, run: ResourceId) -> None:
        delivery = self.jobs.prepare_dispatch(self.tenant, job_id(self.tenant, KIND, run))
        assert delivery is not None
        self.worker.handle(LOCAL_AUTHORIZATION, delivery.model_dump(mode="json"))


class TurnRunIntegrationTests(TurnRunIntegrationFixture):
    def test_competing_turns_and_same_key_recovery(self) -> None:
        conversation = self.conversation()
        results = self.concurrent(
            lambda: self.accept(conversation),
            lambda: self.accept(conversation, "emulator-other-turn-key"),
        )
        self.assertEqual(sum(isinstance(item, RunAccepted) for item in results), 1)
        self.assertIn(ErrorCode.CONFLICT, results)
        accepted = next(item for item in results if isinstance(item, RunAccepted))
        self.execute(accepted.run_id)
        self.assertEqual(
            self.conversations.get_run(self.identity(), self.tenant, accepted.run_id).status,
            "completed",
        )
        self.assertEqual(
            len(self.conversations.messages(self.identity(), self.tenant, conversation).items), 2
        )
        self.assertEqual(len(self.persistence.page(f"tenants/{self.tenant}/jobs")), 1)

    def test_acceptance_commit_response_loss_replays_one_message_run_job(self) -> None:
        conversation = self.conversation()
        commit = self.db._firestore_api.commit

        def lose(*args: Any, **kwargs: Any) -> Any:
            commit(*args, **kwargs)
            raise ServiceUnavailable("synthetic lost response")  # type: ignore[no-untyped-call]

        with patch.object(self.db._firestore_api, "commit", side_effect=lose):
            self.fails(lambda: self.accept(conversation), ErrorCode.TEMPORARILY_UNAVAILABLE)
        accepted = self.accept(conversation)
        self.assertEqual(self.accept(conversation), accepted)
        self.assertEqual(
            len(self.conversations.messages(self.identity(), self.tenant, conversation).items), 1
        )
        self.assertEqual(
            len(self.persistence.page(f"tenants/{self.tenant}/runs")), self.initial_runs + 1
        )
        self.assertEqual(len(self.persistence.page(f"tenants/{self.tenant}/jobs")), 1)
        self.execute(accepted.run_id)
        self.assertEqual(len(self.model.requests), 1)

    def test_completed_checkpoint_survives_worker_restart_and_final_commit_response_loss(
        self,
    ) -> None:
        conversation = self.conversation()
        accepted = self.accept(conversation)
        with patch.object(self.jobs, "complete", side_effect=PersistenceUnavailable()):
            with self.assertRaises(PersistenceUnavailable):
                self.execute(accepted.run_id)
        self.assertEqual(self.saved(accepted.run_id).checkpoints[0].state, "completed")
        self.now += timedelta(seconds=6)
        original_complete = self.jobs.complete

        def lose_after_commit(*args: Any, **kwargs: Any) -> bool:
            original_complete(*args, **kwargs)
            raise PersistenceUnavailable()

        self.handler = RunTurn(self.jobs, self.model, credential_epoch=self.tokens.epoch)
        self.worker = Worker(self.jobs, LocalCaller(), {KIND: self.handler})
        with patch.object(self.jobs, "complete", side_effect=lose_after_commit):
            with self.assertRaises(PersistenceUnavailable):
                self.execute(accepted.run_id)
        self.assertEqual(self.saved(accepted.run_id).status, "succeeded")
        self.assertEqual(
            self.conversations.get_run(self.identity(), self.tenant, accepted.run_id).status,
            "completed",
        )
        self.assertEqual(len(self.model.requests), 1)
        self.assertEqual(
            len(self.conversations.messages(self.identity(), self.tenant, conversation).items), 2
        )

    def test_private_tenant_and_revocation_before_final_commit(self) -> None:
        conversation = self.conversation(private=True)
        accepted = self.accept(conversation)
        for uid in ("demo-a-admin", "demo-b-owner"):
            identity = self.identity().model_copy(update={"user_id": FirebaseUid(uid)})
            self.fails(
                partial(self.conversations.get_run, identity, self.tenant, accepted.run_id),
                ErrorCode.RESOURCE_NOT_FOUND,
            )
            self.fails(
                partial(self.conversations.messages, identity, self.tenant, conversation),
                ErrorCode.RESOURCE_NOT_FOUND,
            )

        def revoke() -> None:
            self.db.document(f"tenants/{self.tenant}/memberships/demo-a-owner").update(
                {"status": "revoked"}
            )

        self.model.close_hook = revoke
        self.execute(accepted.run_id)
        self.assertEqual(self.saved(accepted.run_id).status, "obsolete")
        self.assertEqual(
            len(
                list(
                    self.db.collection(
                        conversation_path(self.tenant, conversation) + "/messages"
                    ).stream()
                )
            ),
            1,
        )

    def test_seeded_shared_and_private_history_can_be_read_and_extended(self) -> None:
        for visibility, uid in (("shared", "demo-a-owner"), ("private", "demo-a-member")):
            identity = self.identity().model_copy(update={"user_id": FirebaseUid(uid)})
            parent = next(
                d
                for _, d in self.persistence.page(f"tenants/{self.tenant}/conversations")
                if d["visibility"] == visibility
            )
            conversation = ResourceId(parent["id"])
            self.assertIn(
                conversation, [c.id for c in self.conversations.list(identity, self.tenant).items]
            )
            before = self.conversations.messages(identity, self.tenant, conversation)
            self.assertEqual(len(before.items), 1)
            accepted = self.conversations.turn(
                identity,
                self.tenant,
                conversation,
                TurnCreate(text="Fixture continuation", output_mode="text"),
                "emulator-seeded-" + visibility,
            )
            self.execute(accepted.run_id)
            self.assertEqual(
                self.conversations.get_run(identity, self.tenant, accepted.run_id).status,
                "completed",
            )
            self.assertEqual(
                len(self.conversations.messages(identity, self.tenant, conversation).items), 3
            )

    def test_conversation_only_session_accepts_device_and_phone_without_memory(self) -> None:
        pairing = self.pairing()
        device = self.confirm(pairing)
        self.db.document(f"tenants/{self.tenant}/devices/{device.id}").update(
            {"capabilities": ["display"]}
        )
        credentials = self.exchange(pairing)
        identity = self.repo.authenticate(credentials.access_token)
        session = self.sessions.start(
            self.identity(),
            self.tenant,
            identity.sub,
            PrivateSessionCreate(acknowledge_shared_space=True, allowed_scopes=["conversation"]),
            "emulator-private-session",
            "fixture",
        )
        conversation = self.conversation(private=True)
        self.now += timedelta(seconds=20)

        def accept() -> RunAccepted:
            return self.conversations.turn(
                identity,
                self.tenant,
                conversation,
                TurnCreate(text="Fixture device question", output_mode="text"),
                "emulator-private-turn",
                session.id,
            )

        accepted = accept()
        self.assertEqual(
            (
                self.db.document(f"tenants/{self.tenant}/runs/{accepted.run_id}").get().to_dict()
                or {}
            )["memory_scopes"],
            [],
        )
        path = f"tenants/{self.tenant}/private_sessions/{session.id}"
        before = self.db.document(path).get().to_dict()
        self.now += timedelta(seconds=10)
        self.assertEqual(accept(), accepted)
        with patch("brainoid_worker.run_turn.BoundMemory.search", new_callable=AsyncMock) as search:
            self.execute(accepted.run_id)
            search.assert_not_called()
        self.assertEqual(self.db.document(path).get().to_dict(), before)
        self.assertEqual(
            self.conversations.get_run(self.identity(), self.tenant, accepted.run_id).status,
            "completed",
        )
        phone = self.conversations.turn(
            self.identity(),
            self.tenant,
            conversation,
            TurnCreate(text="Phone text only", output_mode="text", output_device_id=identity.sub),
            "emulator-phone-conversation-only",
        )
        self.assertEqual(
            (self.db.document(f"tenants/{self.tenant}/runs/{phone.run_id}").get().to_dict() or {})[
                "memory_scopes"
            ],
            [],
        )
        with patch("brainoid_worker.run_turn.BoundMemory.search", new_callable=AsyncMock) as search:
            self.execute(phone.run_id)
            search.assert_not_called()
        self.assertEqual(
            self.conversations.get_run(self.identity(), self.tenant, phone.run_id).status,
            "completed",
        )
        self.assertEqual(len(self.model.requests), 2)
        self.assertEqual(self.db.document(path).get().to_dict(), before)

    def process(self, function: str, result: Path) -> None:
        subprocess.run(
            [
                sys.executable,
                "-B",
                "-c",
                f"from test_turn_run_integration import {function}; {function}()",
                self.directory,
                self.tenant,
                str(result),
            ],
            env={
                **os.environ,
                "PYTHONPATH": os.pathsep.join(
                    (str(Path("tests").resolve()), str(Path("tests/emulator").resolve()))
                ),
            },
            check=True,
            timeout=30,
        )

    def test_api_exits_after_202_and_fresh_worker_process_completes_and_redelivery_matches(
        self,
    ) -> None:
        result = Path(self.directory) / "acceptance.json"
        self.process("accept_process", result)
        accepted = json.loads(result.read_text())
        run = ResourceId(accepted["run_id"])
        self.assertEqual(self.saved(run).status, "pending")
        self.process("worker_process", result)
        self.assertEqual(self.saved(run).status, "succeeded")
        self.assertEqual(
            self.conversations.get_run(self.identity(), self.tenant, run).status, "completed"
        )
        self.process("worker_process", result)
        self.assertEqual(
            len(
                self.conversations.messages(
                    self.identity(), self.tenant, ResourceId(accepted["conversation_id"])
                ).items
            ),
            2,
        )


def process_services() -> tuple[Any, Jobs, Conversations]:
    from brainoid_api.adapters.local_authz_ledger import LocalAuthzObjects
    from brainoid_api.common.firestore import FirestoreTransactions as APIStore
    from brainoid_api.repositories.tenants import Tenants
    from brainoid_policy.control_ledger import ControlLedger
    from google.auth.credentials import AnonymousCredentials
    from google.cloud.firestore_v1 import Client

    client = Client(
        project="demo-brainoid",
        database="(default)",
        credentials=AnonymousCredentials(),  # type: ignore[no-untyped-call]
    )
    ledger = ControlLedger(LocalAuthzObjects(sys.argv[1]))
    cursors = Cursors(b"r" * 32, now=lambda: datetime.now(UTC))
    jobs = Jobs(FirestoreTransactions(client), ledger, allow_untracked_bootstrap=True)
    store = APIStore(client)
    return client, jobs, Conversations(store, Tenants(store, cursors, ledger=ledger), cursors, jobs)


def accept_process() -> None:
    from datetime import UTC, datetime

    from brainoid_api.app import create_app
    from brainoid_api.auth.context import AuthenticatedIdentity
    from brainoid_api.auth.runtime import AuthServices, get_auth_services
    from brainoid_api.routes.agents import reader_identity
    from fastapi.testclient import TestClient

    client, _, repository = process_services()
    try:
        now = datetime.now(UTC)
        identity = AuthenticatedIdentity(
            user_id=FirebaseUid("demo-a-owner"),
            issued_at=now,
            authenticated_at=now,
            expires_at=now + timedelta(hours=1),
        )
        app = create_app()
        app.dependency_overrides[reader_identity] = lambda: identity
        app.dependency_overrides[get_auth_services] = lambda: AuthServices(conversations=repository)
        base = f"/v1/tenants/{sys.argv[2]}/conversations"
        with TestClient(app) as api:
            agent = next(client.collection(f"tenants/{sys.argv[2]}/agents").stream()).id
            conversation = api.post(
                base,
                json={
                    "agent_id": agent,
                    "visibility": "shared",
                    "display_policy": "shared_display",
                },
                headers={"Idempotency-Key": "process-create-conversation"},
            )
            assert conversation.status_code == 201
            accepted = api.post(
                base + "/" + conversation.json()["id"] + "/turns",
                json={"text": "Fixture process question", "output_mode": "text"},
                headers={"Idempotency-Key": "process-create-turn-key"},
            )
            assert accepted.status_code == 202
        Path(sys.argv[3]).write_text(
            json.dumps(
                {"run_id": accepted.json()["run_id"], "conversation_id": conversation.json()["id"]}
            )
        )
    finally:
        client.close()


def worker_process() -> None:
    from brainoid_worker.dispatcher import Dispatcher
    from brainoid_worker.local import LocalQueue

    client, jobs, _ = process_services()
    try:
        model, queue = Model(), LocalQueue()
        worker = Worker(jobs, LocalCaller(), {KIND: RunTurn(jobs, model, credential_epoch=None)})
        Dispatcher(jobs, jobs.store, queue).recover(ResourceId(sys.argv[2]))  # type: ignore[arg-type]
        queue.drain(worker)
        run = ResourceId(json.loads(Path(sys.argv[3]).read_text())["run_id"])
        job = jobs.get(ResourceId(sys.argv[2]), job_id(ResourceId(sys.argv[2]), KIND, run))
        assert job is not None
        worker.handle(
            LOCAL_AUTHORIZATION,
            {
                "tenant_id": sys.argv[2],
                "job_id": job.id,
                "dispatch_generation": job.dispatch_generation,
            },
        )
        assert job.status == "succeeded"
        assert len(model.requests) <= 1
    finally:
        client.close()
