"""T-007/T-037: durable cancellation, one-shot confirmation and bounded expiry recovery."""

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any, cast
from unittest.mock import patch

from brainoid_agent_core.types import ToolResult
from brainoid_api.errors import APIError
from brainoid_api.routes.agents import reader_identity
from brainoid_api.routes.run_events import next_batch
from brainoid_contracts import FirebaseUid, ResourceId
from brainoid_jobs.models import job_id
from brainoid_policy.models import UserSubject
from brainoid_policy.scopes import PolicyDenied, PolicyUnavailable
from brainoid_runs.state import KIND
from brainoid_usage import Policy, scope_key
from brainoid_worker.dispatcher import Dispatcher
from brainoid_worker.entry import Worker
from brainoid_worker.local import LOCAL_AUTHORIZATION, LocalCaller, LocalQueue
from brainoid_worker.resume_run import Confirmations, ExternalClaim
from brainoid_worker.run_turn import RunTurn
from fakes.run_confirmation import (
    ConfirmationAuthority,
    ConfirmationEffect,
    ConfirmationModel,
    EchoResultModel,
    ExternalBusinessQueue,
)
from test_jobs import JobStore
from test_turn_run import TurnRunFixture


class CheckpointTests(TurnRunFixture):
    def configure(self, run: ResourceId) -> None:
        note = ResourceId(
            next(
                d["id"]
                for p, d in self.store.data.items()
                if p.startswith(f"tenants/{self.tenant}/notes/") and d["visibility"] == "shared"
            )
        )
        self.model = ConfirmationModel(note)  # type: ignore[assignment]
        self.effect = ConfirmationEffect()
        self.authority = ConfirmationAuthority(self.jobs, run)
        self.restart()

    def restart(self) -> None:
        self.handler = RunTurn(
            self.jobs,
            self.model,
            credential_epoch=self.tokens.epoch,
            tool_registry=self.effect.registry,
            tool_authorizer=self.authority,
        )
        self.worker = Worker(self.jobs, LocalCaller(), {KIND: self.handler})
        self.confirmations = Confirmations(
            self.jobs,
            self.handler.state,
            registry=self.effect.registry,
            result_authorizer=self.authority,
        )

    def setup_run(self, *, private: bool = False) -> tuple[ResourceId, ResourceId]:
        self.store.data[f"tenants/{self.tenant}/agents/{self.agent_id}"]["allowed_tools"] = [
            "notes.delete"
        ]
        conversation = self.conversation(private=private)
        run = ResourceId(self.turn(conversation).json()["run_id"])
        self.configure(run)
        return conversation, run

    def proposal(self, run: ResourceId) -> ResourceId:
        return ResourceId(self.get_run(run).json()["waiting_proposal_id"])

    def decision(
        self, run: ResourceId, proposal: ResourceId, value: Any = "confirmed", **kwargs: Any
    ) -> str:
        return self.confirmations.decide(
            self.tenant,
            run,
            proposal,
            value,
            subject=UserSubject(user_id=self.identity().user_id),
            **kwargs,
        )

    def start_external(self, run: ResourceId, proposal: ResourceId) -> ExternalClaim:
        return self.confirmations.start_external(
            self.tenant,
            run,
            proposal,
            subject=UserSubject(user_id=self.identity().user_id),
            prepare=ExternalBusinessQueue(self.effect.registry, self.authority).prepare,
        )

    def external_result(self, run: ResourceId, proposal: ResourceId, *, result: ToolResult) -> str:
        claim = self.start_external(run, proposal)
        return self.confirmations.receive_external(claim, result)

    def cancel(self, run: ResourceId, key: str = "fixture-cancel-key", **headers: str) -> Any:
        return self.client.post(
            f"/v1/tenants/{self.tenant}/runs/{run}:cancel",
            headers={"Idempotency-Key": key, **headers},
        )

    def test_wait_restart_duplicate_confirmation_and_signed_continuation(self) -> None:
        conversation, run = self.setup_run()
        self.model.prefix = "確認します。" * 260  # type: ignore[attr-defined]
        old = self.delivery(run)
        self.worker.handle(LOCAL_AUTHORIZATION, old.model_dump(mode="json"))
        proposal = self.proposal(run)
        job = self.jobs.get(self.tenant, old.job_id)
        assert job is not None
        self.assertEqual(job.status, "waiting")
        self.assertIsNone(job.lease_expires_at)
        queue = LocalQueue()
        recovered = Dispatcher(self.jobs, cast(JobStore, self.store), queue).recover(self.tenant)
        self.assertEqual(recovered.dispatched, 0)
        self.assertEqual(self.turn(conversation, key="while-waiting-key").status_code, 409)
        self.assertEqual(self.effect.calls, [])
        self.restart()
        self.store.retry_once = True
        self.assertEqual(self.decision(run, proposal), "confirmed")
        self.assertEqual(self.decision(run, proposal), "confirmed")
        with self.assertRaises(PolicyDenied):
            self.decision(run, proposal, "declined")
        self.worker.handle(LOCAL_AUTHORIZATION, old.model_dump(mode="json"))
        self.assertEqual(self.effect.calls, [])
        delivery = self.delivery(run)
        self.worker.handle(LOCAL_AUTHORIZATION, delivery.model_dump(mode="json"))
        self.worker.handle(LOCAL_AUTHORIZATION, delivery.model_dump(mode="json"))
        self.assertEqual(self.get_run(run).json()["status"], "completed", self.get_run(run).text)
        self.assertEqual(len(self.effect.calls), 1)
        self.assertEqual(len(self.model.requests), 2)
        messages = self.conversations.messages(self.identity(), self.tenant, conversation).items
        self.assertEqual(messages[-1].text, self.model.prefix + "FIXTURE_完了。")  # type: ignore[attr-defined]
        prefix = f"tenants/{self.tenant}/runs/{run}/events/"
        text = "".join(
            d["payload"]["text"]
            for p, d in self.store.data.items()
            if p.startswith(prefix) and d["type"] == "text.delta"
        )
        self.assertEqual(text, messages[-1].text)
        following = ResourceId(self.turn(conversation, key="following-turn-key").json()["run_id"])
        self.execute(following)
        history = self.model.requests[-1].messages
        signatures = [m.provider_state for m in history if m.provider_state]
        self.assertEqual(
            signatures,
            [{"signature": "PRIVATE_SIGNATURE_CALL"}, {"signature": "PRIVATE_SIGNATURE_FINAL"}],
        )
        self.assertTrue(any(m.role == "tool" for m in history))
        self.assertNotIn("PRIVATE_SIGNATURE", self.get_run(run).text)

    def test_external_result_is_consumed_without_reexecuting_original_tool(self) -> None:
        _, run = self.setup_run()
        self.execute(run)
        proposal = self.proposal(run)
        saved = next(d for p, d in self.store.data.items() if "/tool_executions/" in p)
        result = ToolResult(
            tool_call_id=saved["tool_call_id"],
            operation_id=saved["effect_id"],
            status="succeeded",
            result={"external": "committed"},
        )
        self.external_result(run, proposal, result=result)
        self.external_result(run, proposal, result=result)
        result.result["external"] = "mutated"
        self.restart()
        self.execute(run)
        self.assertEqual(self.get_run(run).json()["status"], "completed")
        self.assertEqual(self.effect.calls, [])
        self.assertIn("committed", str(self.model.requests[-1].messages))

    def result_audience_case(
        self, *, private_conversation: bool, private_source: bool, display: str, allowed: bool
    ) -> None:
        conversation, run = self.setup_run(private=private_conversation)
        note = ResourceId(
            json.loads(cast(ConfirmationModel, self.model).call.arguments_json)["note_id"]
        )
        path = f"tenants/{self.tenant}/notes/{note}"
        self.store.data[path].update(
            visibility="private" if private_source else "shared",
            owner_user_id=self.identity().user_id,
            display_policy=display,
            text="FIXTURE_RESULT_AUDIENCE_SECRET",
        )
        model = EchoResultModel(note)
        self.model = model  # type: ignore[assignment]
        self.restart()
        self.execute(run)
        saved = next(d for p, d in self.store.data.items() if "/tool_executions/" in p)
        self.external_result(
            run,
            self.proposal(run),
            result=ToolResult(
                tool_call_id=saved["tool_call_id"],
                operation_id=saved["effect_id"],
                status="succeeded",
                result={"text": "FIXTURE_RESULT_AUDIENCE_SECRET"},
            ),
        )
        self.restart()
        if allowed:
            self.execute(run)
        else:
            self.assertIsNone(
                self.jobs.prepare_dispatch(self.tenant, job_id(self.tenant, KIND, run))
            )
        self.assertEqual(self.get_run(run).json()["status"], "completed" if allowed else "failed")
        self.assertEqual(len(model.requests), 2 if allowed else 1)
        self.assertEqual(self.effect.calls, [])
        credentials = self.credentials()
        self.store.data[f"tenants/{self.tenant}/devices/{credentials.device_id}"][
            "capabilities"
        ] = ["display"]
        device = self.repo.authenticate(credentials.access_token)
        member = self.identity().model_copy(update={"user_id": FirebaseUid("demo-a-member")})
        for reader in (self.identity(), member, device):
            if private_conversation and reader != self.identity():
                with self.assertRaises(APIError):
                    self.conversations.messages(reader, self.tenant, conversation)
                with self.assertRaises(APIError):
                    next_batch(self.conversations, reader, self.tenant, run, None, 0)
                continue
            messages = self.conversations.messages(reader, self.tenant, conversation).items
            self.assertEqual(
                any("FIXTURE_RESULT_AUDIENCE_SECRET" in message.text for message in messages),
                allowed,
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
            self.assertEqual("FIXTURE_RESULT_AUDIENCE_SECRET" in "".join(texts), allowed)

    def test_private_result_never_reaches_shared_model_history_or_sse(self) -> None:
        self.result_audience_case(
            private_conversation=False,
            private_source=True,
            display="authenticated_session",
            allowed=False,
        )

    def test_result_display_policy_cannot_be_widened_by_shared_transcript(self) -> None:
        self.result_audience_case(
            private_conversation=False,
            private_source=False,
            display="authenticated_session",
            allowed=False,
        )

    def test_never_display_result_cannot_enter_shared_transcript(self) -> None:
        self.result_audience_case(
            private_conversation=False, private_source=False, display="never", allowed=False
        )

    def test_owner_private_result_can_continue_in_own_private_conversation(self) -> None:
        self.result_audience_case(
            private_conversation=True,
            private_source=True,
            display="authenticated_session",
            allowed=True,
        )

    def test_shared_result_remains_readable_by_member_and_shared_pi(self) -> None:
        self.result_audience_case(
            private_conversation=False, private_source=False, display="shared_display", allowed=True
        )

    def test_private_result_source_owner_must_match_saved_conversation_owner(self) -> None:
        from brainoid_contracts import DisplayPolicy, Scope, Visibility
        from brainoid_tools import ResultConsumer, ResultSource, ResultUse, ToolRejected
        from brainoid_tools.reuse import receipt

        conversation, run = self.setup_run(private=True)
        job = self.jobs.get(self.tenant, job_id(self.tenant, KIND, run))
        assert job is not None
        consumer = ResultConsumer(
            run_id=run,
            context=job.grant.context,
            conversation_id=conversation,
            conversation_scope=job.grant.scope,
            conversation_owner_user_id=job.grant.owner_user_id,
        )
        result_use = ResultUse(
            receipt="a" * 64,
            sources=(
                ResultSource(
                    scope=Scope(
                        visibility=Visibility.PRIVATE,
                        display_policy=DisplayPolicy.AUTHENTICATED_SESSION,
                    ),
                    owner_user_id=FirebaseUid("demo-a-member"),
                ),
            ),
        )
        with self.assertRaises(ToolRejected):
            receipt(result_use, consumer)

    def test_decline_and_expiry_resume_without_provider_or_tool(self) -> None:
        for decision in ("declined", "expired"):
            with self.subTest(decision=decision):
                _, run = self.setup_run()
                self.execute(run)
                proposal = self.proposal(run)
                if decision == "expired":
                    self.clock.advance(301)
                    self.confirmations.expire(self.tenant, cast(JobStore, self.store), limit=1)
                    # A bounded sweep caller follows its cursor; all pages here are tiny.
                    self.confirmations.expire(self.tenant, cast(JobStore, self.store))
                else:
                    self.decision(run, proposal, decision)
                self.restart()
                self.execute(run)
                self.assertEqual(
                    self.get_run(run).json()["status"], "completed", self.get_run(run).text
                )
                self.assertEqual(self.effect.calls, [])
                self.assertEqual(len(self.model.requests), 1)

    def test_cancel_queued_running_waiting_and_completed_is_idempotent(self) -> None:
        _, run = self.setup_run()
        self.assertEqual(self.cancel(run).json()["status"], "cancelled")
        self.assertEqual(self.cancel(run).json()["status"], "cancelled")
        self.assertIsNone(self.jobs.prepare_dispatch(self.tenant, job_id(self.tenant, KIND, run)))
        _, run = self.setup_run()
        self.execute(run)
        proposal = self.proposal(run)
        self.assertEqual(self.cancel(run).json()["status"], "cancelled")
        with self.assertRaises(PolicyDenied):
            self.decision(run, proposal)
        self.assertTrue(
            next_batch(self.conversations, self.identity(), self.tenant, run, None, 0).terminal
        )
        self.assertEqual(self.effect.calls, [])
        _, run = self.setup_run()
        self.authority.waiting = False
        self.effect.hook = lambda: self.cancel(run)
        self.execute(run)
        self.assertEqual(self.get_run(run).json()["status"], "cancelled")
        events = [d for p, d in self.store.data.items() if f"/runs/{run}/events/" in p]
        self.assertTrue(events[-1]["payload"]["external_action_may_have_completed"])
        self.assertEqual(len(self.effect.calls), 1)

    def test_cancel_authority_shared_member_rejected_and_revoked_replay(self) -> None:
        _, run = self.setup_run()
        other = self.identity().model_copy(update={"user_id": FirebaseUid("demo-a-member")})
        self.app.dependency_overrides[reader_identity] = lambda: other
        self.assertEqual(self.cancel(run).status_code, 403)
        self.app.dependency_overrides[reader_identity] = lambda: self.identity()
        self.assertEqual(self.cancel(run).status_code, 200)
        self.store.data[f"tenants/{self.tenant}/memberships/{self.identity().user_id}"][
            "status"
        ] = "revoked"
        self.assertNotEqual(self.cancel(run).status_code, 200)

    def test_confirm_cancel_race_fences_tool_start(self) -> None:
        _, run = self.setup_run()
        self.execute(run)
        proposal = self.proposal(run)

        def confirm() -> None:
            try:
                self.decision(run, proposal)
            except PolicyDenied:
                pass

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures: list[Any] = [
                pool.submit(confirm),
                pool.submit(
                    self.conversations.cancel_run,
                    self.identity(),
                    self.tenant,
                    run,
                    "fixture-race-cancel-key",
                ),
            ]
            for future in futures:
                future.result()
        self.assertEqual(self.get_run(run).json()["status"], "cancelled")
        self.assertIsNone(self.jobs.prepare_dispatch(self.tenant, job_id(self.tenant, KIND, run)))
        self.assertEqual(self.effect.calls, [])

    def test_resume_rechecks_deleted_source_and_expired_private_session(self) -> None:
        _, run = self.setup_run()
        self.execute(run)
        proposal = self.proposal(run)
        self.decision(run, proposal)
        note = json.loads(cast(ConfirmationModel, self.model).call.arguments_json)["note_id"]
        self.store.data[f"tenants/{self.tenant}/notes/{note}"]["deleted"] = True
        self.execute(run)
        self.assertEqual(self.effect.calls, [])
        self.assertEqual(self.get_run(run).json()["status"], "failed")

    def test_private_output_session_expiry_refuses_confirmation_and_recovery(self) -> None:
        from brainoid_api.services.private_sessions import PrivateSessionCreate
        from brainoid_contracts import DisplayPolicy

        self.store.data[f"tenants/{self.tenant}/agents/{self.agent_id}"]["allowed_tools"] = [
            "notes.delete"
        ]
        credentials = self.credentials()
        self.store.data[f"tenants/{self.tenant}/devices/{credentials.device_id}"][
            "capabilities"
        ] = ["display"]
        device = self.repo.authenticate(credentials.access_token)
        session = self.sessions.start(
            self.identity(),
            self.tenant,
            device.sub,
            PrivateSessionCreate(
                acknowledge_shared_space=True, allowed_scopes=["conversation", "notes"]
            ),
            "checkpoint-session-key",
            "fixture",
        )
        conversation = self.conversation(private=True, display=DisplayPolicy.AUTHENTICATED_SESSION)
        response = self.client.post(
            self.url + f"/{conversation}/turns",
            json={
                "text": "Fixture confirmation",
                "output_mode": "text",
                "output_device_id": device.sub,
            },
            headers={"Idempotency-Key": "checkpoint-private-turn"},
        )
        run = ResourceId(response.json()["run_id"])
        self.configure(run)
        self.execute(run)
        proposal = self.proposal(run)
        self.store.data[f"tenants/{self.tenant}/private_sessions/{session.id}"]["status"] = "ended"
        with self.assertRaises(PolicyDenied):
            self.decision(run, proposal)
        self.clock.advance(301)
        self.confirmations.expire(self.tenant, cast(JobStore, self.store))
        self.assertEqual(self.effect.calls, [])
        self.assertEqual(self.get_run(run).json()["status"], "failed")

    def test_suspension_infrastructure_failure_replays_model_and_wait_result(self) -> None:
        from brainoid_persistence.firestore import PersistenceUnavailable

        _, run = self.setup_run()
        # The worker constructs its own controller; fail only at the durable handoff.
        with patch(
            "brainoid_worker.run_turn.Confirmations.suspend", side_effect=PersistenceUnavailable()
        ):
            with self.assertRaises(PersistenceUnavailable):
                self.execute(run)
        self.clock.advance(6)
        self.restart()
        self.execute(run)
        self.assertEqual(self.get_run(run).json()["status"], "waiting_confirmation")
        self.assertEqual(len(self.model.requests), 1)
        self.assertEqual(self.effect.calls, [])

    def test_expiry_authority_outage_does_not_make_job_obsolete(self) -> None:
        from brainoid_jobs import AuthorizationUnavailable

        _, run = self.setup_run()
        self.execute(run)
        self.clock.advance(301)
        with patch.object(self.jobs, "_policy", side_effect=PolicyUnavailable()):
            with self.assertRaises(AuthorizationUnavailable):
                self.confirmations.expire(self.tenant, cast(JobStore, self.store))
        job = self.jobs.get(self.tenant, job_id(self.tenant, KIND, run))
        assert job is not None
        self.assertEqual(job.status, "waiting")
        self.confirmations.expire(self.tenant, cast(JobStore, self.store))
        self.execute(run)
        self.assertEqual(self.get_run(run).json()["status"], "completed")

    def test_untrusted_external_result_and_different_replay_are_rejected(self) -> None:
        _, run = self.setup_run()
        self.execute(run)
        proposal = self.proposal(run)
        saved = next(d for p, d in self.store.data.items() if "/tool_executions/" in p)
        result = ToolResult(
            tool_call_id=saved["tool_call_id"],
            operation_id=saved["effect_id"],
            status="succeeded",
            result={"body": "x" * 66000},
        )
        with self.assertRaises(ValueError):
            self.external_result(run, proposal, result=result)
        result = result.model_copy(update={"result": {"body": "first"}})
        self.external_result(run, proposal, result=result)
        with self.assertRaises(PolicyDenied):
            self.external_result(
                run, proposal, result=result.model_copy(update={"result": {"body": "different"}})
            )
        self.execute(run)
        self.assertEqual(self.effect.calls, [])

    def test_device_initiator_can_cancel_own_run_and_session_revocation_denies(self) -> None:
        from brainoid_api.services.private_sessions import PrivateSessionCreate
        from brainoid_contracts import DisplayPolicy

        credentials = self.credentials()
        self.store.data[f"tenants/{self.tenant}/devices/{credentials.device_id}"][
            "capabilities"
        ] = ["display"]
        device = self.repo.authenticate(credentials.access_token)
        conversation = self.conversation()
        self.app.dependency_overrides[reader_identity] = lambda: device
        run = ResourceId(self.turn(conversation).json()["run_id"])
        self.assertEqual(self.cancel(run).json()["status"], "cancelled")
        self.app.dependency_overrides[reader_identity] = lambda: self.identity()
        session = self.sessions.start(
            self.identity(),
            self.tenant,
            device.sub,
            PrivateSessionCreate(acknowledge_shared_space=True, allowed_scopes=["conversation"]),
            "checkpoint-pi-session-key",
            "fixture",
        )
        private = self.conversation(private=True, display=DisplayPolicy.AUTHENTICATED_SESSION)
        self.app.dependency_overrides[reader_identity] = lambda: device
        response = self.client.post(
            self.url + f"/{private}/turns",
            json={"text": "Private fixture", "output_mode": "text"},
            headers={
                "X-Private-Session": session.id,
                "Idempotency-Key": "checkpoint-pi-private-turn",
            },
        )
        run = ResourceId(response.json()["run_id"])
        self.store.data[f"tenants/{self.tenant}/private_sessions/{session.id}"]["status"] = "ended"
        self.assertNotEqual(self.cancel(run, **{"X-Private-Session": session.id}).status_code, 200)

    def test_blocked_model_observes_durable_cancellation_without_waiting_for_text(self) -> None:
        import asyncio
        import time
        from collections.abc import AsyncGenerator

        from brainoid_agent_core.ports import CallControl
        from brainoid_agent_core.types import Completed, ModelEvent, ModelRequest

        _, run = self.setup_run()
        fixture = self

        class BlockingModel:
            async def stream(
                self, request: ModelRequest, *, control: CallControl
            ) -> AsyncGenerator[ModelEvent]:
                fixture.cancel(run)
                await asyncio.sleep(10)
                yield Completed()

        self.handler.model = BlockingModel()
        started = time.monotonic()
        self.execute(run)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(self.get_run(run).json()["status"], "cancelled")
        self.assertEqual(self.effect.calls, [])

    def test_long_multi_stage_work_renews_lease_at_effect_boundaries(self) -> None:
        _, run = self.setup_run()
        self.authority.waiting = False
        self.model.hook = lambda: self.clock.advance(40)
        self.effect.hook = lambda: self.clock.advance(40)
        self.execute(run)
        self.assertEqual(self.get_run(run).json()["status"], "completed")
        self.assertEqual(len(self.effect.calls), 1)
        self.assertEqual(len(self.model.requests), 2)

    def test_tool_provider_without_optional_model_response_can_resume(self) -> None:
        _, run = self.setup_run()
        cast(ConfirmationModel, self.model).signed = False
        self.execute(run)
        self.decision(run, self.proposal(run))
        self.execute(run)
        self.assertEqual(self.get_run(run).json()["status"], "completed")
        self.assertEqual(len(self.effect.calls), 1)
        self.assertTrue(all(m.provider_state is None for m in self.model.requests[-1].messages))

    def test_output_budget_is_shared_by_before_and_after_confirmation(self) -> None:
        conversation, run = self.setup_run()
        model = cast(ConfirmationModel, self.model)
        model.prefix, model.final_text = "a" * 5000, "b" * 5000
        self.execute(run)
        self.decision(run, self.proposal(run))
        self.execute(run)
        self.assertEqual(self.get_run(run).json()["status"], "completed")
        text = (
            self.conversations.messages(self.identity(), self.tenant, conversation).items[-1].text
        )
        self.assertEqual(text, "a" * 5000 + "b" * 2900)
        self.assertLessEqual(len(text), 8000)

    def test_tool_authority_outage_defers_without_reinvoking_model(self) -> None:
        from brainoid_jobs import AuthorizationUnavailable

        _, run = self.setup_run()
        with patch.object(self.authority, "check", side_effect=PolicyUnavailable()):
            with self.assertRaises(AuthorizationUnavailable):
                self.execute(run)
        job = self.jobs.get(self.tenant, job_id(self.tenant, KIND, run))
        assert job is not None
        self.assertEqual(job.status, "pending")
        self.clock.advance(6)
        self.execute(run)
        self.assertEqual(len(self.model.requests), 1)
        self.assertEqual(self.get_run(run).json()["status"], "waiting_confirmation")
        self.assertEqual(self.effect.calls, [])

    def test_sse_cancellation_after_read_discards_batch_without_cursor_jump(self) -> None:
        from collections.abc import Callable

        from brainoid_api.routes.run_events import Batch
        from brainoid_contracts.transactions import Transaction

        _, run = self.setup_run()
        self.execute(run)
        original = self.store.run

        def racing(work: Callable[[Transaction], Any]) -> Any:
            batch = original(work)
            if isinstance(batch, Batch) and batch.event is not None:
                self.conversations.cancel_run(
                    self.identity(), self.tenant, run, "checkpoint-sse-cancel"
                )
            return batch

        with patch.object(self.store, "run", side_effect=racing):
            batch = next_batch(self.conversations, self.identity(), self.tenant, run, None, 0)
        self.assertIsNone(batch.event)
        self.assertTrue(batch.terminal)

    def test_handler_created_proposal_waits_then_consumes_external_result_once(self) -> None:
        from fakes.run_confirmation import ProposalCreatingEffect

        _, run = self.setup_run()
        self.authority.waiting = False
        self.effect = ProposalCreatingEffect(self.jobs)
        self.restart()
        self.execute(run)
        self.assertEqual(self.get_run(run).json()["status"], "waiting_confirmation")
        proposal = self.proposal(run)
        saved = next(d for p, d in self.store.data.items() if "/tool_executions/" in p)
        self.assertEqual(saved["authority"]["confirmation"], "not_required")
        self.assertEqual(len(self.effect.calls), 1)
        with self.assertRaises(PolicyDenied):
            self.decision(run, proposal)
        self.assertEqual(self.get_run(run).json()["status"], "waiting_confirmation")
        result = ToolResult(
            tool_call_id=saved["tool_call_id"],
            operation_id=saved["effect_id"],
            status="succeeded",
            result={"business_operation": "complete"},
        )
        self.external_result(run, proposal, result=result)
        self.external_result(run, proposal, result=result)
        self.restart()
        delivery = self.delivery(run)
        self.worker.handle(LOCAL_AUTHORIZATION, delivery.model_dump(mode="json"))
        self.worker.handle(LOCAL_AUTHORIZATION, delivery.model_dump(mode="json"))
        self.assertEqual(self.get_run(run).json()["status"], "completed")
        self.assertEqual(len(self.effect.calls), 1)
        self.assertEqual(len(self.model.requests), 2)
        business = self.store.data[f"tenants/{self.tenant}/fixture_business_proposals/{proposal}"]
        self.assertEqual(business["created_count"], 1)

    def external_fixture(self) -> tuple[ResourceId, ResourceId, ResourceId, ToolResult]:
        from fakes.run_confirmation import ProposalCreatingEffect

        conversation, run = self.setup_run()
        self.authority.waiting = False
        self.effect = ProposalCreatingEffect(self.jobs)
        self.restart()
        self.execute(run)
        proposal = self.proposal(run)
        saved = self.store.data[f"tenants/{self.tenant}/run_proposals/{proposal}"]
        tool = self.store.data[f"tenants/{self.tenant}/tool_executions/{saved['tool_call_id']}"]
        result = ToolResult(
            tool_call_id=tool["tool_call_id"],
            operation_id=tool["effect_id"],
            status="succeeded",
            result={"external": "PRIVATE_EXTERNAL_FIXTURE"},
        )
        return conversation, run, proposal, result

    def test_external_claim_outbox_and_late_success_are_durable_and_once_only(self) -> None:
        conversation, run, proposal, result = self.external_fixture()
        self.store.retry_once = True
        claim = self.start_external(run, proposal)
        self.assertEqual(self.start_external(run, proposal), claim)
        self.assertEqual(len([p for p in self.store.data if "/fixture_external_jobs/" in p]), 1)
        job = self.jobs.get(self.tenant, job_id(self.tenant, KIND, run))
        assert job is not None
        self.assertEqual(job.status, "waiting")
        self.assertIsNone(job.lease_expires_at)
        self.clock.advance(301)
        self.confirmations.expire(self.tenant, cast(JobStore, self.store))
        self.assertIsNone(self.jobs.prepare_dispatch(self.tenant, job.id))
        self.restart()
        self.assertEqual(self.confirmations.receive_external(claim, result), "queued")
        self.assertEqual(self.confirmations.receive_external(claim, result), "queued")
        self.execute(run)
        self.assertEqual(self.get_run(run).json()["status"], "completed")
        self.assertEqual(len(self.effect.calls), 1)
        self.assertEqual(len(self.model.requests), 2)
        messages = self.conversations.messages(self.identity(), self.tenant, conversation).items
        self.assertNotIn("操作は実行しませんでした", messages[-1].text)
        self.assertEqual(
            self.store.data[f"tenants/{self.tenant}/tool_executions/{claim.tool_call_id}"][
                "result"
            ],
            result.model_dump(mode="json"),
        )

    def test_external_unknown_is_terminal_immutable_and_never_reexecuted(self) -> None:
        conversation, run, proposal, result = self.external_fixture()
        claim = self.start_external(run, proposal)
        unknown = result.model_copy(update={"status": "outcome_unknown", "result": {}})
        self.assertEqual(self.confirmations.receive_external(claim, unknown), "queued")
        self.assertEqual(self.confirmations.receive_external(claim, unknown), "queued")
        import asyncio
        from unittest.mock import AsyncMock

        from brainoid_agent_core.ports import CallControl
        from brainoid_agent_core.types import ToolExecution
        from brainoid_tools import Executor
        from brainoid_worker.run_turn import SystemClock
        from fakes.runtime import CancellationToken

        request = ToolExecution.model_validate(
            self.store.data[f"tenants/{self.tenant}/run_proposals/{proposal}"]["request"]
        )
        with patch.object(self.effect, "reconcile", new_callable=AsyncMock) as reconcile:
            replay = asyncio.run(
                Executor(self.store, self.effect.registry, self.authority, SystemClock()).reconcile(
                    request, control=CallControl(CancellationToken(), None)
                )
            )
            self.assertEqual(replay, unknown)
            reconcile.assert_not_called()
        for replacement in (
            result,
            result.model_copy(update={"status": "failed", "error_code": "unavailable"}),
        ):
            with self.assertRaises(PolicyDenied):
                self.confirmations.receive_external(claim, replacement)
        self.clock.advance(301)
        self.confirmations.expire(self.tenant, cast(JobStore, self.store))
        self.restart()
        self.execute(run)
        public = self.get_run(run).json()
        self.assertEqual(public["status"], "failed")
        self.assertEqual(public["error_code"], "outcome_unknown")
        self.assertEqual(len(self.model.requests), 1)
        self.assertEqual(len(self.effect.calls), 1)
        messages = self.conversations.messages(self.identity(), self.tenant, conversation).items
        self.assertFalse(any("操作は実行しませんでした" in m.text for m in messages))
        self.assertEqual(
            self.store.data[f"tenants/{self.tenant}/tool_executions/{claim.tool_call_id}"][
                "result"
            ]["status"],
            "outcome_unknown",
        )

    def test_external_cancel_before_claim_prevents_start_and_after_claim_preserves_fact(
        self,
    ) -> None:
        for before in (True, False):
            with self.subTest(cancel_before_claim=before):
                _, run, proposal, result = self.external_fixture()
                if before:
                    self.cancel(run)
                    with self.assertRaises(PolicyDenied):
                        self.start_external(run, proposal)
                else:
                    claim = self.start_external(run, proposal)
                    self.cancel(run)
                    self.assertEqual(self.confirmations.receive_external(claim, result), "recorded")
                    self.assertEqual(self.confirmations.receive_external(claim, result), "recorded")
                    saved = self.store.data[
                        f"tenants/{self.tenant}/tool_executions/{claim.tool_call_id}"
                    ]
                    self.assertEqual(saved["result"]["status"], "succeeded")
                    self.assertEqual(saved["result"]["result"], {})
                    self.assertTrue(saved["result_redacted"])
                cancelled = next(
                    d
                    for p, d in self.store.data.items()
                    if f"/runs/{run}/events/" in p and d["type"] == "run.cancelled"
                )
                self.assertEqual(
                    cancelled["payload"]["external_action_may_have_completed"], not before
                )
                self.assertEqual(self.get_run(run).json()["status"], "cancelled")
                self.assertIsNone(
                    self.jobs.prepare_dispatch(self.tenant, job_id(self.tenant, KIND, run))
                )
                self.assertEqual(len(self.model.requests), 1)
                self.assertEqual(len(self.effect.calls), 1)

    def test_external_results_after_source_or_account_revocation_store_no_payload(self) -> None:
        for change in ("deleted", "suppress_reuse", "account"):
            with self.subTest(change=change):
                _, run, proposal, result = self.external_fixture()
                claim = self.start_external(run, proposal)
                value: str | bool
                if change == "account":
                    changed = next(
                        p
                        for p, d in self.store.data.items()
                        if p.startswith("users/") and d.get("user_id") == self.identity().user_id
                    )
                    field, value = "status", "deleting"
                else:
                    note = json.loads(cast(ConfirmationModel, self.model).call.arguments_json)[
                        "note_id"
                    ]
                    changed = f"tenants/{self.tenant}/notes/{note}"
                    field, value = change, True
                previous = copy.deepcopy(self.store.data[changed])
                self.store.data[changed][field] = value
                self.assertEqual(self.confirmations.receive_external(claim, result), "recorded")
                tool = self.store.data[
                    f"tenants/{self.tenant}/tool_executions/{claim.tool_call_id}"
                ]
                proposal_doc = self.store.data[f"tenants/{self.tenant}/run_proposals/{proposal}"]
                self.assertEqual(tool["result"]["status"], "succeeded")
                self.assertEqual(tool["result"]["result"], {})
                self.assertEqual(proposal_doc["supplied_result"]["result"], {})
                job = self.jobs.get(self.tenant, job_id(self.tenant, KIND, run))
                assert job is not None
                self.assertEqual(job.status, "obsolete")
                self.assertEqual(len(self.model.requests), 1)
                self.store.data[changed] = previous

    def test_external_result_after_private_session_expiry_is_recorded_without_payload(self) -> None:
        from brainoid_api.services.private_sessions import PrivateSessionCreate
        from brainoid_contracts import DisplayPolicy
        from fakes.run_confirmation import ProposalCreatingEffect

        self.store.data[f"tenants/{self.tenant}/agents/{self.agent_id}"]["allowed_tools"] = [
            "notes.delete"
        ]
        credentials = self.credentials()
        self.store.data[f"tenants/{self.tenant}/devices/{credentials.device_id}"][
            "capabilities"
        ] = ["display"]
        device = self.repo.authenticate(credentials.access_token)
        session = self.sessions.start(
            self.identity(),
            self.tenant,
            device.sub,
            PrivateSessionCreate(
                acknowledge_shared_space=True, allowed_scopes=["conversation", "notes"]
            ),
            "external-private-session",
            "fixture",
        )
        conversation = self.conversation(private=True, display=DisplayPolicy.AUTHENTICATED_SESSION)
        response = self.client.post(
            self.url + f"/{conversation}/turns",
            json={
                "text": "Fixture external operation",
                "output_mode": "text",
                "output_device_id": device.sub,
            },
            headers={"Idempotency-Key": "external-private-turn"},
        )
        self.assertEqual(response.status_code, 202, response.text)
        run = ResourceId(response.json()["run_id"])
        self.configure(run)
        self.authority.waiting = False
        self.effect = ProposalCreatingEffect(self.jobs)
        self.restart()
        self.execute(run)
        claim = self.start_external(run, self.proposal(run))
        self.store.data[f"tenants/{self.tenant}/private_sessions/{session.id}"]["status"] = "ended"
        result = ToolResult(
            tool_call_id=claim.tool_call_id,
            operation_id=claim.effect_id,
            status="succeeded",
            result={"text": "PRIVATE_EXPIRED_SESSION_RESULT"},
        )
        self.assertEqual(self.confirmations.receive_external(claim, result), "recorded")
        saved = self.store.data[f"tenants/{self.tenant}/tool_executions/{claim.tool_call_id}"]
        self.assertEqual(saved["result"]["status"], "succeeded")
        self.assertEqual(saved["result"]["result"], {})
        self.assertIsNone(self.jobs.prepare_dispatch(self.tenant, job_id(self.tenant, KIND, run)))
        self.assertEqual(len(self.model.requests), 1)

    def test_external_start_rechecks_target_and_rolls_back_outbox_on_denial(self) -> None:
        from brainoid_tools import ToolRejected

        _, run, proposal, _ = self.external_fixture()
        note = json.loads(cast(ConfirmationModel, self.model).call.arguments_json)["note_id"]
        self.store.data[f"tenants/{self.tenant}/notes/{note}"]["deleted"] = True
        with self.assertRaises((PolicyDenied, ToolRejected)):
            self.start_external(run, proposal)
        self.assertEqual(
            self.store.data[f"tenants/{self.tenant}/run_proposals/{proposal}"]["status"], "pending"
        )
        self.assertFalse(any("/fixture_external_jobs/" in p for p in self.store.data))

    def test_external_receipt_infrastructure_failure_preserves_started_for_retry(self) -> None:
        from brainoid_jobs import AuthorizationUnavailable
        from brainoid_persistence.firestore import PersistenceUnavailable

        _, run, proposal, result = self.external_fixture()
        claim = self.start_external(run, proposal)
        for error in (PolicyUnavailable(), PersistenceUnavailable()):
            with self.subTest(error=type(error).__name__):
                with patch.object(self.handler.state, "check", side_effect=error):
                    with self.assertRaises((AuthorizationUnavailable, PersistenceUnavailable)):
                        self.confirmations.receive_external(claim, result)
                self.assertEqual(
                    self.store.data[f"tenants/{self.tenant}/run_proposals/{proposal}"]["status"],
                    "external_started",
                )
                self.assertIsNone(
                    self.store.data[f"tenants/{self.tenant}/tool_executions/{claim.tool_call_id}"][
                        "result"
                    ]
                )
        self.assertEqual(self.confirmations.receive_external(claim, result), "queued")

    def test_external_result_requires_prior_claim_and_exact_binding(self) -> None:
        from uuid import uuid4

        _, run, proposal, result = self.external_fixture()
        with self.assertRaises(PolicyDenied):
            self.decision(run, proposal, result=result)
        claim = self.start_external(run, proposal)
        with self.assertRaises(PolicyDenied):
            self.confirmations.receive_external(
                claim.model_copy(update={"id": ResourceId(str(uuid4()))}), result
            )
        with self.assertRaises(PolicyDenied):
            self.confirmations.receive_external(
                claim, result.model_copy(update={"operation_id": ResourceId(str(uuid4()))})
            )
        self.assertEqual(self.confirmations.receive_external(claim, result), "queued")

    def test_unclaimed_external_proposal_expires_with_unexecuted_evidence(self) -> None:
        conversation, run, proposal, _ = self.external_fixture()
        self.clock.advance(301)
        with self.assertRaises(PolicyDenied):
            self.start_external(run, proposal)
        self.confirmations.expire(self.tenant, cast(JobStore, self.store))
        self.execute(run)
        text = (
            self.conversations.messages(self.identity(), self.tenant, conversation).items[-1].text
        )
        self.assertIn("操作は実行しませんでした", text)
        self.assertFalse(any("/fixture_external_jobs/" in p for p in self.store.data))

    def two_stages(self) -> tuple[ResourceId, ResourceId, str]:
        import copy
        from uuid import uuid4

        from fakes.run_confirmation import MultiStageModel

        conversation, run = self.setup_run()
        first_call = cast(ConfirmationModel, self.model).call
        first = json.loads(first_call.arguments_json)["note_id"]
        second = ResourceId(str(uuid4()))
        path = f"tenants/{self.tenant}/notes/"
        self.store.data[path + second] = {
            **copy.deepcopy(self.store.data[path + first]),
            "id": second,
        }
        second_call = first_call.model_copy(
            update={
                "call_key": "second",
                "arguments_json": json.dumps({"note_id": second, "expected_version": 1}),
            }
        )
        self.model = MultiStageModel((first_call, second_call))  # type: ignore[assignment]
        self.restart()
        self.execute(run)
        self.decision(run, self.proposal(run))
        self.execute(run)
        self.assertEqual(len(self.model.requests), 2)
        self.assertEqual(len(self.effect.calls), 1)
        return conversation, run, path + first

    def test_every_consumed_result_is_revalidated_after_another_confirmation(self) -> None:
        for change in (
            {"deleted": True},
            {"suppress_reuse": True},
            {
                "visibility": "private",
                "owner_user_id": "demo-a-member",
                "display_policy": "authenticated_session",
            },
        ):
            with self.subTest(change=change):
                _, run, first = self.two_stages()
                before = copy.deepcopy(self.store.data[first])
                self.store.data[first].update(change)
                self.decision(run, self.proposal(run))
                self.restart()
                self.execute(run)
                self.assertEqual(self.get_run(run).json()["status"], "failed")
                self.assertEqual(len(self.model.requests), 2)
                self.assertEqual(len(self.effect.calls), 1)
                self.store.data[first] = before

    def test_normal_multiple_stages_keep_signatures_and_never_repeat_effects(self) -> None:
        conversation, run, _ = self.two_stages()
        self.decision(run, self.proposal(run))
        self.restart()
        delivery = self.delivery(run)
        for _ in range(2):
            self.worker.handle(LOCAL_AUTHORIZATION, delivery.model_dump(mode="json"))
        self.assertEqual(self.get_run(run).json()["status"], "completed")
        self.assertEqual(len(self.effect.calls), 2)
        self.assertEqual(len(self.model.requests), 3)
        self.assertEqual(
            [m.provider_state for m in self.model.requests[-1].messages if m.provider_state],
            [{"signature": "PRIVATE_STAGE_0"}, {"signature": "PRIVATE_STAGE_1"}],
        )
        following = ResourceId(
            self.turn(conversation, key="multiple-stage-next-turn").json()["run_id"]
        )
        self.execute(following)
        self.assertEqual(self.get_run(following).json()["status"], "completed")
        self.assertEqual(len(self.effect.calls), 2)

    def test_next_turn_rejects_deleted_result_source_and_a_different_private_reader(self) -> None:
        for different_reader in (False, True):
            with self.subTest(different_reader=different_reader):
                self.uid = "demo-a-owner"
                conversation, run = self.setup_run()
                source = json.loads(cast(ConfirmationModel, self.model).call.arguments_json)[
                    "note_id"
                ]
                source_path = f"tenants/{self.tenant}/notes/{source}"
                note = self.store.data[source_path]
                before = copy.deepcopy(note)
                self.authority.waiting = False
                self.execute(run)
                self.assertEqual(self.get_run(run).json()["status"], "completed")
                self.assertEqual(len(self.model.requests), 2)
                if different_reader:
                    self.store.data[source_path].update(
                        visibility="private",
                        owner_user_id=self.uid,
                        display_policy="authenticated_session",
                    )
                    self.uid = "demo-a-member"
                else:
                    self.store.data[source_path]["deleted"] = True
                following = ResourceId(
                    self.turn(conversation, key="result-reuse-next-turn").json()["run_id"]
                )
                self.execute(following)
                self.assertEqual(self.get_run(following).json()["status"], "failed")
                self.assertEqual(len(self.model.requests), 2)
                self.assertEqual(len(self.effect.calls), 1)
                self.store.data[source_path] = before

    def test_next_turn_checks_the_current_output_scope_for_previous_tool_results(self) -> None:
        conversation, run = self.setup_run()
        self.authority.waiting = False
        self.execute(run)
        credentials = self.credentials()
        self.store.data[f"tenants/{self.tenant}/devices/{credentials.device_id}"].update(
            capabilities=["display"], allowed_scopes=["shared", "conversation"]
        )
        response = self.client.post(
            self.url + f"/{conversation}/turns",
            json={
                "text": "Fixture next output",
                "output_mode": "text",
                "output_device_id": credentials.device_id,
            },
            headers={"Idempotency-Key": "result-reuse-output-turn"},
        )
        self.assertEqual(response.status_code, 202, response.text)
        following = ResourceId(response.json()["run_id"])
        self.execute(following)
        self.assertEqual(self.get_run(following).json()["status"], "failed")
        self.assertEqual(len(self.model.requests), 2)

    def test_result_binding_mutation_and_missing_reuse_hook_fail_closed(self) -> None:
        for missing_hook in (False, True):
            with self.subTest(missing_hook=missing_hook):
                _, run, _ = self.two_stages()
                self.decision(run, self.proposal(run))
                if missing_hook:

                    class ExecutionOnly:
                        def check(inner_self: Any, *args: Any, **kwargs: Any) -> Any:
                            return self.authority.check(*args, **kwargs)

                    self.handler = RunTurn(
                        self.jobs,
                        self.model,
                        credential_epoch=self.tokens.epoch,
                        tool_registry=self.effect.registry,
                        tool_authorizer=ExecutionOnly(),
                    )
                    self.worker = Worker(self.jobs, LocalCaller(), {KIND: self.handler})
                else:
                    record = self.store.data[f"tenants/{self.tenant}/runs/{run}"]
                    record["tool_bindings"][0]["result"]["result"] = {"tampered": True}
                self.execute(run)
                self.assertEqual(self.get_run(run).json()["status"], "failed")
                self.assertEqual(len(self.model.requests), 2)
                self.assertEqual(len(self.effect.calls), 1)

    def test_expiry_of_inactive_account_continues_to_the_next_proposal(self) -> None:
        pending = []
        for uid in ("demo-a-owner", "demo-a-admin"):
            self.uid = uid
            _, run = self.setup_run()
            self.execute(run)
            pending.append((self.proposal(run), run, uid))
        pending.sort()
        first, healthy = pending
        account = next(
            d
            for p, d in self.store.data.items()
            if p.startswith("users/") and d.get("user_id") == first[2]
        )
        account["status"] = "deleting"
        self.clock.advance(301)
        self.confirmations.expire(self.tenant, cast(JobStore, self.store))
        expired_job = self.jobs.get(self.tenant, job_id(self.tenant, KIND, first[1]))
        healthy_job = self.jobs.get(self.tenant, job_id(self.tenant, KIND, healthy[1]))
        assert expired_job is not None and healthy_job is not None
        self.assertEqual(expired_job.status, "obsolete")
        self.assertEqual(healthy_job.status, "pending")
        for proposal, _, _ in pending:
            self.assertEqual(
                self.store.data[f"tenants/{self.tenant}/run_proposals/{proposal}"]["status"],
                "expired",
            )
        self.uid = healthy[2]
        self.configure(healthy[1])
        self.execute(healthy[1])
        self.assertEqual(self.get_run(healthy[1]).json()["status"], "completed")
        self.assertEqual(self.get_run(first[1]).json()["status"], "failed")

    def test_expiry_of_inactive_output_account_is_not_an_infrastructure_failure(self) -> None:
        _, run = self.setup_run()
        self.execute(run)
        saved = self.jobs.get(self.tenant, job_id(self.tenant, KIND, run))
        assert saved is not None and isinstance(saved.output, UserSubject)
        # The policy permits user output only to the same currently identified user.
        account = next(
            d
            for p, d in self.store.data.items()
            if p.startswith("users/") and d.get("user_id") == saved.output.user_id
        )
        account["status"] = "deleting"
        self.clock.advance(301)
        self.confirmations.expire(self.tenant, cast(JobStore, self.store))
        saved = self.jobs.get(self.tenant, job_id(self.tenant, KIND, run))
        assert saved is not None
        self.assertEqual(saved.status, "obsolete")
        self.uid = "demo-a-admin"
        self.assertEqual(self.get_run(run).json()["status"], "failed")

    def test_account_read_outage_keeps_expiry_retryable(self) -> None:
        from brainoid_persistence.firestore import PersistenceUnavailable

        _, run = self.setup_run()
        self.execute(run)
        proposal = self.proposal(run)
        self.clock.advance(301)
        with patch.object(self.jobs, "_accounts", side_effect=PersistenceUnavailable()):
            with self.assertRaises(PersistenceUnavailable):
                self.confirmations.expire(self.tenant, cast(JobStore, self.store))
        saved = self.jobs.get(self.tenant, job_id(self.tenant, KIND, run))
        assert saved is not None
        self.assertEqual(saved.status, "waiting")
        self.assertEqual(
            self.store.data[f"tenants/{self.tenant}/run_proposals/{proposal}"]["status"], "pending"
        )
        self.confirmations.expire(self.tenant, cast(JobStore, self.store))
        self.execute(run)
        self.assertEqual(self.get_run(run).json()["status"], "completed")

    def test_waiting_keeps_slot_and_resume_checks_next_model_stage_quota(self) -> None:
        _, run = self.setup_run()
        self.execute(run)
        slot = f"tenants/{self.tenant}/usage_slots/{scope_key('tenant')}"
        self.assertEqual(self.store.data[slot]["active"], 1)
        proposal = self.proposal(run)
        self.assertEqual(self.decision(run, proposal), "confirmed")
        policy = Policy().model_dump()
        policy["tenant"]["tokens"] = 263168
        self.store.data[f"tenants/{self.tenant}/usage_policy/current"] = policy
        self.execute(run)
        self.assertEqual(len(self.model.requests), 1)
        self.assertEqual(self.get_run(run).json()["error_code"], "rate_limited")
        self.assertEqual(self.store.data[slot]["active"], 0)
        self.assertEqual(len([p for p in self.store.data if "/usage_reservations/" in p]), 1)
