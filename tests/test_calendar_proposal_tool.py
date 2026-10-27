"""CAL03 default tool, lease-free checkpoints and races through real producers."""

import json
import tempfile
from pathlib import Path
from typing import cast
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from brainoid_agent_core.types import ToolCall
from brainoid_api.auth.runtime import AuthServices, get_auth_services
from brainoid_api.routes.agents import reader_identity
from brainoid_api.services.calendar_events import CalendarEvents
from brainoid_api.services.calendar_proposals import Proposals
from brainoid_api.services.private_sessions import PrivateSessionCreate
from brainoid_contracts import DisplayPolicy, ProviderId, ResourceId
from brainoid_contracts.errors import APIError
from brainoid_jobs.models import job_id
from brainoid_memory.calendar_oauth import WRITE, OAuthStart
from brainoid_memory.calendar_proposals import CHANGE, proposal_path
from brainoid_memory.calendar_reader import CalendarReader, CalendarSelection
from brainoid_policy.models import UserSubject
from brainoid_policy.scopes import PolicyDenied
from brainoid_runs.state import KIND
from brainoid_worker.entry import Worker
from brainoid_worker.local import LocalCaller
from brainoid_worker.resume_run import Confirmations
from brainoid_worker.run_turn import RunTurn
from fakes.calendar_events import EventsProvider
from fakes.calendar_proposal_graph import ProposalGraphScenarios
from fakes.calendar_proposal_http_release import ProposalHTTPReleaseScenarios
from fakes.calendar_proposal_release import ProposalReleaseScenarios
from fakes.calendar_proposal_sources import ProposalSourceScenarios
from fakes.calendar_scenarios import OAuthTransport
from fakes.run_confirmation import MultiStageModel
from test_calendar_lifecycle import calendar
from test_jobs import JobStore
from test_turn_run import TurnRunFixture


