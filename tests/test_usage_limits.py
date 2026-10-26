"""T-026: atomic quota races, body-free authorized usage and uncertain consumption."""

import copy
import json
from collections.abc import AsyncGenerator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

from brainoid_agent_core.core import RunBudget
from brainoid_agent_core.ports import CallControl
from brainoid_agent_core.types import Completed, Failed, ModelEvent, ModelRequest, TextDelta, Usage
from brainoid_api.errors import APIError
from brainoid_contracts import FirebaseUid, ResourceId
from brainoid_contracts.transactions import Transaction
from brainoid_jobs.models import job_id
from brainoid_runs.models import TurnCreate
from brainoid_runs.state import KIND
from brainoid_usage import (
    Amounts,
    Policy,
    QuotaExceeded,
    Settlement,
    UsageLedger,
    bucket_path,
    scope_key,
)
from brainoid_usage.ledger import reservation_path
from brainoid_worker.entry import Worker
from brainoid_worker.local import LOCAL_AUTHORIZATION, LocalCaller
from brainoid_worker.run_turn import RunTurn
from brainoid_worker.usage_reconciler import UsageReconciler
from fakes.usage_model import UsageModel
from test_jobs import JobStore
from test_turn_run import TurnRunFixture


class ReportingModel:
    def __init__(self, *, failed: bool = False, missing: bool = False) -> None:
        self.calls = 0
        self.failed, self.missing = failed, missing

    async def stream(
        self, request: ModelRequest, *, control: CallControl
    ) -> AsyncGenerator[ModelEvent]:
        self.calls += 1
        if self.failed:
            yield Failed(code="rate_limited", retryable=True)
            return
        yield TextDelta(text="Fixture answer")
        yield Usage(input_tokens=10, output_tokens=None if self.missing else 5)
        yield Completed()


