"""T-007/T-037: process restart, transaction races and revoked resume on Firestore."""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from brainoid_agent_core.ports import CallControl
from brainoid_agent_core.types import ToolResult
from brainoid_api.errors import APIError
from brainoid_api.routes.run_events import next_batch
from brainoid_contracts import FirebaseUid, ResourceId, normalize_utc
from brainoid_jobs import Jobs
from brainoid_jobs.models import job_id
from brainoid_policy.models import UserSubject
from brainoid_policy.scopes import PolicyDenied
from brainoid_runs.models import TurnCreate
from brainoid_runs.state import KIND
from brainoid_tools import HandlerResult, Invocation
from brainoid_worker.entry import Worker
from brainoid_worker.local import LOCAL_AUTHORIZATION, LocalCaller
from brainoid_worker.resume_run import Confirmations, ExternalClaim
from brainoid_worker.run_turn import RunTurn
from fakes.run_confirmation import (
    ArgumentsForTest,
    ConfirmationAuthority,
    ConfirmationEffect,
    ConfirmationModel,
    EchoResultModel,
    ExternalBusinessQueue,
    MultiStageModel,
    ProposalCreatingEffect,
)
from test_turn_run_integration import TurnRunIntegrationFixture, process_services


class DurableEffect(ConfirmationEffect):
    def __init__(self, jobs: Jobs) -> None:
        super().__init__()
        self.jobs = jobs

    async def execute(
        self, invocation: Invocation[ArgumentsForTest], *, control: CallControl
    ) -> HandlerResult:
        def write(tx: Any) -> None:
            path = f"tenants/{invocation.context.tenant_id}/fixture_confirmation_effects/{invocation.effect_id}"
            saved = tx.get(path)
            tx.set(path, {"calls": 1 if saved is None else saved["calls"] + 1})

        self.jobs.store.run(write)
        return await super().execute(invocation, control=control)