class CalendarProposalToolTests(
    ProposalHTTPReleaseScenarios,
    ProposalReleaseScenarios,
    ProposalGraphScenarios,
    ProposalSourceScenarios,
    TurnRunFixture,
):
    def setUp(self) -> None:
        super().setUp()
        self.authorization = calendar(
            self.tenants, Path(self.enterContext(tempfile.TemporaryDirectory()))
        )
        transport = OAuthTransport()
        transport.scopes = WRITE
        self.authorization.provider.post = transport.post  # type: ignore[attr-defined]
        started = self.authorization.start(
            self.identity(),
            self.tenant,
            OAuthStart(scope_profile="read_write", return_target="mobile"),
            "calendar-proposal-tool-oauth",
        )
        state = parse_qs(urlsplit(started.authorization_url).query)["state"][0]
        self.authorization.callback(state, "synthetic", None)
        self.integration = self.authorization.integrations(self.identity(), self.tenant)[0].id
        self.provider = EventsProvider()
        self.reader = CalendarReader(
            self.store,
            self.provider,
            self.authorization.secrets,
            self.tenants._cursors,
            now=self.clock.now,
        )
        self.events = CalendarEvents(self.authorization, self.reader, self.sessions)
        self.events.select(
            self.identity(),
            self.tenant,
            self.integration,
            CalendarSelection(allowed_calendar_ids=[ProviderId("private-calendar")]),
            "calendar-proposal-tool-select",
            '"1"',
        )
        self.proposals = Proposals(self.events, self.conversations)
        self.body = {
            "action": "create",
            "calendar_id": "private-calendar",
            "summary": "SYNTHETIC_PRIVATE_PROPOSAL",
            "start": {"date": "2026-03-08"},
            "end": {"date": "2026-03-09"},
        }
        self.arguments = {"integration_id": self.integration, "change": self.body}
        self.configure()
        self.app.dependency_overrides[get_auth_services] = lambda: AuthServices(
            tenants=self.tenants,
            devices=self.repo,
            private_sessions=self.sessions,
            conversations=self.conversations,
            calendar=self.authorization,
            calendar_events=self.events,
        )

    def configure(self) -> None:
        self.store.data[f"tenants/{self.tenant}/agents/{self.agent_id}"]["allowed_tools"] = [
            "calendar.propose"
        ]
        self.model = MultiStageModel(
            (
                ToolCall(
                    call_key="propose",
                    name="calendar.propose",
                    arguments_json=json.dumps(self.arguments),
                ),
            )
        )  # type: ignore[assignment]
        self.restart()

    def restart(self) -> None:
        self.handler = RunTurn(
            self.jobs, self.model, credential_epoch=self.tokens.epoch, calendar_reader=self.reader
        )
        self.worker = Worker(self.jobs, LocalCaller(), {KIND: self.handler})
        self.confirmations = Confirmations(self.jobs, self.handler.state)

    def start_proposal(self, *, private: bool = True) -> ResourceId:
        conversation = self.conversation(private=private)
        run = ResourceId(self.turn(conversation, text="予定の作成案を準備して").json()["run_id"])
        self.execute(run)
        return run

    def proposal(self, run: ResourceId) -> ResourceId:
        result = self.get_run(run)
        self.assertEqual(result.json()["status"], "waiting_confirmation", result.text)
        return ResourceId(result.json()["waiting_proposal_id"])

    def test_default_tool_reject_restart_and_single_resume(self) -> None:
        run = self.start_proposal()
        proposal = self.proposal(run)
        record = self.store.data[proposal_path(self.tenant, proposal)]
        waiting = self.store.data[f"tenants/{self.tenant}/run_proposals/{proposal}"]
        self.assertEqual(waiting["expires_at"], record["expires_at"])
        self.assertEqual(waiting["resume_mode"], "result")
        self.assertEqual(waiting["checkpoint_id"], record["checkpoint_id"])
        confirmations = [
            d
            for p, d in self.store.data.items()
            if p.startswith(f"tenants/{self.tenant}/runs/{run}/events/")
            and d["type"] == "confirmation.required"
        ]
        self.assertEqual(len(confirmations), 1)
        self.assertEqual(
            confirmations[0]["payload"],
            {
                "proposal_id": proposal,
                "kind": "calendar_write",
                "expires_at": record["expires_at"].replace("+00:00", "Z"),
            },
        )
        job = self.jobs.get(self.tenant, job_id(self.tenant, KIND, run))
        assert job is not None
        self.assertEqual(job.status, "waiting")
        self.assertIsNone(job.lease_expires_at)
        self.restart()
        for key in ("calendar-tool-reject", "calendar-tool-reject", "calendar-tool-reject-again"):
            self.assertEqual(
                self.proposals.reject(self.identity(), self.tenant, proposal, key).status,
                "declined",
            )
        before = len(self.model.requests)
        self.execute(run)
        self.assertEqual(self.get_run(run).json()["status"], "completed")
        self.assertEqual(len(self.model.requests), before)
        with self.assertRaises(PolicyDenied):
            self.confirmations.decide(
                self.tenant,
                run,
                proposal,
                "confirmed",
                subject=UserSubject(user_id=self.identity().user_id),
            )
        self.assertEqual(len([p for p in self.store.data if "/calendar_proposals/" in p]), 1)

    def test_early_reject_before_suspend_is_joined_atomically(self) -> None:
        suspend = Confirmations.suspend

        def early(instance, execution, request, result):  # type: ignore[no-untyped-def]
            self.proposals.reject(
                self.identity(), self.tenant, result.proposal_id, "calendar-early-reject"
            )
            suspend(instance, execution, request, result)

        with patch.object(Confirmations, "suspend", early):
            run = self.start_proposal()
        self.assertEqual(self.get_run(run).json()["status"], "queued")
        self.execute(run)
        self.assertEqual(self.get_run(run).json()["status"], "completed")
        records = [d for p, d in self.store.data.items() if "/calendar_proposals/" in p]
        self.assertEqual([r["status"] for r in records], ["declined"])

    def test_deadline_expiry_and_cancel_never_reactivate(self) -> None:
        run = self.start_proposal()
        proposal = self.proposal(run)
        self.clock.advance(300)
        self.assertEqual(
            self.proposals.get(self.identity(), self.tenant, proposal).status, "expired"
        )
        self.execute(run)
        self.assertEqual(self.get_run(run).json()["status"], "completed")
        run2 = self.start_proposal()
        proposal2 = self.proposal(run2)
        response = self.client.post(
            f"/v1/tenants/{self.tenant}/runs/{run2}:cancel",
            headers={"Idempotency-Key": "calendar-proposal-cancel"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            self.proposals.get(self.identity(), self.tenant, proposal2).status, "cancelled"
        )
        with self.assertRaises(APIError):
            self.proposals.reject(
                self.identity(), self.tenant, proposal2, "calendar-reject-cancelled"
            )
        self.assertEqual(self.get_run(run2).json()["status"], "cancelled")

    def test_shared_and_model_authority_injection_denied(self) -> None:
        run = self.start_proposal(private=False)
        self.assertNotEqual(self.get_run(run).json()["status"], "waiting_confirmation")
        self.assertFalse(any("/calendar_proposals/" in p for p in self.store.data))
        self.arguments["run_id"] = str(run)
        self.configure()
        run = self.start_proposal()
        self.assertNotEqual(self.get_run(run).json()["status"], "waiting_confirmation")

    def test_pi_propose_without_read_and_original_family_revocation(self) -> None:
        credentials = self.credentials()
        device = self.store.data[f"tenants/{self.tenant}/devices/{credentials.device_id}"]
        device["capabilities"] = ["display"]
        device["allowed_scopes"] = ["conversation", "calendar.propose"]
        identity = self.repo.authenticate(credentials.access_token)
        session = self.sessions.start(
            self.identity(),
            self.tenant,
            identity.sub,
            PrivateSessionCreate(
                acknowledge_shared_space=True, allowed_scopes=["conversation", "calendar.propose"]
            ),
            "calendar-propose-only-session",
            "calendar-request",
        )
        standalone = self.proposals.create(
            identity,
            self.tenant,
            self.integration,
            CHANGE.validate_python(self.body),
            "calendar-pi-standalone",
            session.id,
        )
        self.assertEqual(standalone.status, "pending")
        conversation = self.conversation(private=True, display=DisplayPolicy.AUTHENTICATED_SESSION)
        self.app.dependency_overrides[reader_identity] = lambda: identity
        accepted = self.client.post(
            f"{self.url}/{conversation}/turns",
            json={"text": "作成案を準備して", "output_mode": "text"},
            headers={
                "X-Private-Session": session.id,
                "Idempotency-Key": "calendar-pi-proposal-turn",
            },
        )
        self.assertEqual(accepted.status_code, 202, accepted.text)
        run = ResourceId(accepted.json()["run_id"])
        self.execute(run)
        record = self.store.data[f"tenants/{self.tenant}/runs/{run}"]
        self.assertEqual(record["status"], "waiting_confirmation")
        self.store.data["device_credential_families/" + identity.family_id]["status"] = "revoked"
        self.app.dependency_overrides[reader_identity] = lambda: self.identity()
        self.assertNotEqual(self.get_run(run).status_code, 200)
        self.assertNotEqual(
            self.client.get(f"/v1/tenants/{self.tenant}/runs/{run}/events").status_code, 200
        )
        for proposal in (standalone.proposal_id, ResourceId(record["waiting_proposal_id"])):
            with self.assertRaises(APIError):
                self.proposals.get(self.identity(), self.tenant, proposal)

    def test_expiry_recovery_and_lost_reject_commit_converge(self) -> None:
        from typing import Any

        from brainoid_persistence.firestore import PersistenceUnavailable

        run = self.start_proposal()
        proposal = self.proposal(run)
        original = self.store.run
        lost = False

        def transaction(work: Any) -> Any:
            nonlocal lost
            result = original(work)
            if (
                self.store.data[proposal_path(self.tenant, proposal)]["status"] == "declined"
                and not lost
            ):
                lost = True
                raise PersistenceUnavailable()
            return result

        with patch.object(self.store, "run", side_effect=transaction):
            with self.assertRaises(PersistenceUnavailable):
                self.proposals.reject(
                    self.identity(), self.tenant, proposal, "calendar-loss-reject"
                )
        self.assertEqual(
            self.proposals.reject(
                self.identity(), self.tenant, proposal, "calendar-loss-reject"
            ).status,
            "declined",
        )
        self.execute(run)
        self.assertEqual(self.get_run(run).json()["status"], "completed")
        run = self.start_proposal()
        proposal = self.proposal(run)
        self.clock.advance(300)
        self.confirmations.expire(self.tenant, cast(JobStore, self.store))
        self.assertEqual(self.store.data[proposal_path(self.tenant, proposal)]["status"], "expired")
        self.execute(run)
        self.assertEqual(self.get_run(run).json()["status"], "completed")

    def test_delay_before_suspend_does_not_extend_deadline(self) -> None:
        suspend = Confirmations.suspend

        def delayed(instance, execution, request, result):  # type: ignore[no-untyped-def]
            self.clock.advance(300)
            suspend(instance, execution, request, result)

        with patch.object(Confirmations, "suspend", delayed):
            run = self.start_proposal()
        self.restart()
        self.execute(run)
        self.assertEqual(self.get_run(run).json()["status"], "queued")
        self.execute(run)
        self.assertEqual(self.get_run(run).json()["status"], "completed")
        records = [d for p, d in self.store.data.items() if "/calendar_proposals/" in p]
        self.assertEqual([r["status"] for r in records], ["expired"])

    def test_cancel_after_canonical_creation_before_suspend(self) -> None:
        suspend = Confirmations.suspend
        seen: list[ResourceId] = []

        def cancelled(instance, execution, request, result):  # type: ignore[no-untyped-def]
            run = execution.check().operation_id
            seen.append(result.proposal_id)
            response = self.client.post(
                f"/v1/tenants/{self.tenant}/runs/{run}:cancel",
                headers={"Idempotency-Key": "calendar-early-cancel"},
            )
            self.assertEqual(response.status_code, 200, response.text)
            suspend(instance, execution, request, result)

        with patch.object(Confirmations, "suspend", cancelled):
            run = self.start_proposal()
        self.assertEqual(self.get_run(run).json()["status"], "cancelled")
        self.assertEqual(len(seen), 1)
        self.assertEqual(
            self.proposals.get(self.identity(), self.tenant, seen[0]).status, "cancelled"
        )
        self.assertNotIn(f"tenants/{self.tenant}/run_proposals/{seen[0]}", self.store.data)
        self.clock.advance(300)
        self.proposals.service.expire_unsuspended(self.tenant, cast(JobStore, self.store))
        self.assertEqual(
            self.proposals.get(self.identity(), self.tenant, seen[0]).status, "cancelled"
        )
        with self.assertRaises(APIError):
            self.proposals.reject(
                self.identity(), self.tenant, seen[0], "calendar-early-cancel-reject"
            )
