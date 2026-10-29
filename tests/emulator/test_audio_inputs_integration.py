"""VOICE-01: real Firestore transaction contention and STT expiry/recovery."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from unittest.mock import patch
from uuid import uuid4

from brainoid_api.audio_inputs import AudioInputs
from brainoid_api.errors import APIError, ErrorCode
from brainoid_contracts import ResourceId
from brainoid_contracts.deletion import lifecycle_account_path
from brainoid_runs.models import TurnCreate
from brainoid_usage import Policy, bucket_path, scope_key
from brainoid_usage.ledger import reservation_path
from brainoid_worker.usage_reconciler import UsageReconciler
from test_audio_inputs import FakeRecognizer, connected, wav
from test_turn_run_integration import TurnRunIntegrationFixture


class AudioIntegrationTests(TurnRunIntegrationFixture):
    def setUp(self) -> None:
        super().setUp()
        self.speech = FakeRecognizer()
        self.audio = AudioInputs(self.conversations, self.speech)
        self.conversations.audio_inputs = self.audio
        self.slot = f"tenants/{self.tenant}/usage_slots/{scope_key('tenant')}"
        self.bucket = bucket_path(self.tenant, self.now.strftime("%Y-%m"), scope_key("tenant"))

    def upload(self, key: str = "emulator-audio-request") -> ResourceId:
        return asyncio.run(
            self.audio.transcribe(self.identity(), self.tenant, None, wav(), key, connected)
        ).audio_input_id

    def test_contention_admits_one_provider_and_replays_ready_without_rebilling(self) -> None:
        started, release = Event(), Event()

        async def delayed() -> None:
            started.set()
            while not release.is_set():
                await asyncio.sleep(0.01)

        self.speech.hook = delayed
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.upload)
            try:
                self.assertTrue(started.wait(10))
                with self.assertRaises(APIError) as duplicate:
                    self.upload()
                self.assertEqual(duplicate.exception.code, ErrorCode.CONFLICT)
                self.assertEqual(self.speech.calls, 1)
            finally:
                release.set()
            audio = first.result(timeout=15)
        self.assertEqual(self.upload(), audio)
        self.assertEqual(self.speech.calls, 1)
        self.assertEqual(self.db.document(self.slot).get().to_dict(), {"active": 0})
        self.assertEqual((self.db.document(self.bucket).get().to_dict() or {})["turns"], 0)
        conversation = self.conversation(private=True)
        run = self.conversations.turn(
            self.identity(),
            self.tenant,
            conversation,
            TurnCreate(audio_input_id=audio, output_mode="text"),
            "emulator-audio-turn",
        ).run_id
        self.execute(run)
        self.assertEqual(
            self.conversations.get_run(self.identity(), self.tenant, run).status, "completed"
        )
        self.assertEqual((self.db.document(self.bucket).get().to_dict() or {})["turns"], 1)

    def test_concurrent_voice_budget_cannot_overspend(self) -> None:
        policy = Policy().model_dump()
        policy["tenant"]["voice_seconds"] = 1
        self.db.document(f"tenants/{self.tenant}/usage_policy/current").set(policy)

        def attempt() -> int:
            try:
                self.upload(str(uuid4()))
                return 201
            except APIError as error:
                self.assertEqual(error.code, ErrorCode.RATE_LIMITED)
                return 429

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: attempt(), range(2)))
        self.assertEqual(sorted(results), [201, 429])
        self.assertEqual(self.speech.calls, 1)
        self.assertEqual((self.db.document(self.bucket).get().to_dict() or {})["voice_seconds"], 1)
        self.assertEqual(self.db.document(self.slot).get().to_dict(), {"active": 0})

    def test_expired_execution_reconciles_without_run_and_late_provider_cannot_publish(
        self,
    ) -> None:
        async def delayed() -> None:
            self.now += timedelta(seconds=60)
            audio = ResourceId(
                next(self.db.collection(f"tenants/{self.tenant}/audio_inputs").stream()).id
            )
            UsageReconciler(self.jobs).reconcile(self.tenant, audio)
            UsageReconciler(self.jobs).reconcile(self.tenant, audio)

        self.speech.hook = delayed
        with self.assertRaises(APIError):
            self.upload()
        self.assertEqual(self.db.document(self.slot).get().to_dict(), {"active": 0})
        docs = self.persistence.page(f"tenants/{self.tenant}/audio_inputs")
        self.assertEqual(len(docs), 1)
        path, doc = docs[0]
        self.assertNotIn("transcript", doc)
        saved = (
            self.db.document(reservation_path(self.tenant, ResourceId(doc["id"]), "speech"))
            .get()
            .to_dict()
        )
        assert saved is not None
        self.assertEqual(saved["state"], "unknown")
        self.now += timedelta(minutes=15)
        self.audio.expire(self.tenant)
        self.assertFalse(self.db.document(path).get().exists)

    def test_account_deletion_during_provider_blocks_persisted_transcript(self) -> None:
        async def revoke() -> None:
            self.db.document(lifecycle_account_path(self.identity().user_id)).set(
                {"state": "pending"}
            )

        self.speech.hook = revoke
        with self.assertRaises(APIError):
            self.upload()
        self.assertFalse(
            any(
                "transcript" in doc
                for _, doc in self.persistence.page(f"tenants/{self.tenant}/audio_inputs")
            )
        )
        self.assertEqual(self.db.document(self.slot).get().to_dict(), {"active": 0})

    def test_admission_response_loss_does_not_retry_provider(self) -> None:
        original = self.audio.idempotency.execute

        def lose(**kwargs: object) -> object:
            original(**kwargs)  # type: ignore[arg-type]
            raise RuntimeError("synthetic commit acknowledgement loss")

        with patch.object(self.audio.idempotency, "execute", side_effect=lose):
            with self.assertRaises(RuntimeError):
                self.upload()
        self.assertEqual(self.speech.calls, 0)
        with self.assertRaises(APIError) as pending:
            self.upload()
        self.assertEqual(pending.exception.code, ErrorCode.CONFLICT)
        audio = ResourceId(
            next(self.db.collection(f"tenants/{self.tenant}/audio_inputs").stream()).id
        )
        self.now += timedelta(seconds=60)
        UsageReconciler(self.jobs).reconcile(self.tenant, audio)
        self.assertEqual(self.db.document(self.slot).get().to_dict(), {"active": 0})
        self.assertEqual(self.speech.calls, 0)
