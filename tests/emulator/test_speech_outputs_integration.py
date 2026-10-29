"""VOICE-02: replica contention, durable attempt bounds, expiry and quota recovery."""

import asyncio
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import timedelta
from threading import Barrier, Event, local
from typing import Any
from unittest.mock import patch
from uuid import uuid4

from brainoid_api.errors import APIError, ErrorCode
from brainoid_api.speech_outputs import SpeechOutputs
from brainoid_contracts import ResourceId
from brainoid_runs.models import TurnCreate
from brainoid_runs.speech import speech_path
from brainoid_usage import Policy, bucket_path, scope_key
from brainoid_usage.ledger import reservation_path
from brainoid_worker.usage_reconciler import UsageReconciler
from test_audio_inputs import connected, wav
from test_speech_outputs import FakeSynthesizer
from test_turn_run_integration import TurnRunIntegrationFixture


class SpeechIntegrationTests(TurnRunIntegrationFixture):
    def setUp(self) -> None:
        super().setUp()
        self.synth = FakeSynthesizer()
        self.speech = SpeechOutputs(self.conversations, self.synth)
        self.conversations.speech_enabled = True
        self.model.text = "第一文。第二文。"
        policy = Policy().model_dump()
        policy["run_seconds"] = 60
        self.db.document(f"tenants/{self.tenant}/usage_policy/current").set(policy)
        run = self.conversations.turn(
            self.identity(),
            self.tenant,
            self.conversation(),
            TurnCreate(text="Synthetic question", output_mode="speech"),
            str(uuid4()),
        ).run_id
        self.execute(run)
        refs = sorted(
            self.persistence.page(f"tenants/{self.tenant}/speech_outputs"),
            key=lambda r: r[1]["sentence_index"],
        )
        self.assertEqual(len(refs), 2)
        self.first, self.second = (ResourceId(ref[1]["id"]) for ref in refs)
        self.slot = f"tenants/{self.tenant}/usage_slots/{scope_key('tenant')}"
        self.bucket = bucket_path(self.tenant, self.now.strftime("%Y-%m"), scope_key("tenant"))

    def fetch(self, speech: ResourceId, service: SpeechOutputs | None = None) -> bytes:
        return asyncio.run(
            (service or self.speech).synthesize(
                self.identity(), self.tenant, None, speech, connected
            )
        ).audio

    def test_replicas_admit_one_inflight_call_then_allow_only_one_retry(self) -> None:
        started, release = Event(), Event()

        async def delayed() -> None:
            started.set()
            while not release.is_set():
                await asyncio.sleep(0.01)

        self.synth.hook = delayed
        replica = SpeechOutputs(self.conversations, self.synth)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.fetch, self.first)
            try:
                self.assertTrue(started.wait(10))
                with self.assertRaises(APIError) as concurrent:
                    self.fetch(self.first, replica)
                self.assertEqual(concurrent.exception.code, ErrorCode.CONFLICT)
                self.assertEqual(len(self.synth.calls), 1)
            finally:
                release.set()
            self.assertEqual(first.result(timeout=15), wav())
        self.assertEqual(self.fetch(self.first, replica), wav())
        with self.assertRaises(APIError) as bounded:
            self.fetch(self.first)
        self.assertEqual(bounded.exception.code, ErrorCode.RATE_LIMITED)
        self.assertEqual(len(self.synth.calls), 2)
        self.assertEqual(self.db.document(self.slot).get().to_dict(), {"active": 0})
        self.assertEqual((self.db.document(self.bucket).get().to_dict() or {})["voice_seconds"], 2)

    def test_attempt_deadline_during_settlement_does_not_refund_consumed_audio(self) -> None:
        settle = self.speech.usage.settle

        def delayed(*args: Any, **kwargs: Any) -> None:
            settle(*args, **kwargs)
            self.now += timedelta(seconds=60)

        with patch.object(self.speech.usage, "settle", side_effect=delayed):
            with self.assertRaises(APIError) as expired:
                self.fetch(self.first)
        self.assertEqual(expired.exception.code, ErrorCode.RESOURCE_EXPIRED)
        self.assertEqual(len(self.synth.calls), 1)
        self.assertEqual(self.db.document(self.slot).get().to_dict(), {"active": 0})
        self.assertEqual((self.db.document(self.bucket).get().to_dict() or {})["voice_seconds"], 1)

    def test_completed_parent_deadline_does_not_expire_live_speech_reference(self) -> None:
        self.now += timedelta(seconds=60)
        self.assertEqual(self.fetch(self.first), wav())
        self.assertEqual(self.db.document(self.slot).get().to_dict(), {"active": 0})

    def test_two_admissions_read_same_reference_and_transaction_retry_fences_loser(self) -> None:
        barrier, attempt = Barrier(2), local()
        release = Event()
        original = self.speech.usage.prepare_admit

        def synchronize(*args: object, **kwargs: object) -> object:
            result = original(*args, **kwargs)  # type: ignore[arg-type]
            if not getattr(attempt, "waited", False):
                attempt.waited = True
                barrier.wait(timeout=10)
            return result

        async def delayed() -> None:
            while not release.is_set():
                await asyncio.sleep(0.01)

        self.synth.hook = delayed

        def fetch() -> int:
            try:
                self.fetch(self.first, SpeechOutputs(self.conversations, self.synth))
                return 200
            except APIError as error:
                self.assertEqual(error.code, ErrorCode.CONFLICT)
                return 409

        with patch.object(self.speech.usage, "prepare_admit", side_effect=synchronize):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(fetch), pool.submit(fetch)]
                try:
                    done, _ = wait(futures, timeout=15, return_when=FIRST_COMPLETED)
                    self.assertEqual(len(done), 1)
                    self.assertEqual(next(iter(done)).result(), 409)
                finally:
                    release.set()
                self.assertEqual(sorted(f.result(timeout=15) for f in futures), [200, 409])
        self.assertEqual(len(self.synth.calls), 1)
        self.assertEqual(self.db.document(self.slot).get().to_dict(), {"active": 0})

    def test_two_sentences_compete_for_the_same_voice_budget(self) -> None:
        policy = Policy().model_dump()
        policy["tenant"]["voice_seconds"] = 60
        self.db.document(f"tenants/{self.tenant}/usage_policy/current").set(policy)
        started, release = Event(), Event()

        async def delayed() -> None:
            started.set()
            while not release.is_set():
                await asyncio.sleep(0.01)

        self.synth.hook = delayed
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.fetch, self.first)
            try:
                self.assertTrue(started.wait(10))
                with self.assertRaises(APIError) as limited:
                    self.fetch(self.second)
                self.assertEqual(limited.exception.code, ErrorCode.RATE_LIMITED)
                self.assertEqual(len(self.synth.calls), 1)
            finally:
                release.set()
            first.result(timeout=15)
        self.assertEqual(self.db.document(self.slot).get().to_dict(), {"active": 0})

    def test_commit_ack_loss_never_calls_provider_and_recovers_unknown_after_sweep(self) -> None:
        original = self.speech.store.run

        def lose(work: object) -> object:
            result: object = original(work)  # type: ignore[arg-type]
            if isinstance(result, tuple) and isinstance(result[0], str):
                raise RuntimeError("synthetic acknowledgement loss")
            return result

        with patch.object(self.speech.store, "run", side_effect=lose):
            with self.assertRaises(RuntimeError):
                self.fetch(self.first)
        doc = self.db.document(speech_path(self.tenant, self.first)).get().to_dict()
        assert doc is not None
        attempt = ResourceId(doc["active_attempt"])
        self.assertEqual(self.synth.calls, [])
        self.now += timedelta(minutes=5)
        self.speech.expire(self.tenant)
        self.assertFalse(self.db.document(speech_path(self.tenant, self.first)).get().exists)
        UsageReconciler(self.jobs).reconcile(self.tenant, attempt)
        UsageReconciler(self.jobs).reconcile(self.tenant, attempt)
        self.assertEqual(self.db.document(self.slot).get().to_dict(), {"active": 0})
        self.assertEqual(
            (
                self.db.document(reservation_path(self.tenant, attempt, "speech")).get().to_dict()
                or {}
            )["state"],
            "unknown",
        )

    def test_recovery_before_late_provider_result_fences_audio(self) -> None:
        async def delayed() -> None:
            doc = self.db.document(speech_path(self.tenant, self.first)).get().to_dict()
            assert doc is not None
            self.now += timedelta(seconds=60)
            UsageReconciler(self.jobs).reconcile(self.tenant, ResourceId(doc["active_attempt"]))

        self.synth.hook = delayed
        with self.assertRaises(APIError):
            self.fetch(self.first)
        self.assertEqual(self.db.document(self.slot).get().to_dict(), {"active": 0})
        self.assertEqual((self.db.document(self.bucket).get().to_dict() or {})["voice_seconds"], 60)
