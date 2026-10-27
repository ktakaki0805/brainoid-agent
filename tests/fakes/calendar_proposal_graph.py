"""Real same-conversation proposal generations, one scenario per backend."""

import json
from collections.abc import Callable
from functools import partial
from typing import Any
from unittest.mock import patch
from uuid import uuid4

from brainoid_agent_core.types import ToolCall
from brainoid_contracts import ResourceId
from brainoid_contracts.errors import APIError
from brainoid_contracts.transactions import Document, Transaction
from brainoid_memory import calendar_proposal_access as access
from brainoid_memory.calendar_proposals import proposal_path
from brainoid_runs.models import TurnCreate
from test_memory_revalidation import MatchedClock

from fakes.run_confirmation import MultiStageModel


def _model(fixture: Any, model: MultiStageModel) -> None:
    fixture.model = model


def _load(tx: Transaction, *, tenant: ResourceId, identity: ResourceId) -> Document:
    record = tx.get(proposal_path(tenant, identity))
    assert record is not None
    return record


def _counted(counter: dict[str, int], field: str, call: Callable[..., Any]) -> Callable[..., Any]:
    def invoke(*args: Any, **kwargs: Any) -> Any:
        counter[field] += 1
        return call(*args, **kwargs)

    return invoke


class ProposalGraphScenarios:
    def test_real_generations_deduplicate_ancestors_and_recheck_revocation(self: Any) -> None:
        parent = self.conversation(private=True)
        rows: list[dict[str, int]] = []
        first = None
        transaction = self.store.run(type)
        get = transaction.get
        origin = access._check_origin
        for generation in range(1, getattr(self, "graph_generations", 6) + 1):
            _model(
                self,
                MultiStageModel(
                    (
                        ToolCall(
                            call_key="proposal",
                            name="calendar.propose",
                            arguments_json=json.dumps(self.arguments),
                        ),
                    )
                ),
            )
            self.restart()
            self.handler.clock = MatchedClock(self.jobs.now)
            run = self.conversations.turn(
                self.identity(),
                self.tenant,
                parent,
                TurnCreate(text="Prepare an event", output_mode="text"),
                str(uuid4()),
            ).run_id
            self.execute(run)
            public = self.conversations.get_run(self.identity(), self.tenant, run)
            self.assertEqual(public.status, "waiting_confirmation")
            proposal = public.waiting_proposal_id
            assert proposal is not None
            first = first or proposal
            saved = self.store.run(partial(_load, tenant=self.tenant, identity=proposal))
            self.assertEqual(len(saved["origin"]["calendar_sources"]), generation - 1)
            counts = {"generation": generation, "origin_visits": 0, "database_gets": 0}

            with (
                patch.object(access, "_check_origin", _counted(counts, "origin_visits", origin)),
                patch.object(transaction, "get", _counted(counts, "database_gets", get)),
            ):
                self.assertEqual(
                    self.proposals.get(self.identity(), self.tenant, proposal).status, "pending"
                )
            self.assertEqual(counts["origin_visits"], generation)
            rows.append(counts)
            # Budget is deliberately tolerant of extra current-authority reads, but
            # rejects the old doubling sequence (6 generations: 32 visits/2,839 gets).
            self.assertLessEqual(
                counts["database_gets"], rows[0]["database_gets"] * (2 * generation - 1)
            )
            print(json.dumps({"calendar_proposal_graph": counts}), flush=True)
            self.proposals.reject(self.identity(), self.tenant, proposal, str(uuid4()))
            self.execute(run)
            self.assertEqual(
                self.conversations.get_run(self.identity(), self.tenant, run).status, "completed"
            )
        assert first is not None
        first_path = proposal_path(self.tenant, first)

        def suppress(tx: Any) -> None:
            tx.set(first_path, {**tx.get(first_path), "suppress_reuse": True})

        self.store.run(suppress)
        with self.assertRaises(APIError):
            self.proposals.get(self.identity(), self.tenant, proposal)
