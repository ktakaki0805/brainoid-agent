"""CAL03 on real Firestore transactions, provider and secret storage are synthetic."""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

from brainoid_agent_core.types import ToolCall
from brainoid_api.app import create_app
from brainoid_api.auth.runtime import authenticated_identity
from brainoid_api.services.calendar_events import CalendarEvents
from brainoid_api.services.calendar_proposals import Proposals
from brainoid_contracts import ResourceId
from brainoid_memory.calendar_proposals import proposal_path
from brainoid_persistence.firestore import FirestoreTransactions
from brainoid_runs.state import KIND
from brainoid_worker.entry import Worker
from brainoid_worker.local import LocalCaller
from brainoid_worker.resume_run import Confirmations
from brainoid_worker.run_turn import RunTurn
from fakes import calendar_proposals
from fakes.calendar_proposal_graph import ProposalGraphScenarios
from fakes.calendar_proposal_http_release import ProposalHTTPReleaseScenarios
from fakes.calendar_proposal_release import ProposalReleaseScenarios
from fakes.calendar_proposal_sources import ProposalSourceScenarios
from fakes.run_confirmation import MultiStageModel
from google.auth.credentials import AnonymousCredentials
from google.cloud.firestore_v1 import Client
from starlette.testclient import TestClient
from test_calendar_proposal_lifecycle import produce
from test_turn_run_integration import TurnRunIntegrationFixture

from scripts.demo_emulator import DATABASE, PROJECT, DemoEmulator


class CalendarProposalIntegrationTests(calendar_proposals.ProposalScenarios):
    def setUp(self) -> None:
        emulator = DemoEmulator(os.environ)
        emulator.reset()
        self.addCleanup(emulator.reset)
        client = Client(project=PROJECT, database=DATABASE, credentials=AnonymousCredentials())  # type: ignore[no-untyped-call]
        self.addCleanup(client.close)
        self.store = FirestoreTransactions(client)
        self.setup_proposals()

    def client(self) -> TestClient:
        app = create_app()
        app.dependency_overrides[authenticated_identity] = lambda: self.identity()
        return TestClient(app)


class CalendarProposalRunIntegrationTests(
    ProposalHTTPReleaseScenarios,
    ProposalReleaseScenarios,
    ProposalGraphScenarios,
    ProposalSourceScenarios,
    TurnRunIntegrationFixture,
):
    graph_generations = 3

    def setUp(self) -> None:
        super().setUp()
        standalone, proposal = produce(
            self.tenants, self.identity(), self.tenant, Path(self.directory) / "calendar-secrets"
        )
        self.proposals = Proposals(
            CalendarEvents(
                standalone.events.authorization, standalone.events.reader, self.sessions
            ),
            self.conversations,
        )
        self.db.document(f"tenants/{self.tenant}/agents/{self.agent}").update(
            {"allowed_tools": ["calendar.propose"]}
        )
        self.arguments = {
            "integration_id": proposal.integration_id,
            "change": proposal.normalized_request.model_dump(mode="json"),
        }
        self.model = MultiStageModel(
            (
                ToolCall(
                    call_key="proposal",
                    name="calendar.propose",
                    arguments_json=json.dumps(self.arguments),
                ),
            )
        )  # type: ignore[assignment]
        self.restart()

    def restart(self) -> None:
        self.handler = RunTurn(
            self.jobs,
            self.model,
            credential_epoch=self.tokens.epoch,
            calendar_reader=self.proposals.events.reader,
        )
        self.worker = Worker(self.jobs, LocalCaller(), {KIND: self.handler})
        self.confirmations = Confirmations(self.jobs, self.handler.state)

    def start_proposal(self) -> tuple[ResourceId, ResourceId]:
        conversation = self.conversation(private=True)
        run = self.accept(conversation).run_id
        self.execute(run)
        result = self.conversations.get_run(self.identity(), self.tenant, run)
        self.assertEqual(result.status, "waiting_confirmation")
        assert result.waiting_proposal_id is not None
        return run, result.waiting_proposal_id

    def test_parallel_reject_restart_and_one_lease_free_resume(self) -> None:
        run, proposal = self.start_proposal()
        self.assertEqual(self.saved(run).status, "waiting")
        self.assertIsNone(self.saved(run).lease_expires_at)
        self.restart()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda _: self.proposals.reject(
                        self.identity(), self.tenant, proposal, "calendar-emulator-parallel-reject"
                    ),
                    range(2),
                )
            )
        self.assertEqual([r.status for r in results], ["declined", "declined"])
        self.assertEqual(self.saved(run).dispatch_generation, 2)
        self.execute(run)
        self.assertEqual(
            self.conversations.get_run(self.identity(), self.tenant, run).status, "completed"
        )
        self.assertEqual(len(self.model.requests), 1)

    def test_early_reject_is_atomic_with_suspension(self) -> None:
        original = Confirmations.suspend

        def early(instance: Any, execution: Any, request: Any, result: Any) -> None:
            self.proposals.reject(
                self.identity(), self.tenant, result.proposal_id, "calendar-emulator-early-reject"
            )
            original(instance, execution, request, result)

        conversation = self.conversation(private=True)
        run = self.accept(conversation).run_id
        with patch.object(Confirmations, "suspend", early):
            self.execute(run)
        self.assertEqual(
            self.conversations.get_run(self.identity(), self.tenant, run).status, "queued"
        )
        self.execute(run)
        self.assertEqual(
            self.conversations.get_run(self.identity(), self.tenant, run).status, "completed"
        )

    def test_exact_expiry_recovery_updates_both_proposals(self) -> None:
        run, proposal = self.start_proposal()
        self.now += timedelta(seconds=300)
        self.confirmations.expire(self.tenant, self.persistence)
        canonical = self.db.document(proposal_path(self.tenant, proposal)).get().to_dict()
        waiting = (
            self.db.document(f"tenants/{self.tenant}/run_proposals/{proposal}").get().to_dict()
        )
        assert canonical is not None and waiting is not None
        self.assertEqual(canonical["status"], "expired")
        self.assertEqual(waiting["status"], "expired")
        self.assertEqual(canonical["expires_at"], waiting["expires_at"])
        self.restart()
        self.execute(run)
        self.assertEqual(
            self.conversations.get_run(self.identity(), self.tenant, run).status, "completed"
        )
