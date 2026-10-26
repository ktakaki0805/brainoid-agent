"""Summary prefix compression through public turn admission and Core model input."""

from datetime import timedelta

from brainoid_contracts import ResourceId
from brainoid_memory import CanonicalSources, MemoryRepository
from brainoid_policy.models import UserSubject
from brainoid_runs.state import conversation_path
from brainoid_worker.entry import Worker
from brainoid_worker.local import LOCAL_AUTHORIZATION, LocalCaller
from brainoid_worker.summarize_conversation import KIND, SummarizeConversation
from fakes.memory_repository import AuthenticatedCaller, context
from fakes.turn_model import Model
from test_turn_run import TurnRunFixture


class SummaryRuntimeTests(TurnRunFixture):
    def summarize(self, conversation: ResourceId) -> None:
        identity = self.identity()
        caller = AuthenticatedCaller(UserSubject(user_id=identity.user_id))
        caller.valid_from = self.clock.now() - timedelta(seconds=1)
        caller.valid_to = self.clock.now() + timedelta(minutes=10)
        repository = MemoryRepository(
            self.jobs.store,
            authorizer=caller,
            sources=CanonicalSources(),
            ledger=self.ledger,
            now=self.clock.now,
            allow_untracked_bootstrap=True,
        )
        model = Model(text="SUMMARY_USER_TEA")
        handler = SummarizeConversation(self.jobs, model, revision="runtime-v1")
        job = handler.schedule(
            lambda tx: repository.summaries(tx, context(self.tenant, identity.user_id)),
            conversation,
        )
        delivery = self.jobs.prepare_dispatch(self.tenant, job, queue_name=KIND)
        assert delivery is not None
        Worker(self.jobs, LocalCaller(), {KIND: handler}).handle(
            LOCAL_AUTHORIZATION, delivery.model_dump(mode="json")
        )
        self.assertEqual(self.jobs.get(self.tenant, job).status, "succeeded")  # type: ignore[union-attr]
        self.assertNotIn("ASSISTANT_OLD_SENTINEL", model.requests[0].model_dump_json())
        self.assertNotIn(identity.user_id, model.requests[0].model_dump_json())

    def test_summary_prefix_used_after_multiple_normal_turns_with_truthful_provenance(self) -> None:
        conversation = self.conversation(private=True)
        self.model.text = "ASSISTANT_OLD_SENTINEL"
        accepted = self.turn(conversation, text="ORIGINAL_USER_LONG_TEXT_TEA")
        self.execute(accepted.json()["run_id"])
        self.summarize(conversation)
        for index in range(2):
            accepted = self.turn(
                conversation, key=f"summary-next-turn-{index}", text=f"next question {index}"
            )
            self.assertEqual(accepted.status_code, 202, accepted.text)
            self.execute(accepted.json()["run_id"])
            request = self.model.requests[-1].model_dump_json()
            self.assertIn("SUMMARY_USER_TEA", request)
            self.assertIn("Summary of direct user statements", request)
            self.assertIn("source_refs", request)
            self.assertNotIn("ORIGINAL_USER_LONG_TEXT_TEA", request)
            self.assertNotIn(self.identity().user_id, request)

    def test_missing_or_suppressed_source_never_returns_via_summary_or_raw_assistant(self) -> None:
        for modification in ("missing", "suppressed", "revised", "missing_body"):
            with self.subTest(modification=modification):
                conversation = self.conversation(private=True)
                self.model.text = "ASSISTANT_OLD_SENTINEL"
                accepted = self.turn(
                    conversation,
                    key="summary-original-" + modification,
                    text="ORIGINAL_USER_LONG_TEXT_TEA",
                )
                self.execute(accepted.json()["run_id"])
                self.summarize(conversation)
                prefix = conversation_path(self.tenant, conversation) + "/messages/"
                original = next(
                    path
                    for path, doc in self.store.data.items()
                    if path.startswith(prefix) and doc.get("role") == "user"
                )
                if modification == "missing":
                    del self.store.data[original]
                elif modification == "suppressed":
                    self.store.data[original]["suppress_reuse"] = True
                elif modification == "missing_body":
                    del self.store.data[original]["text"]
                else:
                    self.store.data[original].update(version=2, text="CORRECTED_USER_TEXT")
                self.model.text = "New answer"
                accepted = self.turn(
                    conversation, key="summary-following-" + modification, text="safe new question"
                )
                self.assertEqual(accepted.status_code, 202, accepted.text)
                count = len(self.model.requests)
                self.execute(accepted.json()["run_id"])
                self.assertEqual(len(self.model.requests), count + 1)
                request = self.model.requests[-1].model_dump_json()
                self.assertNotIn("SUMMARY_USER_TEA", request)
                self.assertNotIn("ASSISTANT_OLD_SENTINEL", request)
                self.assertNotIn("ORIGINAL_USER_LONG_TEXT_TEA", request)
                if modification == "revised":
                    self.assertIn("CORRECTED_USER_TEXT", request)

    def test_summary_remains_usable_after_confirmation_wait_without_renewing_waiting_lease(
        self,
    ) -> None:
        from brainoid_policy.models import UserSubject
        from brainoid_runs.state import KIND as RUN_KIND
        from brainoid_worker.resume_run import Confirmations
        from brainoid_worker.run_turn import RunTurn
        from fakes.run_confirmation import (
            ConfirmationAuthority,
            ConfirmationEffect,
            ConfirmationModel,
        )

        conversation = self.conversation()
        original = self.turn(conversation, text="original direct user statement")
        self.execute(original.json()["run_id"])
        self.summarize(conversation)
        self.store.data[f"tenants/{self.tenant}/agents/{self.agent_id}"]["allowed_tools"] = [
            "notes.delete"
        ]
        accepted = self.turn(conversation, key="summary-confirmation-turn")
        run = ResourceId(accepted.json()["run_id"])
        note = ResourceId(
            next(
                doc["id"]
                for path, doc in self.store.data.items()
                if path.startswith(f"tenants/{self.tenant}/notes/")
                and doc["visibility"] == "shared"
            )
        )
        model, effect = ConfirmationModel(note), ConfirmationEffect()
        authority = ConfirmationAuthority(self.jobs, run)
        self.handler = RunTurn(
            self.jobs,
            model,
            credential_epoch=self.tokens.epoch,
            tool_registry=effect.registry,
            tool_authorizer=authority,
        )
        self.worker = Worker(self.jobs, LocalCaller(), {RUN_KIND: self.handler})
        self.execute(run)
        saved = self.get_run(run).json()
        self.assertEqual(saved["status"], "waiting_confirmation")
        confirmation = Confirmations(
            self.jobs, self.handler.state, registry=effect.registry, result_authorizer=authority
        )
        self.assertEqual(
            confirmation.decide(
                self.tenant,
                run,
                ResourceId(saved["waiting_proposal_id"]),
                "confirmed",
                subject=UserSubject(user_id=self.identity().user_id),
            ),
            "confirmed",
        )
        self.execute(run)
        self.assertEqual(self.get_run(run).json()["status"], "completed")
        self.assertEqual(len(model.requests), 2)
        self.assertTrue(
            all("SUMMARY_USER_TEA" in request.model_dump_json() for request in model.requests)
        )