class CheckpointIntegrationTests(TurnRunIntegrationFixture):
    def setup_run(self, *, private: bool = False) -> tuple[ResourceId, ResourceId]:
        self.db.document(f"tenants/{self.tenant}/agents/{self.agent}").update(
            {"allowed_tools": ["notes.delete"]}
        )
        conversation = self.conversation(private=private)
        run = self.accept(conversation, str(uuid4())).run_id
        self.configure(run)
        return conversation, run

    def configure(self, run: ResourceId) -> None:
        note = next(
            d
            for _, d in self.persistence.page(f"tenants/{self.tenant}/notes")
            if d["visibility"] == "shared"
        )
        self.model = ConfirmationModel(ResourceId(note["id"]))  # type: ignore[assignment]
        self.effect = DurableEffect(self.jobs)
        self.handler = RunTurn(
            self.jobs,
            self.model,
            credential_epoch=self.tokens.epoch,
            tool_registry=self.effect.registry,
            tool_authorizer=ConfirmationAuthority(self.jobs, run),
        )
        self.worker = Worker(self.jobs, LocalCaller(), {KIND: self.handler})
        self.confirmations = Confirmations(
            self.jobs,
            self.handler.state,
            registry=self.effect.registry,
            result_authorizer=cast(ConfirmationAuthority, self.handler.tool_authorizer),
        )

    def proposal(self, run: ResourceId) -> ResourceId:
        result = self.conversations.get_run(self.identity(), self.tenant, run)
        assert result.waiting_proposal_id is not None
        return result.waiting_proposal_id

    def decision(self, run: ResourceId, proposal: ResourceId) -> str:
        return self.confirmations.decide(
            self.tenant,
            run,
            proposal,
            "confirmed",
            subject=UserSubject(user_id=self.identity().user_id),
        )

    def start_external(self, run: ResourceId, proposal: ResourceId) -> ExternalClaim:
        return self.confirmations.start_external(
            self.tenant,
            run,
            proposal,
            subject=UserSubject(user_id=self.identity().user_id),
            prepare=ExternalBusinessQueue(
                self.effect.registry, cast(ConfirmationAuthority, self.handler.tool_authorizer)
            ).prepare,
        )

    def checkpoint_process(
        self, run: ResourceId, *, proposal_handler: bool = False
    ) -> dict[str, Any]:
        result = Path(self.directory) / "checkpoint-process.json"
        subprocess.run(
            [
                sys.executable,
                "-B",
                "-c",
                "from test_run_checkpoint_integration import checkpoint_worker_process; checkpoint_worker_process()",
                self.directory,
                self.tenant,
                str(run),
                str(result),
                self.now.isoformat(),
                "proposal" if proposal_handler else "execute",
            ],
            env={
                **os.environ,
                "PYTHONPATH": os.pathsep.join(
                    (str(Path("tests").resolve()), str(Path("tests/emulator").resolve()))
                ),
            },
            check=True,
            timeout=40,
        )
        return json.loads(result.read_text())  # type: ignore[no-any-return]

    def test_waiting_worker_exits_then_new_process_resumes_once(self) -> None:
        _, run = self.setup_run()
        first = self.checkpoint_process(run)
        self.assertEqual(first, {"status": "waiting", "model_calls": 1, "tool_calls": 0})
        job = self.saved(run)
        self.assertIsNone(job.lease_expires_at)
        self.assertIsNone(self.jobs.prepare_dispatch(self.tenant, job.id))
        proposal = self.proposal(run)
        self.assertEqual(self.decision(run, proposal), "confirmed")
        self.assertEqual(self.decision(run, proposal), "confirmed")
        second = self.checkpoint_process(run)
        self.assertEqual(second, {"status": "succeeded", "model_calls": 1, "tool_calls": 1})
        third = self.checkpoint_process(run)
        self.assertEqual(third, {"status": "succeeded", "model_calls": 0, "tool_calls": 0})
        effects = self.persistence.page(f"tenants/{self.tenant}/fixture_confirmation_effects")
        self.assertEqual(len(effects), 1)
        self.assertEqual(effects[0][1]["calls"], 1)

    def test_confirm_cancel_race_and_duplicate_delivery_do_not_start_effect(self) -> None:
        _, run = self.setup_run()
        self.execute(run)
        proposal = self.proposal(run)

        def confirm() -> str:
            try:
                return self.decision(run, proposal)
            except PolicyDenied:
                return "denied"

        self.concurrent(
            confirm,
            lambda: self.conversations.cancel_run(
                self.identity(), self.tenant, run, "emulator-confirm-cancel-key"
            ),
        )
        self.assertEqual(
            self.conversations.get_run(self.identity(), self.tenant, run).status, "cancelled"
        )
        self.assertIsNone(self.jobs.prepare_dispatch(self.tenant, job_id(self.tenant, KIND, run)))
        self.assertEqual(
            self.persistence.page(f"tenants/{self.tenant}/fixture_confirmation_effects"), []
        )

    def test_confirmed_resume_rechecks_membership_and_expiry_is_recovered(self) -> None:
        _, run = self.setup_run()
        self.execute(run)
        self.decision(run, self.proposal(run))
        self.db.document(f"tenants/{self.tenant}/memberships/{self.identity().user_id}").update(
            {"status": "revoked"}
        )
        delivery = self.jobs.prepare_dispatch(self.tenant, job_id(self.tenant, KIND, run))
        assert delivery is not None
        self.worker.handle(LOCAL_AUTHORIZATION, delivery.model_dump(mode="json"))
        self.assertEqual(self.saved(run).status, "obsolete")
        self.assertEqual(
            self.persistence.page(f"tenants/{self.tenant}/fixture_confirmation_effects"), []
        )

    def test_handler_created_proposal_resumes_with_result_without_recreation(self) -> None:
        _, run = self.setup_run()
        authority = ConfirmationAuthority(self.jobs, run)
        authority.waiting = False
        effect = ProposalCreatingEffect(self.jobs)

        def worker() -> Worker:
            return Worker(
                self.jobs,
                LocalCaller(),
                {
                    KIND: RunTurn(
                        self.jobs,
                        self.model,
                        credential_epoch=self.tokens.epoch,
                        tool_registry=effect.registry,
                        tool_authorizer=authority,
                    )
                },
            )

        self.worker = worker()
        self.confirmations.result_authorizer = authority
        self.execute(run)
        proposal = self.proposal(run)
        self.assertEqual(len(effect.calls), 1)
        with self.assertRaises(PolicyDenied):
            self.decision(run, proposal)
        saved = self.persistence.page(f"tenants/{self.tenant}/tool_executions")[0][1]
        result = ToolResult(
            tool_call_id=saved["tool_call_id"],
            operation_id=saved["effect_id"],
            status="succeeded",
            result={"business_operation": "complete"},
        )
        claim = self.start_external(run, proposal)
        for _ in range(2):
            self.confirmations.receive_external(claim, result)
        self.worker = worker()
        self.execute(run)
        self.assertEqual(
            self.conversations.get_run(self.identity(), self.tenant, run).status, "completed"
        )
        self.assertEqual(len(effect.calls), 1)
        business = self.persistence.page(f"tenants/{self.tenant}/fixture_business_proposals")
        self.assertEqual(len(business), 1)
        self.assertEqual(business[0][1]["created_count"], 1)

    def test_expiry_resume_and_duplicate_decision_are_atomic(self) -> None:
        _, run = self.setup_run()
        self.execute(run)
        proposal = self.proposal(run)
        self.now += timedelta(seconds=301)
        self.confirmations.expire(self.tenant, self.persistence)
        self.assertEqual(self.saved(run).status, "pending")
        self.configure(run)
        self.execute(run)
        self.assertEqual(
            self.conversations.get_run(self.identity(), self.tenant, run).status, "completed"
        )
        self.assertEqual(self.model.requests, [])
        saved = self.db.document(f"tenants/{self.tenant}/run_proposals/{proposal}").get().to_dict()
        assert saved is not None
        self.assertEqual(saved["status"], "expired")

    def test_consumed_result_deleted_while_next_confirmation_waits_is_not_reused(self) -> None:
        _, run = self.setup_run()
        call = cast(ConfirmationModel, self.model).call
        first = ResourceId(json.loads(call.arguments_json)["note_id"])
        first_ref = self.db.document(f"tenants/{self.tenant}/notes/{first}")
        second = ResourceId(str(uuid4()))
        source = first_ref.get().to_dict()
        assert source is not None
        self.db.document(f"tenants/{self.tenant}/notes/{second}").set({**source, "id": second})
        other = call.model_copy(
            update={
                "call_key": "second",
                "arguments_json": json.dumps({"note_id": second, "expected_version": 1}),
            }
        )
        self.model = MultiStageModel((call, other))  # type: ignore[assignment]
        self.handler = RunTurn(
            self.jobs,
            self.model,
            credential_epoch=self.tokens.epoch,
            tool_registry=self.effect.registry,
            tool_authorizer=ConfirmationAuthority(self.jobs, run),
        )
        self.worker = Worker(self.jobs, LocalCaller(), {KIND: self.handler})
        self.confirmations = Confirmations(
            self.jobs,
            self.handler.state,
            registry=self.effect.registry,
            result_authorizer=cast(ConfirmationAuthority, self.handler.tool_authorizer),
        )
        self.execute(run)
        self.decision(run, self.proposal(run))
        self.execute(run)
        self.assertEqual(len(self.model.requests), 2)
        self.assertEqual(len(self.effect.calls), 1)
        first_ref.update({"deleted": True})
        self.decision(run, self.proposal(run))
        self.execute(run)
        self.assertEqual(
            self.conversations.get_run(self.identity(), self.tenant, run).status, "failed"
        )
        self.assertEqual(len(self.model.requests), 2)
        self.assertEqual(len(self.effect.calls), 1)

    def test_historical_result_reuse_checks_the_new_reader(self) -> None:
        conversation, run = self.setup_run()
        note_id = json.loads(cast(ConfirmationModel, self.model).call.arguments_json)["note_id"]
        self.execute(run)
        self.decision(run, self.proposal(run))
        self.execute(run)
        self.assertEqual(
            self.conversations.get_run(self.identity(), self.tenant, run).status, "completed"
        )
        self.db.document(f"tenants/{self.tenant}/notes/{note_id}").update(
            {
                "visibility": "private",
                "owner_user_id": self.identity().user_id,
                "display_policy": "authenticated_session",
            }
        )
        reader = self.identity().model_copy(update={"user_id": FirebaseUid("demo-a-member")})
        following = self.conversations.turn(
            reader,
            self.tenant,
            conversation,
            TurnCreate(text="Fixture new reader", output_mode="text"),
            "emulator-result-new-reader",
        )
        self.execute(following.run_id)
        self.assertEqual(
            self.conversations.get_run(reader, self.tenant, following.run_id).status, "failed"
        )
        self.assertEqual(len(self.model.requests), 2)
        self.assertEqual(len(self.effect.calls), 1)

    def result_audience_case(self, *, private: bool, private_source: bool) -> None:
        conversation, run = self.setup_run(private=private)
        note = ResourceId(
            json.loads(cast(ConfirmationModel, self.model).call.arguments_json)["note_id"]
        )
        self.db.document(f"tenants/{self.tenant}/notes/{note}").update(
            {
                "visibility": "private" if private_source else "shared",
                "owner_user_id": self.identity().user_id,
                "display_policy": "authenticated_session" if private_source else "shared_display",
                "text": "FIXTURE_EMULATOR_RESULT_SECRET",
            }
        )
        model = EchoResultModel(note)
        self.model = model  # type: ignore[assignment]
        self.handler = RunTurn(
            self.jobs,
            model,
            credential_epoch=self.tokens.epoch,
            tool_registry=self.effect.registry,
            tool_authorizer=ConfirmationAuthority(self.jobs, run),
        )
        self.worker = Worker(self.jobs, LocalCaller(), {KIND: self.handler})
        self.confirmations = Confirmations(
            self.jobs,
            self.handler.state,
            registry=self.effect.registry,
            result_authorizer=cast(ConfirmationAuthority, self.handler.tool_authorizer),
        )
        self.execute(run)
        saved = next(d for _, d in self.persistence.page(f"tenants/{self.tenant}/tool_executions"))
        claim = self.start_external(run, self.proposal(run))
        self.confirmations.receive_external(
            claim,
            ToolResult(
                tool_call_id=saved["tool_call_id"],
                operation_id=saved["effect_id"],
                status="succeeded",
                result={"text": "FIXTURE_EMULATOR_RESULT_SECRET"},
            ),
        )
        allowed = private or not private_source
        if allowed:
            self.execute(run)
        else:
            self.assertIsNone(
                self.jobs.prepare_dispatch(self.tenant, job_id(self.tenant, KIND, run))
            )
        self.assertEqual(
            self.conversations.get_run(self.identity(), self.tenant, run).status,
            "completed" if allowed else "failed",
        )
        self.assertEqual(len(model.requests), 2 if allowed else 1)
        self.assertEqual(self.effect.calls, [])
        pairing = self.pairing()
        device = self.confirm(pairing)
        self.db.document(f"tenants/{self.tenant}/devices/{device.id}").update(
            {"capabilities": ["display"]}
        )
        credentials = self.exchange(pairing)
        device_identity = self.repo.authenticate(credentials.access_token)
        member = self.identity().model_copy(update={"user_id": FirebaseUid("demo-a-member")})
        for reader in (self.identity(), member, device_identity):
            if private and reader != self.identity():
                with self.assertRaises(APIError):
                    self.conversations.messages(reader, self.tenant, conversation)
                with self.assertRaises(APIError):
                    next_batch(self.conversations, reader, self.tenant, run, None, 0)
                continue
            messages = self.conversations.messages(reader, self.tenant, conversation).items
            self.assertEqual(
                any("FIXTURE_EMULATOR_RESULT_SECRET" in m.text for m in messages), allowed
            )
            after, texts = 0, []
            while True:
                batch = next_batch(self.conversations, reader, self.tenant, run, None, after)
                if batch.event:
                    after = batch.event.seq
                    if batch.event.type == "text.delta":
                        texts.append(batch.event.payload.text)
                elif batch.terminal:
                    break
                else:
                    self.fail("expected terminal stream")
            self.assertEqual("FIXTURE_EMULATOR_RESULT_SECRET" in "".join(texts), allowed)

    def test_private_result_is_rejected_before_shared_model_or_public_projection(self) -> None:
        self.result_audience_case(private=False, private_source=True)

    def test_private_result_is_available_only_in_owners_private_conversation(self) -> None:
        self.result_audience_case(private=True, private_source=True)

    def test_compatible_shared_result_reaches_member_and_shared_pi(self) -> None:
        self.result_audience_case(private=False, private_source=False)

    def external_fixture(self) -> tuple[ResourceId, ResourceId, ResourceId]:
        conversation, run = self.setup_run()
        authority = cast(ConfirmationAuthority, self.handler.tool_authorizer)
        authority.waiting = False
        self.effect = ProposalCreatingEffect(self.jobs)  # type: ignore[assignment]
        self.handler = RunTurn(
            self.jobs,
            self.model,
            credential_epoch=self.tokens.epoch,
            tool_registry=self.effect.registry,
            tool_authorizer=authority,
        )
        self.worker = Worker(self.jobs, LocalCaller(), {KIND: self.handler})
        self.confirmations = Confirmations(
            self.jobs,
            self.handler.state,
            registry=self.effect.registry,
            result_authorizer=authority,
        )
        self.execute(run)
        return conversation, run, self.proposal(run)

    def test_external_claim_is_atomic_and_late_success_resumes_after_restart(self) -> None:
        _, run, proposal = self.external_fixture()
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(lambda _: self.start_external(run, proposal), range(2)))
        claim = claims[0]
        self.assertEqual(claims[1], claim)
        self.assertEqual(
            len(self.persistence.page(f"tenants/{self.tenant}/fixture_external_jobs")), 1
        )
        self.assertEqual(self.saved(run).status, "waiting")
        self.assertIsNone(self.saved(run).lease_expires_at)
        self.now += timedelta(seconds=301)
        self.confirmations.expire(self.tenant, self.persistence)
        self.assertEqual(self.saved(run).status, "waiting")
        self.assertEqual(
            self.confirmations.receive_external(
                claim,
                ToolResult(
                    tool_call_id=claim.tool_call_id,
                    operation_id=claim.effect_id,
                    status="succeeded",
                    result={"external": "late success"},
                ),
            ),
            "queued",
        )
        result = self.checkpoint_process(run, proposal_handler=True)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["tool_calls"], 0)
        self.assertEqual(
            self.conversations.get_run(self.identity(), self.tenant, run).status, "completed"
        )
        tool = (
            self.db.document(f"tenants/{self.tenant}/tool_executions/{claim.tool_call_id}")
            .get()
            .to_dict()
        )
        assert tool is not None
        self.assertEqual(tool["result"]["status"], "succeeded")

    def test_external_unknown_is_persisted_once_and_terminates_without_model(self) -> None:
        conversation, run, proposal = self.external_fixture()
        claim = self.start_external(run, proposal)
        result = ToolResult(
            tool_call_id=claim.tool_call_id,
            operation_id=claim.effect_id,
            status="outcome_unknown",
            result={},
        )
        for _ in range(2):
            self.assertEqual(self.confirmations.receive_external(claim, result), "queued")
        with self.assertRaises(PolicyDenied):
            self.confirmations.receive_external(
                claim, result.model_copy(update={"status": "succeeded"})
            )
        self.now += timedelta(seconds=301)
        self.confirmations.expire(self.tenant, self.persistence)
        result_process = self.checkpoint_process(run, proposal_handler=True)
        self.assertEqual(result_process["status"], "succeeded")
        self.assertEqual(result_process["model_calls"], 0)
        self.assertEqual(result_process["tool_calls"], 0)
        self.assertEqual(
            self.conversations.get_run(self.identity(), self.tenant, run).error_code,
            "outcome_unknown",
        )
        self.assertFalse(
            any(
                "操作は実行しませんでした" in m.text
                for m in self.conversations.messages(
                    self.identity(), self.tenant, conversation
                ).items
            )
        )

    def test_external_claim_cancel_race_preserves_started_fact_without_resuming(self) -> None:
        _, run, proposal = self.external_fixture()

        def claim_start() -> ExternalClaim | None:
            try:
                return self.start_external(run, proposal)
            except PolicyDenied:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            claiming = pool.submit(claim_start)
            cancelling = pool.submit(
                self.conversations.cancel_run,
                self.identity(),
                self.tenant,
                run,
                "external-cancel-race",
            )
            claim = claiming.result()
            cancelling.result()
        event = next(
            d
            for _, d in self.persistence.page(f"tenants/{self.tenant}/runs/{run}/events")
            if d["type"] == "run.cancelled"
        )
        self.assertEqual(event["payload"]["external_action_may_have_completed"], claim is not None)
        if claim is not None:
            result = ToolResult(
                tool_call_id=claim.tool_call_id,
                operation_id=claim.effect_id,
                status="succeeded",
                result={"secret": "LATE_CANCELLED_FIXTURE"},
            )
            self.assertEqual(self.confirmations.receive_external(claim, result), "recorded")
            tool = (
                self.db.document(f"tenants/{self.tenant}/tool_executions/{claim.tool_call_id}")
                .get()
                .to_dict()
            )
            assert tool is not None
            self.assertEqual(tool["result"]["status"], "succeeded")
            self.assertEqual(tool["result"]["result"], {})
        self.assertEqual(
            self.conversations.get_run(self.identity(), self.tenant, run).status, "cancelled"
        )
        self.assertIsNone(self.jobs.prepare_dispatch(self.tenant, self.saved(run).id))

    def test_external_receipt_after_source_deletion_or_suppression_is_content_free(self) -> None:
        for field in ("deleted", "suppress_reuse"):
            with self.subTest(field=field):
                _, run, proposal = self.external_fixture()
                claim = self.start_external(run, proposal)
                note = json.loads(cast(ConfirmationModel, self.model).call.arguments_json)[
                    "note_id"
                ]
                target = self.db.document(f"tenants/{self.tenant}/notes/{note}")
                target.update({field: True})
                result = ToolResult(
                    tool_call_id=claim.tool_call_id,
                    operation_id=claim.effect_id,
                    status="succeeded",
                    result={"secret": "DELETED_RESULT_FIXTURE"},
                )
                self.assertEqual(self.confirmations.receive_external(claim, result), "recorded")
                tool = (
                    self.db.document(f"tenants/{self.tenant}/tool_executions/{claim.tool_call_id}")
                    .get()
                    .to_dict()
                )
                assert tool is not None
                self.assertEqual(tool["result"]["status"], "succeeded")
                self.assertEqual(tool["result"]["result"], {})
                self.assertEqual(self.saved(run).status, "obsolete")
                target.update({field: False})

    def test_inactive_account_expiry_does_not_block_later_proposals(self) -> None:
        from unittest.mock import patch

        owner, admin = (
            self.identity(),
            self.identity().model_copy(update={"user_id": FirebaseUid("demo-a-admin")}),
        )
        pending = []
        for identity in (owner, admin):
            with patch.object(self, "identity", return_value=identity):
                _, run = self.setup_run()
                self.execute(run)
                pending.append((self.proposal(run), run, identity))
        pending.sort(key=lambda item: item[0])
        expired, healthy = pending
        account = next(
            d
            for d in self.db.collection("users").stream()
            if (d.to_dict() or {}).get("user_id") == expired[2].user_id
        )
        account.reference.update({"status": "deleting"})
        self.now += timedelta(seconds=301)
        self.confirmations.expire(self.tenant, self.persistence)
        self.assertEqual(self.saved(expired[1]).status, "obsolete")
        self.assertEqual(self.saved(healthy[1]).status, "pending")
        self.configure(healthy[1])
        self.execute(healthy[1])
        self.assertEqual(
            self.conversations.get_run(healthy[2], self.tenant, healthy[1]).status, "completed"
        )
        self.assertEqual(
            self.conversations.get_run(healthy[2], self.tenant, expired[1]).status, "failed"
        )


