"""MEM-06 / T-010 T-011 canonical prefix, suppression and generation fencing."""

import copy
import hashlib
import unittest
from datetime import timedelta
from typing import Any

from brainoid_contracts.reuse import reusable_text
from brainoid_jobs import Jobs
from brainoid_jobs.models import SourceObsolete
from brainoid_memory import CanonicalSources, MemoryRepository
from brainoid_memory.summaries import Summary, summary_path
from brainoid_policy.control_ledger import ControlLedger
from brainoid_policy.scopes import PolicyDenied
from brainoid_runs.state import history
from brainoid_worker.entry import Execution, Worker
from brainoid_worker.local import LOCAL_AUTHORIZATION, LocalCaller
from brainoid_worker.summarize_conversation import KIND, SummarizeConversation
from fakes.authz_ledger import MemoryObjects
from fakes.memory_repository import (
    CONVERSATION,
    NOW,
    SOURCE,
    TENANT,
    USER,
    AuthenticatedCaller,
    GraphStore,
    context,
)
from fakes.summaries import BASE, MESSAGE, summary_data
from fakes.turn_model import Model


class SummaryFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.now = NOW
        self.store = GraphStore()
        self.store.data = summary_data()
        self.ledger = ControlLedger(MemoryObjects())
        self.jobs = Jobs(
            self.store, self.ledger, now=lambda: self.now, allow_untracked_bootstrap=True
        )
        self.model = Model(text="The user likes tea.")
        self.handler = SummarizeConversation(self.jobs, self.model, revision="fixture-v1")
        self.repo = MemoryRepository(
            self.jobs.store,
            authorizer=AuthenticatedCaller(),
            sources=CanonicalSources(),
            ledger=self.ledger,
            now=lambda: self.now,
            allow_untracked_bootstrap=True,
        )
        self.identity = self.handler.schedule(
            lambda tx: self.repo.summaries(tx, context()), CONVERSATION
        )
        job = self.jobs.get(TENANT, self.identity)
        assert job is not None
        self.path = summary_path(TENANT, CONVERSATION, job.operation_id)
        self.worker = Worker(self.jobs, LocalCaller(), {KIND: self.handler})

    def deliver(self) -> None:
        delivery = self.jobs.prepare_dispatch(TENANT, self.identity, queue_name=KIND)
        assert delivery is not None
        self.worker.handle(LOCAL_AUTHORIZATION, delivery.model_dump(mode="json"))

    def state(self) -> Any:
        return self.jobs.get(TENANT, self.identity)

    def latest(self) -> Summary | None:
        return self.jobs.store.run(
            lambda tx: self.repo.summaries(tx, context()).latest(CONVERSATION, before=100)
        )


