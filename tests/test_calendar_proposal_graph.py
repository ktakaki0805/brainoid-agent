"""Bounded graph authorization, fresh conditions, retry and retained deadlines."""

import copy
import unittest
from functools import partial
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import test_calendar_proposal_tool as tool_fixtures
import test_calendar_proposals as fixtures
from brainoid_contracts import FirebaseUid, ResourceId
from brainoid_contracts.access import fingerprint
from brainoid_contracts.guards import GuardedTransactions
from brainoid_memory import calendar_proposal_access as access
from brainoid_memory.calendar_access import check_calendar_sources
from brainoid_memory.calendar_proposals import immutable, proposal_path
from brainoid_memory.sources import CanonicalSources
from brainoid_policy.models import UserSubject
from brainoid_policy.scopes import PolicyDenied
from brainoid_runs.memory_sources import SavedMessages


class ProposalGraphBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.CalendarProposalTests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.propose()
        self.seed = next(
            v for p, v in self.fixture.store.data.items() if "/calendar_proposals/" in p
        )

    def graph(self, size: int) -> list[dict[str, Any]]:
        # Synthetic, internally hash-consistent DB corruption/budget fixtures. The
        # multi-generation acceptance test uses only actual producer records.
        records: list[dict[str, Any]] = []
        for _ in range(size):
            record = copy.deepcopy(self.seed)
            identity = ResourceId(str(uuid4()))
            record["id"] = identity
            record["calendar_source"]["proposal_id"] = identity
            record["origin"]["calendar_sources"] = (
                [] if not records else [records[-1]["calendar_source"]]
            )
            records.append(record)
        self.save(records)
        return records

    def save(self, records: list[dict[str, Any]]) -> None:
        for record in records:
            record["args_hash"] = fingerprint(immutable(record))
            self.fixture.store.data[proposal_path(self.fixture.tenant, record["id"])] = (
                copy.deepcopy(record)
            )

    def check(self, tx: Any, record: dict[str, Any]) -> None:
        f = self.fixture
        access.check_origin(
            tx,
            f.proposals.policy(tx),
            record,
            now=f.clock.now,
            credential_epoch=None,
            sources=CanonicalSources(SavedMessages()),
        )

    def test_iterative_node_limit_accepts_boundary_then_rejects_excess(self) -> None:
        records = self.graph(access.MAX_PROPOSALS + 1)
        self.fixture.store.run(lambda tx: self.check(tx, records[-2]))
        with self.assertRaises(PolicyDenied):
            self.fixture.store.run(partial(self.check, record=records[-1]))
        self.assertEqual(len(records[-1]["origin"]["calendar_sources"]), 1)

    def test_cycles_fail_closed_even_when_each_record_hash_is_valid(self) -> None:
        for size in (1, 2):
            with self.subTest(size=size):
                records = self.graph(size)
                records[0]["origin"]["calendar_sources"] = [records[-1]["calendar_source"]]
                self.save(records)
                with self.assertRaises(PolicyDenied):
                    self.fixture.store.run(partial(self.check, record=records[-1]))

    def test_edge_limit_counts_duplicate_sources_and_does_not_drop_them(self) -> None:
        f = self.fixture
        source = self.seed["calendar_source"]

        def check(tx: Any, size: int) -> None:
            policy = f.proposals.policy(tx)
            principal = policy.principal(f.tenant, UserSubject(user_id=f.identity().user_id))
            check_calendar_sources(
                tx,
                policy,
                principal,
                {"calendar_sources": [source] * size},
                sources=CanonicalSources(SavedMessages()),
            )

        f.store.run(lambda tx: check(tx, access.MAX_SOURCE_EDGES))
        with self.assertRaises(PolicyDenied):
            f.store.run(lambda tx: check(tx, access.MAX_SOURCE_EDGES + 1))

    def test_reader_conditions_are_fresh_in_same_transaction(self) -> None:
        f = self.fixture

        def work(tx: Any) -> None:
            policy = f.proposals.policy(tx)
            document = {"calendar_sources": [self.seed["calendar_source"]] * 2}
            owner = policy.principal(f.tenant, UserSubject(user_id=f.identity().user_id))
            other = policy.principal(f.tenant, UserSubject(user_id=FirebaseUid("demo-a-owner")))
            check_calendar_sources(
                tx, policy, owner, document, sources=CanonicalSources(SavedMessages())
            )
            with self.assertRaises(PolicyDenied):
                check_calendar_sources(
                    tx, policy, other, document, sources=CanonicalSources(SavedMessages())
                )

        f.store.run(work)

    def test_transaction_retry_revalidates_a_new_snapshot(self) -> None:
        f = self.fixture

        def suppress() -> None:
            f.store.data[proposal_path(f.tenant, self.seed["id"])]["suppress_reuse"] = True

        f.store.retry = suppress
        attempts: list[bool] = []

        def work(tx: Any) -> None:
            attempts.append(True)
            self.check(tx, tx.get(proposal_path(f.tenant, self.seed["id"])))

        with self.assertRaises(PolicyDenied):
            f.store.run(work)
        self.assertEqual(len(attempts), 2)


class ProposalGraphDeadlineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = tool_fixtures.CalendarProposalToolTests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        f.source_fact(expires=True)
        run = f.source_run()
        proposal = f.proposal(run)
        self.record = f.store.data[proposal_path(f.tenant, proposal)]
        self.document = {"calendar_sources": [self.record["calendar_source"]] * 2}

    def check(self, tx: Any, *, for_model: bool) -> None:
        f = self.fixture
        policy = f.proposals.policy(tx)
        check_calendar_sources(
            tx,
            policy,
            policy.principal(f.tenant, UserSubject(user_id=f.identity().user_id)),
            self.document,
            credential_epoch=f.tokens.epoch,
            sources=CanonicalSources(SavedMessages()),
            for_model=for_model,
        )

    def test_preview_pass_cannot_satisfy_model_pass_even_in_same_transaction(self) -> None:
        f = self.fixture
        f.clock.advance(1)

        def work(tx: Any) -> None:
            self.check(tx, for_model=False)
            with self.assertRaises(PolicyDenied):
                self.check(tx, for_model=True)

        f.store.run(work)

    def test_shared_ancestor_guard_survives_pass_until_later_write(self) -> None:
        f = self.fixture
        path = f"tenants/{f.tenant}/fixture_guard/test"
        origin = access._check_origin
        with patch.object(access, "_check_origin", wraps=origin) as visits:

            def work(tx: Any) -> None:
                self.check(tx, for_model=True)
                self.assertEqual(visits.call_count, 1)
                f.clock.advance(1)
                tx.set(path, {"persisted": True})

            with self.assertRaises(PolicyDenied):
                GuardedTransactions(f.store).run(work)
        self.assertNotIn(path, f.store.data)

    def test_shared_ancestor_guard_survives_delayed_response(self) -> None:
        f = self.fixture
        run = f.store.run

        def delayed(work: Any) -> Any:
            result = run(work)
            f.clock.advance(1)
            return result

        with patch.object(f.store, "run", side_effect=delayed), self.assertRaises(PolicyDenied):
            GuardedTransactions(f.store).run(lambda tx: self.check(tx, for_model=True))
