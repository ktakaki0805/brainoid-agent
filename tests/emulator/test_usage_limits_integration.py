"""T-026 against Firestore transactions, including an abruptly exiting worker process."""

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from brainoid_agent_core.core import RunBudget
from brainoid_agent_core.types import Completed, TextDelta, Usage
from brainoid_api.errors import APIError
from brainoid_contracts import ResourceId
from brainoid_jobs.models import job_id
from brainoid_runs.state import KIND
from brainoid_usage import Amounts, Policy, Settlement, bucket_path, scope_key
from brainoid_usage.ledger import reservation_path
from brainoid_worker.entry import Worker
from brainoid_worker.local import LocalCaller
from brainoid_worker.run_turn import RunTurn
from brainoid_worker.usage_reconciler import UsageReconciler
from fakes.usage_model import UsageModel
from test_turn_run_integration import TurnRunIntegrationFixture


class UsageIntegrationTests(TurnRunIntegrationFixture):
    def setUp(self) -> None:
        super().setUp()
        self.slot = f"tenants/{self.tenant}/usage_slots/{scope_key('tenant')}"
        self.bucket = bucket_path(self.tenant, self.now.strftime("%Y-%m"), scope_key("tenant"))

    def test_conflicting_admissions_count_one_and_rejected_transaction_writes_nothing(self) -> None:
        policy = Policy().model_dump()
        policy["tenant"]["concurrent_runs"] = 1
        self.db.document(f"tenants/{self.tenant}/usage_policy/current").set(policy)
        conversations = [self.conversation(), self.conversation()]

        def accept(conversation: ResourceId) -> int:
            try:
                self.accept(conversation, key=str(uuid4()))
                return 202
            except APIError as error:
                self.assertEqual(error.code, "rate_limited")
                return 429

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(accept, conversations)), [202, 429])
        self.assertEqual(self.db.document(self.slot).get().to_dict(), {"active": 1})
        self.assertEqual((self.db.document(self.bucket).get().to_dict() or {})["turns"], 1)
        self.assertEqual(
            len(self.persistence.page(f"tenants/{self.tenant}/runs")) - self.initial_runs, 1
        )
        self.assertEqual(
            sum(
                len(self.persistence.page(f"tenants/{self.tenant}/conversations/{c}/messages"))
                for c in conversations
            ),
            1,
        )

    def test_known_audio_settlement_and_release_are_idempotent_under_contention(self) -> None:
        run = self.accept(self.conversation()).run_id
        ledger = self.conversations.usage
        self.store.run(
            lambda tx: ledger.reserve(tx, self.tenant, run, "speech.0", Amounts(voice_seconds=30))
        )

        def settle(_: int) -> None:
            ledger.settle(self.tenant, run, "speech.0", Settlement(voice_seconds=12))

        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(settle, range(3)))
        self.assertEqual((self.db.document(self.bucket).get().to_dict() or {})["voice_seconds"], 12)
        self.conversations.cancel_run(self.identity(), self.tenant, run, "usage-emulator-cancel")
        reconciler = UsageReconciler(self.jobs)
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(lambda _: reconciler.reconcile(self.tenant, run), range(3)))
        self.assertEqual(self.db.document(self.slot).get().to_dict(), {"active": 0})
        self.assertEqual((self.db.document(self.bucket).get().to_dict() or {})["voice_seconds"], 12)

    def test_process_exit_preserves_started_consumption_and_recovery_fences_reexecution(
        self,
    ) -> None:
        run = self.accept(self.conversation()).run_id
        code = """
import os, sys
from brainoid_contracts import ResourceId
from brainoid_jobs.models import job_id
from brainoid_runs.state import KIND
from brainoid_usage import Amounts, UsageLedger
from test_turn_run_integration import process_services
client, jobs, _ = process_services()
tenant, run = ResourceId(sys.argv[2]), ResourceId(sys.argv[3])
delivery = jobs.prepare_dispatch(tenant, job_id(tenant, KIND, run))
lease = jobs.acquire(delivery, known_handler=lambda _: True)
jobs.start_effect(lease, 'model')
ledger = UsageLedger(jobs.store, now=jobs.now)
jobs.store.run(lambda tx: ledger.reserve(tx, tenant, run, 'model', Amounts(input_tokens=100, output_tokens=20)))
os._exit(0)
"""
        subprocess.run(
            [sys.executable, "-B", "-c", code, self.directory, self.tenant, run],
            env={
                **os.environ,
                "PYTHONPATH": os.pathsep.join(
                    (str(Path("tests").resolve()), str(Path("tests/emulator").resolve()))
                ),
            },
            check=True,
            timeout=30,
        )
        self.now += timedelta(minutes=31)
        self.assertIsNone(self.jobs.prepare_dispatch(self.tenant, job_id(self.tenant, KIND, run)))
        reconciler = UsageReconciler(self.jobs)
        self.assertIsNone(reconciler.recover(self.tenant, self.persistence))
        self.assertEqual(self.db.document(self.slot).get().to_dict(), {"active": 0})
        self.assertEqual((self.db.document(self.bucket).get().to_dict() or {})["tokens"], 120)
        reservation = self.db.document(reservation_path(self.tenant, run, "model")).get().to_dict()
        assert reservation is not None
        self.assertEqual(reservation["state"], "unknown")
        self.assertIsNone(self.jobs.prepare_dispatch(self.tenant, job_id(self.tenant, KIND, run)))
        self.conversations.usage.settle(
            self.tenant, run, "model", Settlement(input_tokens=8, output_tokens=2)
        )
        self.assertEqual((self.db.document(self.bucket).get().to_dict() or {})["tokens"], 10)
        self.assertEqual(self.db.document(self.slot).get().to_dict(), {"active": 0})

    def test_length_stop_persists_provider_observation_before_core_discards_terminal(self) -> None:
        policy = Policy().model_dump()
        policy["model_input_tokens"] = 4096
        self.db.document(f"tenants/{self.tenant}/usage_policy/current").set(policy)
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
        run = self.accept(self.conversation()).run_id
        self.execute(run)
        saved = self.db.document(reservation_path(self.tenant, run, "model")).get().to_dict()
        assert saved is not None
        self.assertEqual(saved["state"], "unknown")
        self.assertEqual(saved["observed"]["output_tokens"], 20)
        self.assertEqual((self.db.document(self.bucket).get().to_dict() or {})["tokens"], 4116)
        self.assertEqual(self.db.document(self.slot).get().to_dict(), {"active": 0})
        self.conversations.usage.settle(
            self.tenant, run, "model", Settlement(input_tokens=4096, output_tokens=22)
        )
        self.assertEqual((self.db.document(self.bucket).get().to_dict() or {})["tokens"], 4118)
        self.assertEqual(model.calls, 1)

    def test_observation_final_and_expiry_transactions_keep_monotonic_lower_bounds(self) -> None:
        run = self.accept(self.conversation()).run_id
        ledger = self.conversations.usage
        self.store.run(
            lambda tx: ledger.reserve(tx, self.tenant, run, "speech", Amounts(voice_seconds=10))
        )
        ledger.observe(self.tenant, run, "speech", Amounts(voice_seconds=20))
        ledger.observe(self.tenant, run, "speech", Amounts(voice_seconds=15))
        self.assertEqual((self.db.document(self.bucket).get().to_dict() or {})["voice_seconds"], 20)
        with self.assertRaises(ValueError):
            ledger.settle(
                self.tenant,
                run,
                "speech",
                Settlement(voice_seconds=24),
                observed=Amounts(voice_seconds=25),
            )
        self.assertEqual((self.db.document(self.bucket).get().to_dict() or {})["voice_seconds"], 25)
        self.now += timedelta(minutes=31)
        reconciler = UsageReconciler(self.jobs)

        def work(index: int) -> None:
            if index % 3 == 0:
                ledger.observe(self.tenant, run, "speech", Amounts(voice_seconds=27))
            elif index % 3 == 1:
                ledger.settle(self.tenant, run, "speech", Settlement(voice_seconds=30))
            else:
                reconciler.reconcile(self.tenant, run)

        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(work, range(6)))
        saved = self.db.document(reservation_path(self.tenant, run, "speech")).get().to_dict()
        assert saved is not None
        self.assertEqual(saved["state"], "settled")
        self.assertEqual(saved["observed"]["voice_seconds"], 27)
        self.assertEqual((self.db.document(self.bucket).get().to_dict() or {})["voice_seconds"], 30)
        self.assertEqual(self.db.document(self.slot).get().to_dict(), {"active": 0})