def checkpoint_worker_process() -> None:
    client, jobs, _ = process_services()
    tenant, run = ResourceId(sys.argv[2]), ResourceId(sys.argv[3])
    now = normalize_utc(sys.argv[5])
    jobs.now = lambda: now
    try:
        note = next(
            d.to_dict()
            for d in client.collection(f"tenants/{tenant}/notes").stream()
            if d.to_dict()["visibility"] == "shared"
        )
        model, effect = ConfirmationModel(ResourceId(note["id"])), DurableEffect(jobs)
        authority = ConfirmationAuthority(jobs, run)
        authority.waiting = sys.argv[6] != "proposal"
        handler = RunTurn(
            jobs,
            model,
            credential_epoch=None,
            tool_registry=effect.registry,
            tool_authorizer=authority,
        )
        worker = Worker(jobs, LocalCaller(), {KIND: handler})
        identity = job_id(tenant, KIND, run)
        delivery = jobs.prepare_dispatch(tenant, identity)
        if delivery is not None:
            worker.handle(LOCAL_AUTHORIZATION, delivery.model_dump(mode="json"))
        saved = jobs.get(tenant, identity)
        assert saved is not None
        Path(sys.argv[4]).write_text(
            json.dumps(
                {
                    "status": saved.status,
                    "model_calls": len(model.requests),
                    "tool_calls": len(effect.calls),
                }
            )
        )
    finally:
        client.close()