class UsageTests(TurnRunFixture):
    def policy(self, **changes: Any) -> None:
        doc = Policy().model_dump()
        for name, value in changes.items():
            doc[name] = {**doc[name], **value} if isinstance(value, dict) else value
        self.store.data[f"tenants/{self.tenant}/usage_policy/current"] = doc

    def bucket(self, kind: str = "tenant", identity: str = "") -> dict[str, Any]:
        return self.store.data.get(
            bucket_path(self.tenant, self.clock.now().strftime("%Y-%m"), scope_key(kind, identity)),
            {},
        )

    def active(self, kind: str = "tenant", identity: str = "") -> int:
        return int(
            self.store.data.get(
                f"tenants/{self.tenant}/usage_slots/{scope_key(kind, identity)}", {}
            ).get("active", 0)
        )

    def meter(self, model: ReportingModel) -> None:
        self.handler = RunTurn(self.jobs, model, credential_epoch=self.tokens.epoch)
        self.worker = Worker(self.jobs, LocalCaller(), {KIND: self.handler})

    def test_racing_admission_is_atomic_and_retry_key_does_not_charge_twice(self) -> None:
        conversations = [self.conversation(), self.conversation()]
        self.policy(tenant={"concurrent_runs": 1})

        def accept(conversation: ResourceId) -> int:
            try:
                self.conversations.turn(
                    self.identity(),
                    self.tenant,
                    conversation,
                    TurnCreate(text="PRIVATE_BODY", output_mode="text"),
                    "race-key-" + conversation,
                )
                return 202
            except APIError as error:
                self.assertEqual(error.code, "rate_limited")
                return 429

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(accept, conversations)), [202, 429])
        self.assertEqual(self.bucket()["turns"], 1)
        self.assertEqual(self.active(), 1)
        denied = next(
            c
            for c in conversations
            if self.store.data[f"tenants/{self.tenant}/conversations/{c}"]["active_run_id"] is None
        )
        before = copy.deepcopy(self.store.data)
        response = self.turn(denied)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["Retry-After"], "60")
        self.assertTrue(response.json()["error"]["retryable"])
        self.assertEqual(self.store.data, before)
        accepted = next(c for c in conversations if c != denied)
        replay = self.conversations.turn(
            self.identity(),
            self.tenant,
            accepted,
            TurnCreate(text="PRIVATE_BODY", output_mode="text"),
            "race-key-" + accepted,
        )
        self.assertEqual(self.bucket()["turns"], 1)
        self.execute(replay.run_id)
        self.assertEqual(self.active(), 0)
        self.assertEqual(self.turn(denied).status_code, 202)
        ledger = {p: d for p, d in self.store.data.items() if "/usage_" in p}
        self.assertNotIn("PRIVATE_BODY", json.dumps(ledger))
        self.assertNotIn("PRIVATE_SIGNATURE", json.dumps(ledger))

    def test_known_unknown_and_provider_429_do_not_retry_or_refund_unknown(self) -> None:
        for failed, missing, expected in [
            (False, False, 15),
            (False, True, 1034),
            (True, False, 263168),
        ]:
            with self.subTest(failed=failed, missing=missing):
                model = ReportingModel(failed=failed, missing=missing)
                self.meter(model)
                run = ResourceId(self.turn(self.conversation(), key=str(uuid4())).json()["run_id"])
                before = self.bucket().get("tokens", 0)
                delivery = self.delivery(run)
                self.worker.handle(LOCAL_AUTHORIZATION, delivery.model_dump(mode="json"))
                self.worker.handle(LOCAL_AUTHORIZATION, delivery.model_dump(mode="json"))
                self.assertEqual(model.calls, 1)
                self.assertEqual(self.bucket()["tokens"] - before, expected)
                self.assertEqual(self.active(), 0)
                if failed:
                    self.assertEqual(self.get_run(run).json()["error_code"], "rate_limited")

    def test_provider_quota_checks_current_policy_and_never_calls_on_rejection(self) -> None:
        model = ReportingModel()
        self.meter(model)
        run = ResourceId(self.turn(self.conversation()).json()["run_id"])
        self.policy(tenant={"tokens": 5})
        self.execute(run)
        self.assertEqual(model.calls, 0)
        self.assertEqual(self.get_run(run).json()["error_code"], "rate_limited")
        self.assertEqual(self.active(), 0)

    def test_audio_unknown_late_fact_overrun_and_settlement_races(self) -> None:
        run = ResourceId(self.turn(self.conversation()).json()["run_id"])
        ledger = self.conversations.usage
        self.store.run(
            lambda tx: ledger.reserve(tx, self.tenant, run, "speech.0", Amounts(voice_seconds=10))
        )
        ledger.settle(self.tenant, run, "speech.0", Settlement())
        self.assertEqual(self.bucket()["voice_seconds"], 10)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(
                pool.map(
                    lambda _: ledger.settle(
                        self.tenant, run, "speech.0", Settlement(voice_seconds=13)
                    ),
                    range(4),
                )
            )
        self.assertEqual(self.bucket()["voice_seconds"], 13)
        self.policy(tenant={"voice_seconds": 13})
        with self.assertRaises(QuotaExceeded):
            self.store.run(
                lambda tx: ledger.reserve(
                    tx, self.tenant, run, "speech.1", Amounts(voice_seconds=1)
                )
            )
        with self.assertRaises(ValueError):
            ledger.settle(self.tenant, run, "speech.0", Settlement(voice_seconds=0))

    def test_crash_expiry_cancel_and_late_facts_release_exactly_once(self) -> None:
        run = ResourceId(self.turn(self.conversation()).json()["run_id"])
        lease = self.jobs.acquire(self.delivery(run), known_handler=lambda _: True)
        assert lease is not None
        self.jobs.start_effect(lease, "model")
        ledger = self.conversations.usage
        self.store.run(
            lambda tx: ledger.reserve(
                tx, self.tenant, run, "model", Amounts(input_tokens=100, output_tokens=20)
            )
        )
        self.conversations.cancel_run(self.identity(), self.tenant, run, "fixture-cancel-key")
        reconciler = UsageReconciler(self.jobs)
        reconciler.reconcile(self.tenant, run)
        self.assertEqual(self.active(), 1)  # cancellation cannot prove the provider stopped
        self.clock.advance(1801)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: reconciler.reconcile(self.tenant, run), range(4)))
        self.assertEqual(self.active(), 0)
        self.assertEqual(self.bucket()["tokens"], 120)
        self.assertEqual(
            self.store.data[reservation_path(self.tenant, run, "model")]["state"], "unknown"
        )
        ledger.settle(self.tenant, run, "model", Settlement(input_tokens=7, output_tokens=3))
        reconciler.reconcile(self.tenant, run)
        self.assertEqual(self.bucket()["tokens"], 10)
        self.assertEqual(self.active(), 0)
        self.assertIsNone(self.jobs.prepare_dispatch(self.tenant, job_id(self.tenant, KIND, run)))

    def test_queued_expiry_fences_job_without_provider_or_turn_refund(self) -> None:
        run = ResourceId(self.turn(self.conversation()).json()["run_id"])
        self.clock.advance(1801)
        reconciler = UsageReconciler(self.jobs)
        self.assertIsNone(reconciler.recover(self.tenant, cast(JobStore, self.store)))
        self.assertEqual(self.active(), 0)
        self.assertEqual(self.bucket()["turns"], 1)
        self.assertEqual(self.get_run(run).json()["status"], "cancelled")
        self.assertEqual(len(self.model.requests), 0)
        self.assertIsNone(self.jobs.prepare_dispatch(self.tenant, job_id(self.tenant, KIND, run)))

    def test_member_admin_other_tenant_and_selector_authorization(self) -> None:
        run = ResourceId(self.turn(self.conversation()).json()["run_id"])
        self.execute(run)
        url = f"/v1/tenants/{self.tenant}/usage"
        self.assertEqual(self.client.get(url).json()["turns"], 1)
        self.uid = "demo-a-member"
        own = self.client.get(url)
        self.assertEqual(own.status_code, 200, own.text)
        self.assertEqual(own.json()["turns"], 0)
        self.assertEqual(
            set(own.json()), {"period", "turns", "voice_seconds", "remaining_turns", "quota_state"}
        )
        self.assertEqual(self.client.get(url + "?user_id=demo-a-owner").status_code, 400)
        self.assertEqual(self.client.get(f"/v1/tenants/{self.other}/usage").status_code, 404)
        self.uid = "demo-a-admin"
        self.assertEqual(self.client.get(url).json()["turns"], 1)

    def test_opaque_uid_device_user_limits_and_month_rollover(self) -> None:
        ledger = self.conversations.usage
        tenant = self.tenant
        user = FirebaseUid("opaque/user:α")
        device = ResourceId(str(uuid4()))
        self.policy(user={"concurrent_runs": 1}, device={"turns": 1})
        run = ResourceId(str(uuid4()))

        def admit(tx: Transaction) -> None:
            ledger.prepare_admit(tx, tenant, run, user=user, device=device)()

        self.store.run(admit)
        self.assertEqual(self.active("user", user), 1)
        self.assertEqual(self.bucket("device", device)["turns"], 1)
        old_period = self.clock.now().strftime("%Y-%m")
        future = datetime(2027, 1, 1, tzinfo=UTC)
        later = UsageLedger(self.store, now=lambda: future)
        with self.assertRaises(QuotaExceeded):
            self.store.run(
                lambda tx: later.prepare_admit(
                    tx, tenant, ResourceId(str(uuid4())), user=user, device=None
                )
            )
        # Exact reservation month survives later settlement; UIDs never form path segments.
        self.store.run(
            lambda tx: ledger.reserve(tx, tenant, run, "speech", Amounts(voice_seconds=10))
        )
        later.settle(tenant, run, "speech", Settlement(voice_seconds=4))
        self.assertEqual(
            self.store.data[bucket_path(tenant, old_period, scope_key("user", user))][
                "voice_seconds"
            ],
            4,
        )
        self.assertFalse(any("opaque/user" in path for path in self.store.data))

    def test_provider_reservation_uses_new_month_but_preserves_admission_month(self) -> None:
        edge = datetime(2026, 10, 31, 23, 59, tzinfo=UTC)
        self.clock.advance((edge - self.clock.now()).total_seconds())
        ledger = self.conversations.usage
        run = ResourceId(str(uuid4()))
        self.store.run(
            lambda tx: ledger.prepare_admit(
                tx, self.tenant, run, user=FirebaseUid("member"), device=None
            )()
        )
        self.clock.advance(120)
        self.store.run(
            lambda tx: ledger.reserve(tx, self.tenant, run, "speech", Amounts(voice_seconds=10))
        )
        ledger.settle(self.tenant, run, "speech", Settlement(voice_seconds=3))
        self.assertEqual(self.bucket()["turns"], 0)
        self.assertEqual(self.bucket()["voice_seconds"], 3)
        self.assertEqual(
            self.store.data[bucket_path(self.tenant, "2026-10", scope_key("tenant"))]["turns"], 1
        )
        self.assertEqual(self.active(), 1)

    def test_observed_length_overrun_is_an_unknown_lower_bound_until_finalized(self) -> None:
        self.policy(model_input_tokens=4096)
        model = UsageModel(
            (TextDelta(text="OK"), Usage(input_tokens=4096, output_tokens=20), Completed())
        )
        self.handler = RunTurn(
            self.jobs,
            model,
            credential_epoch=self.tokens.epoch,
            budget=RunBudget(max_output_tokens=10),
        )
        self.worker = Worker(self.jobs, LocalCaller(), {KIND: self.handler})
        run = ResourceId(self.turn(self.conversation()).json()["run_id"])
        self.execute(run)
        path = reservation_path(self.tenant, run, "model")
        saved = self.store.data[path]
        self.assertEqual(model.calls, 1)
        self.assertEqual(saved["state"], "unknown")
        self.assertEqual(
            saved["observed"], {"input_tokens": 4096, "output_tokens": 20, "voice_seconds": 0}
        )
        self.assertFalse(saved["known"]["output_tokens"])
        self.assertEqual(self.bucket()["tokens"], 4116)
        self.assertEqual(self.active(), 0)
        self.policy(tenant={"tokens": 4116})
        self.assertEqual(self.turn(self.conversation(), key=str(uuid4())).status_code, 429)
        self.conversations.usage.settle(
            self.tenant, run, "model", Settlement(input_tokens=4096, output_tokens=22)
        )
        self.assertEqual(self.bucket()["tokens"], 4118)
        self.assertEqual(self.store.data[path]["state"], "settled")
        self.assertEqual(self.active(), 0)

    def test_unprocessed_usage_survives_core_event_limit_revocation_and_cancel(self) -> None:
        self.policy(model_input_tokens=4096)
        original_agent = copy.deepcopy(
            self.store.data[f"tenants/{self.tenant}/agents/{self.agent_id}"]
        )
        for mode in ("event_limit", "revocation", "cancel"):
            with self.subTest(mode=mode):
                conversation = self.conversation()
                run = ResourceId(self.turn(conversation, key=str(uuid4())).json()["run_id"])

                def before(event: ModelEvent, mode: str = mode, run: ResourceId = run) -> None:
                    if isinstance(event, Usage):
                        if mode == "revocation":
                            self.store.data[f"tenants/{self.tenant}/agents/{self.agent_id}"][
                                "version"
                            ] += 1
                        elif mode == "cancel":
                            self.conversations.cancel_run(
                                self.identity(), self.tenant, run, str(uuid4())
                            )

                model = UsageModel(
                    (TextDelta(text="OK"), Usage(input_tokens=5000, output_tokens=5), Completed()),
                    before_event=before,
                )
                self.handler = RunTurn(
                    self.jobs,
                    model,
                    credential_epoch=self.tokens.epoch,
                    budget=RunBudget(
                        max_output_tokens=10, max_model_events=1 if mode == "event_limit" else 100
                    ),
                )
                self.worker = Worker(self.jobs, LocalCaller(), {KIND: self.handler})
                self.execute(run)
                saved = self.store.data[reservation_path(self.tenant, run, "model")]
                # Core checks authority/event count before processing Usage. The
                # adapter's observation is still a charge, never output authority.
                self.assertEqual(saved["charged"]["input_tokens"], 5000)
                self.assertEqual(saved["charged"]["output_tokens"], 10)
                self.assertEqual(saved["state"], "unknown")
                self.assertEqual(saved["observed"]["output_tokens"], 5)
                if mode != "event_limit":
                    messages = [
                        doc
                        for path, doc in self.store.data.items()
                        if path.startswith(
                            f"tenants/{self.tenant}/conversations/{conversation}/messages/"
                        )
                    ]
                    self.assertEqual([doc["role"] for doc in messages], ["user"])
                self.store.data[f"tenants/{self.tenant}/agents/{self.agent_id}"] = copy.deepcopy(
                    original_agent
                )

    def test_partial_observations_do_not_refund_or_finish_an_active_provider(self) -> None:
        run = ResourceId(self.turn(self.conversation()).json()["run_id"])
        ledger = self.conversations.usage
        self.store.run(
            lambda tx: ledger.reserve(
                tx, self.tenant, run, "speech", Amounts(input_tokens=100, voice_seconds=30)
            )
        )
        for observed in (
            Amounts(input_tokens=20, voice_seconds=40),
            Amounts(input_tokens=10, voice_seconds=35),
            Amounts(input_tokens=20, voice_seconds=40),
        ):
            ledger.observe(self.tenant, run, "speech", observed)
        saved = self.store.data[reservation_path(self.tenant, run, "speech")]
        self.assertEqual(saved["state"], "started")
        self.assertEqual(saved["observed"]["voice_seconds"], 40)
        self.assertEqual(self.bucket()["tokens"], 100)
        self.assertEqual(self.bucket()["voice_seconds"], 40)
        self.conversations.cancel_run(self.identity(), self.tenant, run, str(uuid4()))
        self.assertEqual(self.active(), 1)
        ledger.settle(self.tenant, run, "speech", Settlement(input_tokens=25))
        self.assertEqual(self.bucket()["tokens"], 25)
        self.assertEqual(self.bucket()["voice_seconds"], 40)
        self.assertEqual(
            self.store.data[reservation_path(self.tenant, run, "speech")]["state"], "unknown"
        )

    def test_observation_final_and_expiry_races_preserve_all_scope_totals(self) -> None:
        run = ResourceId(self.turn(self.conversation()).json()["run_id"])
        ledger = self.conversations.usage
        self.store.run(
            lambda tx: ledger.reserve(
                tx,
                self.tenant,
                run,
                "mixed",
                Amounts(input_tokens=100, output_tokens=10, voice_seconds=30),
            )
        )
        # Compatibility with reservations written before the lower-bound field.
        self.store.data[reservation_path(self.tenant, run, "mixed")].pop("observed")
        ledger.observe(
            self.tenant, run, "mixed", Amounts(input_tokens=40, output_tokens=20, voice_seconds=40)
        )
        self.clock.advance(1801)
        reconciler = UsageReconciler(self.jobs)

        def work(index: int) -> None:
            if index % 3 == 0:
                ledger.observe(
                    self.tenant,
                    run,
                    "mixed",
                    Amounts(input_tokens=50, output_tokens=25, voice_seconds=45),
                )
            elif index % 3 == 1:
                ledger.settle(
                    self.tenant,
                    run,
                    "mixed",
                    Settlement(input_tokens=60, output_tokens=30, voice_seconds=50),
                )
            else:
                reconciler.reconcile(self.tenant, run)

        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(work, range(18)))
        saved = self.store.data[reservation_path(self.tenant, run, "mixed")]
        self.assertEqual(saved["state"], "settled")
        self.assertEqual(
            saved["observed"], {"input_tokens": 50, "output_tokens": 25, "voice_seconds": 45}
        )
        for kind, identity in (("tenant", ""), ("user", self.uid)):
            self.assertEqual(self.bucket(kind, identity)["tokens"], 90)
            self.assertEqual(self.bucket(kind, identity)["voice_seconds"], 50)
            self.assertEqual(self.active(kind, identity), 0)
        ledger.observe(self.tenant, run, "mixed", Amounts(input_tokens=1, voice_seconds=1))
        self.assertEqual(self.store.data[reservation_path(self.tenant, run, "mixed")], saved)

    def test_rejected_final_preserves_higher_observation_and_later_reconciliation(self) -> None:
        run = ResourceId(self.turn(self.conversation()).json()["run_id"])
        ledger = self.conversations.usage
        self.store.run(
            lambda tx: ledger.reserve(tx, self.tenant, run, "speech", Amounts(voice_seconds=10))
        )
        with self.assertRaises(ValueError):
            ledger.settle(
                self.tenant,
                run,
                "speech",
                Settlement(voice_seconds=15),
                observed=Amounts(voice_seconds=20),
            )
        saved = self.store.data[reservation_path(self.tenant, run, "speech")]
        self.assertEqual(saved["observed"]["voice_seconds"], 20)
        self.assertEqual(self.bucket()["voice_seconds"], 20)
        self.assertFalse(saved["known"]["voice_seconds"])
        ledger.settle(self.tenant, run, "speech", Settlement(voice_seconds=22))
        self.assertEqual(self.bucket()["voice_seconds"], 22)
        # Contradictory later provider evidence cannot leave the earlier smaller
        # final fact as an undercharge. It requires an adequate new final total.
        ledger.observe(self.tenant, run, "speech", Amounts(voice_seconds=25))
        self.assertEqual(self.bucket()["voice_seconds"], 25)
        self.assertEqual(
            self.store.data[reservation_path(self.tenant, run, "speech")]["state"], "unknown"
        )
        with self.assertRaises(ValueError):
            ledger.settle(self.tenant, run, "speech", Settlement(voice_seconds=22))
        ledger.settle(self.tenant, run, "speech", Settlement(voice_seconds=27))
        self.assertEqual(self.bucket()["voice_seconds"], 27)
