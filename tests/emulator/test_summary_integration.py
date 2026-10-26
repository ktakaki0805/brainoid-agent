"""MEM-06 real Firestore prefix reuse, source races and atomic checkpoint retry."""

from datetime import timedelta
from typing import Any
from unittest.mock import patch

from brainoid_memory import CanonicalSources, MemoryRepository
from brainoid_memory.summaries import Summary, summary_path
from brainoid_policy.models import UserSubject
from brainoid_runs.models import TurnCreate
from brainoid_runs.state import conversation_path
from brainoid_worker.entry import Worker
from brainoid_worker.local import LOCAL_AUTHORIZATION, LocalCaller
from brainoid_worker.summarize_conversation import KIND, SummarizeConversation
from fakes.memory_repository import AuthenticatedCaller, context
from fakes.turn_model import Model
from google.api_core.exceptions import Aborted
from test_turn_run_integration import TurnRunIntegrationFixture


class SummaryIntegrationTests(TurnRunIntegrationFixture):
    def setUp(self) -> None:
        super().setUp()
        self.conversation_id = self.conversation(private=True)
        self.model.text = "OLD_ASSISTANT_SENTINEL"
        accepted = self.conversations.turn(
            self.identity(),
            self.tenant,
            self.conversation_id,
            TurnCreate(text="ORIGINAL_DIRECT_USER_TEA", output_mode="text"),
            "summary-original-turn",
        )
        self.execute(accepted.run_id)
        caller = AuthenticatedCaller(UserSubject(user_id=self.identity().user_id))
        caller.valid_from = self.now - timedelta(seconds=1)
        caller.valid_to = self.now + timedelta(minutes=10)
        self.memory = MemoryRepository(
            self.jobs.store,
            authorizer=caller,
            sources=CanonicalSources(),
            ledger=self.ledger,
            now=lambda: self.now,
            allow_untracked_bootstrap=True,
        )
        self.summary_model = Model(text="SUMMARY_DIRECT_USER_TEA")
        self.summarizer = SummarizeConversation(
            self.jobs, self.summary_model, revision="emulator-v1"
        )
        self.summary_job = self.summarizer.schedule(
            lambda tx: self.memory.summaries(tx, context(self.tenant, self.identity().user_id)),
            self.conversation_id,
        )
        job = self.jobs.get(self.tenant, self.summary_job)
        assert job is not None
        self.location = summary_path(self.tenant, self.conversation_id, job.operation_id)
        self.message = next(
            snap.reference
            for snap in self.db.collection(
                conversation_path(self.tenant, self.conversation_id) + "/messages"
            ).stream()
            if (snap.to_dict() or {}).get("role") == "user"
        )

    def summarize(self) -> None:
        delivery = self.jobs.prepare_dispatch(self.tenant, self.summary_job, queue_name=KIND)
        assert delivery is not None
        Worker(self.jobs, LocalCaller(), {KIND: self.summarizer}).handle(
            LOCAL_AUTHORIZATION, delivery.model_dump(mode="json")
        )

    def status(self) -> str:
        job = self.jobs.get(self.tenant, self.summary_job)
        assert job is not None
        return job.status

    def test_real_private_summary_and_later_completed_turns_reuse_exact_prefix(self) -> None:
        self.summarize()
        self.assertEqual(self.status(), "succeeded")
        summary = Summary.model_validate(self.db.document(self.location).get().to_dict())
        self.assertEqual(summary.sources.owner_user_id, self.identity().user_id)
        self.assertEqual(summary.sources.scope.visibility, "private")
        self.assertNotIn("OLD_ASSISTANT_SENTINEL", self.summary_model.requests[0].model_dump_json())
        for index in range(2):
            accepted = self.accept(self.conversation_id, f"summary-following-{index}")
            self.execute(accepted.run_id)
            request = self.model.requests[-1].model_dump_json()
            self.assertIn("SUMMARY_DIRECT_USER_TEA", request)
            self.assertNotIn("ORIGINAL_DIRECT_USER_TEA", request)
            self.assertNotIn(self.identity().user_id, request)

    def test_correction_during_generation_discards_summary_and_checkpoint(self) -> None:
        def update() -> None:
            self.message.update({"version": 2, "text": "corrected"})

        self.summary_model.hook = update
        self.summarize()
        self.assertEqual(self.status(), "obsolete")
        self.assertFalse(self.db.document(self.location).get().exists)
        self.assertEqual(len(self.summary_model.requests), 1)

    def test_suppression_during_generation_discards_summary(self) -> None:
        def suppress() -> None:
            self.message.update({"suppress_reuse": True})

        self.summary_model.hook = suppress
        self.summarize()
        self.assertEqual(self.status(), "obsolete")
        self.assertFalse(self.db.document(self.location).get().exists)

    def test_missing_source_after_save_drops_summary_and_derived_raw_history(self) -> None:
        self.summarize()
        self.message.delete()
        accepted = self.accept(self.conversation_id, "summary-after-deletion")
        self.execute(accepted.run_id)
        request = self.model.requests[-1].model_dump_json()
        self.assertNotIn("SUMMARY_DIRECT_USER_TEA", request)
        self.assertNotIn("OLD_ASSISTANT_SENTINEL", request)
        self.assertNotIn("ORIGINAL_DIRECT_USER_TEA", request)
        summary = Summary.model_validate(self.db.document(self.location).get().to_dict())
        self.assertEqual(str(summary.sources.messages[0].resource_id), self.message.id)

    def test_sdk_retry_revalidates_prefix_without_second_model_call(self) -> None:
        commit = self.db._firestore_api.commit
        changed = False

        def abort(*args: Any, **kwargs: Any) -> Any:
            nonlocal changed
            request = kwargs.get("request", {})
            if not changed and self.summary_model.requests and request.get("transaction"):
                changed = True
                self.db._firestore_api.rollback(
                    request={
                        "database": self.db._database_string,
                        "transaction": request["transaction"],
                    }
                )
                self.message.update({"suppress_reuse": True})
                raise Aborted("synthetic summary transaction retry")  # type: ignore[no-untyped-call]
            return commit(*args, **kwargs)

        with patch.object(self.db._firestore_api, "commit", side_effect=abort):
            self.summarize()
        self.assertTrue(changed)
        self.assertEqual(self.status(), "obsolete")
        self.assertFalse(self.db.document(self.location).get().exists)
        self.assertEqual(len(self.summary_model.requests), 1)