class SummaryProvenanceTests(SummaryFixture):
    def test_durable_private_summary_inherits_provenance_and_never_extracts_fact(self) -> None:
        self.deliver()
        self.assertEqual(self.state().status, "succeeded")
        record = self.latest()
        assert record is not None
        self.assertEqual(record.sources.owner_user_id, USER)
        self.assertEqual(record.sources.scope.visibility, "private")
        self.assertEqual(record.sources.scope.display_policy, "never")
        self.assertIsNone(record.sources.expires_at)
        self.assertEqual(
            [(m.resource_id, m.version) for m in record.sources.messages if m.included],
            [(SOURCE, 1)],
        )
        request = self.model.requests[0].model_dump_json()
        self.assertIn("DIRECT_USER_SENTINEL", request)
        self.assertNotIn("DERIVED_MEMORY_SENTINEL", request)
        self.assertNotIn(USER, request)
        self.assertNotIn(TENANT, request)
        self.assertNotIn("likes tea", self.state().model_dump_json())
        self.assertFalse(any("/facts/" in p for p in self.store.data))
        self.assertIsNone(self.jobs.prepare_dispatch(TENANT, self.identity))

    def test_shared_creator_owner_and_null_owner_are_both_preserved(self) -> None:
        for owner in (USER, None):
            with self.subTest(owner=owner):
                self.store.data[BASE].update(
                    visibility="shared", display_policy="shared_display", owner_user_id=owner
                )
                self.identity = self.handler.schedule(
                    lambda tx: self.repo.summaries(tx, context()), CONVERSATION
                )
                self.deliver()
                self.assertEqual(self.latest().sources.owner_user_id, owner)  # type: ignore[union-attr]

    def test_source_updates_deletions_suppression_and_authority_races_drop_result(self) -> None:
        baseline = copy.deepcopy(self.store.data)
        for path, changes in (
            (MESSAGE, {"version": 2}),
            (MESSAGE, {"deletion_epoch": 1, "deleted": True}),
            (MESSAGE, {"suppress_reuse": True}),
            (MESSAGE, {"suppressed_spans": [{"start": 0}]}),
            (MESSAGE, {"text": "same-version corruption"}),
            (BASE, {"version": 2}),
            (BASE, {"deletion_epoch": 1}),
            (BASE, {"visibility": "shared", "display_policy": "shared_display"}),
            (BASE, {"owner_user_id": "someone-else"}),
            (f"tenants/{TENANT}/memberships/safe-key", {"authorization_version": 2}),
        ):
            with self.subTest(path=path, changes=changes):
                self.store.data = copy.deepcopy(baseline)

                def mutate(path: str = path, changes: dict[str, Any] = changes) -> None:
                    self.store.data[path].update(changes)

                self.model.hook = mutate
                self.deliver()
                self.assertEqual(self.state().status, "obsolete")
                self.assertNotIn(self.path, self.store.data)

    def test_old_summary_survives_append_but_never_uses_changed_or_missing_source(self) -> None:
        self.deliver()
        record = self.latest()
        assert record is not None
        self.store.data[BASE]["version"] = 3
        self.store.data[BASE + "/messages/00000000-0000-0000-0000-000000000099"] = {
            **self.store.data[MESSAGE],
            "id": "00000000-0000-0000-0000-000000000099",
            "sequence": 3,
        }
        self.assertEqual(self.latest(), record)
        baseline = copy.deepcopy(self.store.data)
        for changes in (
            {"version": 2},
            {"deleted": True},
            {"suppress_reuse": True},
            {"text": None},
            {"deletion_epoch": 1},
        ):
            with self.subTest(changes=changes):
                self.store.data = copy.deepcopy(baseline)
                self.store.data[MESSAGE].update(changes)
                self.assertIsNone(self.latest())
                # Durable provenance remains truthful even without the original body.
                saved = Summary.model_validate(self.store.data[self.path])
                self.assertEqual(saved.sources.messages[0].resource_id, SOURCE)
                self.assertEqual(saved.sources.messages[0].version, 1)

    def test_expired_parent_and_changed_scope_never_release_summary(self) -> None:
        self.deliver()
        baseline = copy.deepcopy(self.store.data)
        for changes in (
            {"expires_at": NOW.isoformat()},
            {"display_policy": "authenticated_session"},
            {"owner_user_id": "other"},
            {"suppress_reuse": True},
        ):
            self.store.data = copy.deepcopy(baseline)
            self.store.data[BASE].update(changes)
            try:
                result = self.latest()
            except PolicyDenied:
                result = None
            self.assertIsNone(result)

    def test_retries_do_not_rebill_and_completed_checkpoint_requires_current_summary(self) -> None:
        delivery = self.jobs.prepare_dispatch(TENANT, self.identity)
        assert delivery is not None
        lease = self.jobs.acquire(delivery, known_handler=lambda kind: True)
        assert lease is not None
        result = self.handler.run(Execution(self.jobs, lease))
        self.assertEqual(self.handler.run(Execution(self.jobs, lease)), result)
        self.assertEqual(len(self.model.requests), 1)
        del self.store.data[self.path]
        with self.assertRaises(SourceObsolete):
            self.jobs.complete(lease, result, self.handler.commit)

    def test_expired_lease_or_unknown_provider_never_reexecutes(self) -> None:
        self.model.hook = lambda: setattr(self, "now", self.now + timedelta(minutes=2))
        self.deliver()
        self.assertNotIn(self.path, self.store.data)
        self.assertIsNone(self.jobs.prepare_dispatch(TENANT, self.identity))
        self.assertEqual(len(self.model.requests), 1)

    def test_commit_retry_rechecks_source_with_one_provider_call(self) -> None:
        def arm() -> None:
            self.store.retry = lambda: self.store.data[MESSAGE].update(suppress_reuse=True)

        self.model.close_hook = arm
        self.deliver()
        self.assertNotIn(self.path, self.store.data)
        self.assertEqual(self.state().status, "obsolete")
        self.assertEqual(len(self.model.requests), 1)

    def test_deleted_source_before_dispatch_never_sends_text(self) -> None:
        del self.store.data[MESSAGE]
        self.deliver()
        self.assertEqual(self.state().status, "obsolete")
        self.assertEqual(self.model.requests, [])

    def test_derived_user_metadata_and_roles_are_not_summary_sources(self) -> None:
        changes: dict[str, Any]
        for changes in (
            {"memory_records": []},
            {"tool_bindings": []},
            {"role": "tool"},
            {"role": "assistant"},
            {"source_refs": [{"kind": "note"}]},
        ):
            with self.subTest(changes=changes):
                self.store.data[MESSAGE] = {**summary_data()[MESSAGE], **changes}
                with self.assertRaises(PolicyDenied):
                    self.handler.schedule(
                        lambda tx: self.repo.summaries(tx, context()), CONVERSATION
                    )

    def test_valid_span_redaction_is_shared_by_history_and_summary(self) -> None:
        text = "safe SECRET safe"
        self.store.data[MESSAGE].update(
            text=text,
            suppressed_spans=[
                {
                    "start": 5,
                    "end": 11,
                    "version": 1,
                    "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                }
            ],
        )
        self.identity = self.handler.schedule(
            lambda tx: self.repo.summaries(tx, context()), CONVERSATION
        )
        self.deliver()
        request = self.model.requests[0].model_dump_json()
        self.assertIn("safe [omitted] safe", request)
        self.assertNotIn("SECRET", request)
        docs, messages, _ = self.jobs.store.run(lambda tx: history(tx, TENANT, CONVERSATION))
        self.assertEqual([d["id"] for d in docs], [SOURCE])
        self.assertNotIn("SECRET", str(messages))
        self.assertNotIn("DERIVED_MEMORY_SENTINEL", str(messages))
        for span in ({"start": 0}, {"start": -1, "end": 11, "version": 1, "text_sha256": "bad"}):
            self.store.data[MESSAGE]["suppressed_spans"] = [span]
            self.assertIsNone(reusable_text(self.store.data[MESSAGE]))
            docs, _, _ = self.jobs.store.run(lambda tx: history(tx, TENANT, CONVERSATION))
            self.assertEqual(docs, [])

    def test_original_authorization_deadline_is_not_renewed_by_background_lease(self) -> None:
        self.now = NOW + timedelta(hours=1)
        self.deliver()
        self.assertEqual(self.state().status, "obsolete")
        self.assertEqual(self.model.requests, [])
        self.assertNotIn(self.path, self.store.data)

    def test_provider_tool_requests_and_partial_answers_are_not_saved(self) -> None:
        baseline = copy.deepcopy(self.store.data)
        for reason in ("tool", "length", "content_filter", "failure"):
            with self.subTest(reason=reason):
                self.store.data = copy.deepcopy(baseline)
                self.model.tool = reason == "tool"
                self.model.failure = reason == "failure"
                self.model.finish_reason = (
                    reason if reason in ("length", "content_filter") else "stop"
                )
                self.deliver()
                self.assertEqual(self.state().reason, "outcome_unknown")
                self.assertNotIn(self.path, self.store.data)
                self.assertIsNone(self.jobs.prepare_dispatch(TENANT, self.identity))

    def test_parent_expiry_is_inherited_and_stale_candidate_does_not_poison_new_summary(
        self,
    ) -> None:
        first_expiry = NOW + timedelta(seconds=10)
        second_expiry = NOW + timedelta(seconds=20)
        self.store.data[BASE]["expires_at"] = first_expiry.isoformat()
        self.identity = self.handler.schedule(
            lambda tx: self.repo.summaries(tx, context()), CONVERSATION
        )
        self.deliver()
        self.assertEqual(self.latest().sources.expires_at, first_expiry)  # type: ignore[union-attr]
        self.store.data[BASE].update(version=2, expires_at=second_expiry.isoformat())
        self.identity = self.handler.schedule(
            lambda tx: self.repo.summaries(tx, context()), CONVERSATION
        )
        self.deliver()
        self.now += timedelta(seconds=11)
        self.assertEqual(self.latest().sources.expires_at, second_expiry)  # type: ignore[union-attr]
        self.now += timedelta(seconds=10)
        self.assertIsNone(self.latest())
