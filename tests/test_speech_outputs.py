"""VOICE-02 / T-007 T-021: sentence delivery, current authorization and bounded billing."""

import asyncio
import base64
import copy
import json
import time
import unittest
from collections.abc import AsyncGenerator, Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Event
from typing import Any, cast
from unittest.mock import patch
from uuid import uuid4

import httpx
from brainoid_agent_core.ports import CallControl
from brainoid_agent_core.types import Completed, ModelEvent, ModelRequest, TextDelta
from brainoid_api.adapters.google_tts import GoogleTTS, TTSSettings
from brainoid_api.auth.runtime import AuthServices, get_auth_services
from brainoid_api.errors import APIError, ErrorCode
from brainoid_api.routes.agents import reader_identity
from brainoid_api.services.private_sessions import PrivateSessionCreate
from brainoid_api.settings import ConfigurationError, load_settings
from brainoid_api.speech_outputs import SpeechOutputs
from brainoid_contracts import DisplayPolicy, ResourceId
from brainoid_contracts.deletion import lifecycle_account_path, purge_barrier_path
from brainoid_persistence.firestore import PersistenceUnavailable
from brainoid_runs.models import TurnCreate
from brainoid_runs.speech import sentence_ends, speech_path
from brainoid_runs.state import conversation_path, run_path
from brainoid_usage import Policy, bucket_path, scope_key
from brainoid_usage.ledger import reservation_path
from brainoid_usage.ledger import run_path as usage_path
from brainoid_worker.usage_reconciler import UsageReconciler
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from pydantic import SecretStr
from test_api_foundation import CLOUD_ENV, LOCAL_ENV
from test_audio_inputs import connected, wav
from test_deletion_requests import OWNER, USER, DeletionFixture
from test_turn_run import StrictStore, TurnRunFixture


class FakeSynthesizer:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.hook: Callable[[], Awaitable[None]] | None = None
        self.audio = wav()
        self.cancelled = False

    async def synthesize(self, text: str) -> bytes:
        self.calls.append(text)
        try:
            if self.hook:
                await self.hook()
            return self.audio
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class SpeechFixture(TurnRunFixture):
    def setUp(self) -> None:
        super().setUp()
        self.synth = FakeSynthesizer()
        self.speech = SpeechOutputs(self.conversations, self.synth)
        self.conversations.speech_enabled = True
        self.app.dependency_overrides[get_auth_services] = lambda: AuthServices(
            tenants=self.tenants,
            devices=self.repo,
            conversations=self.conversations,
            speech_outputs=self.speech,
        )
        self.model.text = "最初の文です。二つ目の文です。"
        self.bucket = bucket_path(
            self.tenant, self.clock.now().strftime("%Y-%m"), scope_key("tenant")
        )
        self.slot = f"tenants/{self.tenant}/usage_slots/{scope_key('tenant')}"

    def accept_speech(self, conversation: ResourceId, **fields: Any) -> ResourceId:
        response = self.client.post(
            self.url + f"/{conversation}/turns",
            json={"text": "Synthetic question", "output_mode": "speech", **fields},
            headers={"Idempotency-Key": str(uuid4())},
        )
        self.assertEqual(response.status_code, 202, response.text)
        return ResourceId(response.json()["run_id"])

    def references(self, run: ResourceId) -> list[dict[str, Any]]:
        return sorted(
            [
                d
                for p, d in self.store.data.items()
                if p.startswith(f"tenants/{self.tenant}/speech_outputs/") and d["run_id"] == run
            ],
            key=lambda d: d["sentence_index"],
        )

    def complete_speech(
        self, *, private: bool = False
    ) -> tuple[ResourceId, ResourceId, ResourceId]:
        conversation = self.conversation(private=private)
        run = self.accept_speech(conversation)
        self.execute(run)
        refs = self.references(run)
        self.assertEqual(len(refs), 2)
        return conversation, run, ResourceId(refs[0]["id"])

    def fetch(self, speech: ResourceId, session: ResourceId | None = None) -> Any:
        return self.client.get(
            f"/v1/tenants/{self.tenant}/speech-outputs/{speech}/audio",
            headers={"X-Private-Session": session} if session else {},
        )

    def private_output(self) -> tuple[ResourceId, ResourceId, Any, Any]:
        credentials = self.credentials()
        device = self.repo.authenticate(credentials.access_token)
        self.store.data[f"tenants/{self.tenant}/devices/{device.sub}"]["capabilities"] = ["display"]
        session = self.sessions.start(
            self.identity(),
            self.tenant,
            device.sub,
            PrivateSessionCreate(acknowledge_shared_space=True, allowed_scopes=["conversation"]),
            "speech-session-request",
            "speech-session-trace",
        )
        conversation = self.conversation(private=True, display=DisplayPolicy.AUTHENTICATED_SESSION)
        run = self.accept_speech(
            conversation, output_device_id=device.sub, expected_output_private_session_id=session.id
        )
        self.execute(run)
        self.app.dependency_overrides[reader_identity] = lambda: device
        return run, ResourceId(self.references(run)[0]["id"]), device, session

    def stopped_speech_run(self) -> tuple[ResourceId, ResourceId]:
        """Real first-sentence commit, then restore its snapshot after worker cleanup."""
        release = Event()

        class Model:
            async def stream(
                self, request: ModelRequest, *, control: CallControl
            ) -> AsyncGenerator[ModelEvent]:
                yield TextDelta(text="最初の文です。")
                while not release.is_set():
                    await asyncio.sleep(0.01)
                yield Completed()

        self.handler.model = Model()
        policy = Policy().model_dump()
        policy["run_seconds"] = 60
        self.store.data[f"tenants/{self.tenant}/usage_policy/current"] = policy
        run = self.accept_speech(self.conversation())
        with ThreadPoolExecutor(max_workers=1) as pool:
            worker = pool.submit(self.execute, run)
            try:
                deadline = time.monotonic() + 5
                while not self.references(run) and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(len(self.references(run)), 1)
                with cast(StrictStore, self.store).lock:
                    stopped = copy.deepcopy(self.store.data)
                speech = ResourceId(self.references(run)[0]["id"])
            finally:
                release.set()
            worker.result(timeout=10)
        self.store.data = stopped
        return run, speech


class SpeechTests(SpeechFixture):
    def test_attempt_expiry_during_settlement_retains_usage_but_rejects_audio(self) -> None:
        _, _, speech = self.complete_speech()
        settle = self.speech.usage.settle

        def delayed(*args: Any, **kwargs: Any) -> None:
            settle(*args, **kwargs)
            self.clock.advance(60)

        with patch.object(self.speech.usage, "settle", side_effect=delayed):
            response = self.fetch(speech)
        self.assertEqual(response.status_code, 410)
        self.assertEqual(len(self.synth.calls), 1)
        self.assertEqual(self.store.data[self.slot], {"active": 0})
        self.assertEqual(self.store.data[self.bucket]["voice_seconds"], 1)

    def test_attempt_deadline_survives_final_http_handoff_and_later_reference_deadline(
        self,
    ) -> None:
        _, _, speech = self.complete_speech()
        authorize = self.speech.authorize
        calls = 0

        def delayed(*args: Any, **kwargs: Any) -> Any:
            nonlocal calls
            result = authorize(*args, **kwargs)
            calls += 1
            if calls == 2:  # HTTP's final fresh read, after the service already returned.
                self.clock.advance(60)
                # A subsequent attempt's persisted deadline cannot extend this audio.
                self.store.data[speech_path(self.tenant, speech)]["attempt_expires_at"] = (
                    self.clock.now() + timedelta(seconds=60)
                ).isoformat()
            return result

        with patch.object(self.speech, "authorize", side_effect=delayed):
            response = self.fetch(speech)
        self.assertEqual(calls, 2)
        self.assertEqual(response.status_code, 410)
        self.assertEqual(len(self.synth.calls), 1)
        self.assertEqual(self.store.data[self.slot], {"active": 0})
        self.assertEqual(self.store.data[self.bucket]["voice_seconds"], 1)

    def test_stopped_running_and_waiting_parent_expiry_requires_no_reconciler(self) -> None:
        run, speech = self.stopped_speech_run()
        self.clock.advance(60)
        for status in ("running", "waiting_confirmation"):
            with self.subTest(status=status):
                self.store.data[run_path(self.tenant, run)]["status"] = status
                before = copy.deepcopy(self.store.data)
                self.assertEqual(self.fetch(speech).status_code, 429)
                self.assertEqual(self.store.data, before)
                self.assertEqual(self.synth.calls, [])

    def test_inactive_unfinished_parent_rejects_synthesis_before_hard_deadline(self) -> None:
        run, speech = self.stopped_speech_run()
        self.store.data[usage_path(self.tenant, run)]["active"] = False
        self.assertEqual(self.fetch(speech).status_code, 429)
        self.assertEqual(self.synth.calls, [])

    def test_parent_deadline_crossed_during_provider_rejects_late_audio(self) -> None:
        run, speech = self.stopped_speech_run()
        self.clock.advance(59)

        async def expire() -> None:
            self.clock.advance(1)

        self.synth.hook = expire
        self.assertEqual(self.fetch(speech).status_code, 429)
        self.assertEqual(len(self.synth.calls), 1)
        self.assertEqual(self.store.data[run_path(self.tenant, run)]["status"], "running")
        self.assertEqual(self.store.data[self.bucket]["voice_seconds"], 60)
        self.assertEqual(self.store.data[self.slot], {"active": 1})  # Only the stopped parent.
        UsageReconciler(self.jobs).reconcile(self.tenant, run)
        self.assertEqual(self.store.data[self.slot], {"active": 0})

    def test_parent_deadline_crossed_in_final_http_handoff_rejects_audio(self) -> None:
        _, speech = self.stopped_speech_run()
        self.clock.advance(59)
        authorize = self.speech.authorize
        calls = 0

        def delayed(*args: Any, **kwargs: Any) -> Any:
            nonlocal calls
            result = authorize(*args, **kwargs)
            calls += 1
            if calls == 2:
                self.clock.advance(1)
            return result

        with patch.object(self.speech, "authorize", side_effect=delayed):
            self.assertEqual(self.fetch(speech).status_code, 410)
        self.assertEqual(calls, 2)
        self.assertEqual(self.store.data[self.bucket]["voice_seconds"], 1)

    def test_completed_parent_releases_slot_and_audio_remains_available_until_reference_expiry(
        self,
    ) -> None:
        policy = Policy().model_dump()
        policy["run_seconds"] = 60
        self.store.data[f"tenants/{self.tenant}/usage_policy/current"] = policy
        _, run, speech = self.complete_speech()
        self.assertFalse(self.store.data[usage_path(self.tenant, run)]["active"])
        self.clock.advance(60)
        self.assertEqual(self.fetch(speech).status_code, 200)
        self.assertEqual(self.store.data[self.bucket]["voice_seconds"], 1)
        self.assertEqual(self.store.data[self.slot], {"active": 0})

    def test_two_ordered_sentences_and_first_audio_before_model_finishes(self) -> None:
        release = Event()

        class Model:
            async def stream(
                self, request: ModelRequest, *, control: CallControl
            ) -> AsyncGenerator[ModelEvent]:
                yield TextDelta(text="最初の")
                yield TextDelta(text="文です。")
                while not release.is_set():
                    await asyncio.sleep(0.01)
                yield TextDelta(text="二つ目の文です。")
                yield Completed()

        self.handler.model = Model()
        conversation = self.conversation()
        run = self.accept_speech(conversation)
        with ThreadPoolExecutor(max_workers=1) as pool:
            worker = pool.submit(self.execute, run)
            try:
                deadline = time.monotonic() + 5
                while not self.references(run) and time.monotonic() < deadline:
                    time.sleep(0.01)
                refs = self.references(run)
                self.assertEqual(len(refs), 1)
                self.assertFalse(worker.done())
                self.assertEqual(self.get_run(run).json()["status"], "running")
                policy = Policy().model_dump()
                policy["tenant"]["concurrent_runs"] = 1
                self.store.data[f"tenants/{self.tenant}/usage_policy/current"] = policy
                self.assertEqual(self.fetch(ResourceId(refs[0]["id"])).status_code, 429)
                self.assertEqual(self.synth.calls, [])
                self.assertEqual(self.references(run)[0]["attempt_count"], 0)
                policy["tenant"]["concurrent_runs"] = 2
                response = self.fetch(ResourceId(refs[0]["id"]))
                self.assertEqual(response.status_code, 200, response.text[:50])
                self.assertEqual(response.content, wav())
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertEqual(self.synth.calls, ["最初の文です。"])
            finally:
                release.set()
            worker.result(timeout=10)
        refs = self.references(run)
        self.assertEqual([r["sentence_index"] for r in refs], [0, 1])
        self.assertEqual(self.fetch(ResourceId(refs[1]["id"])).status_code, 200)
        self.assertEqual(self.synth.calls, ["最初の文です。", "二つ目の文です。"])
        events = sorted(
            [
                d
                for p, d in self.store.data.items()
                if p.startswith(run_path(self.tenant, run) + "/events/")
            ],
            key=lambda d: d["seq"],
        )
        self.assertEqual([e["seq"] for e in events], list(range(1, len(events) + 1)))
        validator = Draft202012Validator(
            json.loads(Path("specs/run-events.schema.json").read_text())
        )
        for event in events:
            validator.validate({k: v for k, v in event.items() if k != "expires_at"})
        ready = [e["payload"] for e in events if e["type"] == "speech.ready"]
        self.assertEqual([r["speech_id"] for r in ready], [r["id"] for r in refs])
        self.assertEqual(set(ready[0]), {"speech_id", "sentence_index", "expires_at"})
        self.assertEqual(events[-1]["type"], "run.completed")
        sse = self.client.get(f"/v1/tenants/{self.tenant}/runs/{run}/events")
        self.assertEqual(sse.status_code, 200)
        self.assertEqual(sse.text.count("event: speech.ready"), 2)
        self.assertFalse(
            any(isinstance(v, bytes) for d in self.store.data.values() for v in d.values())
        )
        self.assertNotIn("text", refs[0])

    def test_final_remainder_checkpoint_replay_and_transaction_retry_do_not_duplicate(self) -> None:
        self.model.text = "第一文。末尾に句点なし"
        conversation = self.conversation()
        run = self.accept_speech(conversation)
        with patch.object(self.jobs, "complete", side_effect=PersistenceUnavailable()):
            with self.assertRaises(PersistenceUnavailable):
                self.execute(run)
        self.clock.advance(6)
        self.store.retry_once = True
        self.execute(run)
        refs = self.references(run)
        self.assertEqual(len(refs), 2)
        for ref in refs:
            self.assertEqual(self.fetch(ResourceId(ref["id"])).status_code, 200)
        self.assertEqual(self.synth.calls, ["第一文。", "末尾に句点なし"])
        self.assertEqual(len(self.model.requests), 1)
        self.assertEqual(sentence_ends("価格3.14です。続き", final=False), [9])
        self.assertEqual(sentence_ends("Hello. World", final=False), [6])

    def test_retrieval_is_bounded_and_failures_never_return_audio(self) -> None:
        _, run, speech = self.complete_speech()
        expires = self.store.data[speech_path(self.tenant, speech)]["expires_at"]
        self.assertEqual(self.fetch(speech).status_code, 200)
        self.assertEqual(self.fetch(speech).status_code, 200)
        self.assertEqual(self.fetch(speech).status_code, 429)
        self.assertEqual(len(self.synth.calls), 2)
        self.assertEqual(self.store.data[self.bucket]["turns"], 1)
        self.assertEqual(self.store.data[self.bucket]["voice_seconds"], 2)
        self.assertEqual(self.store.data[speech_path(self.tenant, speech)]["expires_at"], expires)
        self.assertEqual(self.store.data[self.slot], {"active": 0})

        second = ResourceId(self.references(run)[1]["id"])

        async def fail() -> None:
            raise RuntimeError("SYNTHETIC PRIVATE PROVIDER ERROR")

        self.synth.hook = fail
        for _ in range(2):
            response = self.fetch(second)
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.headers["content-type"], "application/json")
            self.assertNotIn("PRIVATE", response.text)
        self.assertEqual(self.fetch(second).status_code, 429)
        self.assertEqual(self.store.data[self.bucket]["voice_seconds"], 122)
        self.assertEqual(self.store.data[self.slot], {"active": 0})

    def test_quotes_and_repeated_terminal_marks_never_create_empty_synthesis(self) -> None:
        self.model.text = "「最初の文。」！？二つ目の文です。』"
        _, run, _ = self.complete_speech()
        for ref in self.references(run):
            self.assertEqual(self.fetch(ResourceId(ref["id"])).status_code, 200)
        self.assertEqual(self.synth.calls, ["「最初の文。", "二つ目の文です。"])

    def test_sentence_byte_and_count_limits_bound_atomic_publication(self) -> None:
        for text, status, count in (
            ("文。" * 64, "completed", 64),
            ("文。" * 65, "failed", 0),
            ("a" * 3999 + ".", "completed", 1),
            ("a" * 4000 + ".", "failed", 0),
        ):
            with self.subTest(length=len(text), expected=status):
                self.model.text = text
                run = self.accept_speech(self.conversation())
                self.execute(run)
                self.assertEqual(self.get_run(run).json()["status"], status)
                self.assertEqual(len(self.references(run)), count)
        self.assertEqual(self.synth.calls, [])
        self.assertEqual(self.store.data[self.slot], {"active": 0})

    def test_wrong_actor_tenant_output_mode_scope_and_provider_disabled(self) -> None:
        conversation, run, speech = self.complete_speech()
        self.uid = "demo-a-admin"
        self.assertEqual(self.fetch(speech).status_code, 404)
        self.uid = "demo-a-owner"
        self.assertEqual(
            self.client.get(f"/v1/tenants/{self.other}/speech-outputs/{speech}/audio").status_code,
            404,
        )
        self.store.data[conversation_path(self.tenant, conversation)]["display_policy"] = "never"
        self.assertEqual(self.fetch(speech).status_code, 404)
        self.assertEqual(self.synth.calls, [])
        self.conversations.speech_enabled = False
        with self.assertRaises(APIError) as disabled:
            self.conversations.turn(
                self.identity(),
                self.tenant,
                conversation,
                TurnCreate(text="x", output_mode="speech"),
                str(uuid4()),
            )
        self.assertEqual(disabled.exception.code, ErrorCode.TEMPORARILY_UNAVAILABLE)
        self.assertEqual(self.store.data[run_path(self.tenant, run)]["status"], "completed")

    def test_private_session_end_replacement_and_implicit_reader_fail(self) -> None:
        _, speech, device, session = self.private_output()
        self.assertIn(self.fetch(speech).status_code, (403, 404))
        self.app.dependency_overrides[reader_identity] = lambda: self.identity()
        self.assertEqual(self.fetch(speech).status_code, 404)
        self.app.dependency_overrides[reader_identity] = lambda: device
        self.assertEqual(self.fetch(speech, session.id).status_code, 200)
        self.store.data[f"tenants/{self.tenant}/private_sessions/{session.id}"]["status"] = "ended"
        self.assertIn(self.fetch(speech, session.id).status_code, (403, 404))
        self.store.data[f"tenants/{self.tenant}/private_sessions/{session.id}"]["status"] = "active"
        self.store.data[f"tenants/{self.tenant}/private_session_slots/{device.sub}"][
            "session_id"
        ] = str(uuid4())
        self.assertIn(self.fetch(speech, session.id).status_code, (403, 404))
        self.assertEqual(len(self.synth.calls), 1)

    def test_session_expiry_during_late_provider_and_after_settlement_discard_audio(self) -> None:
        _, speech, _, session = self.private_output()

        async def expire() -> None:
            self.clock.advance(60)

        self.synth.hook = expire
        self.assertIn(self.fetch(speech, session.id).status_code, (403, 404, 410, 429))
        self.assertEqual(len(self.synth.calls), 1)
        self.assertEqual(self.store.data[self.slot], {"active": 0})

    def test_cancellation_deletion_and_epoch_during_provider_discard_audio(self) -> None:
        conversation, run, speech = self.complete_speech(private=True)
        baseline = copy.deepcopy(self.store.data)
        for change in ("cancel", "account", "epoch", "conversation"):
            with self.subTest(change=change):
                self.store.data = copy.deepcopy(baseline)

                async def revoke(change: str = change) -> None:
                    if change == "cancel":
                        self.store.data[run_path(self.tenant, run)]["status"] = "cancelled"
                    elif change == "account":
                        self.store.data[lifecycle_account_path(self.uid)] = {"state": "pending"}
                    elif change == "epoch":
                        self.store.data[purge_barrier_path(self.tenant)] = {"epoch": 1}
                    else:
                        self.store.data[conversation_path(self.tenant, conversation)]["deleted"] = (
                            True
                        )

                self.synth.hook = revoke
                response = self.fetch(speech)
                self.assertIn(response.status_code, (401, 403, 404))
                self.assertNotEqual(response.headers["content-type"], "audio/wav")
                self.assertEqual(self.store.data[self.slot], {"active": 0})

    def test_exact_five_minute_expiry_and_expiry_crossing_in_admission(self) -> None:
        _, _, speech = self.complete_speech()
        self.clock.advance(299)
        before = copy.deepcopy(self.store.data)
        original = self.speech.usage.prepare_admit

        def delayed(*args: Any, **kwargs: Any) -> Any:
            result = original(*args, **kwargs)
            self.clock.advance(1)
            return result

        with patch.object(self.speech.usage, "prepare_admit", side_effect=delayed):
            self.assertEqual(self.fetch(speech).status_code, 410)
        self.assertEqual(self.store.data, before)
        self.assertEqual(self.synth.calls, [])
        self.speech.expire(self.tenant)
        self.assertNotIn(speech_path(self.tenant, speech), self.store.data)
        self.assertEqual(self.fetch(speech).status_code, 404)

    def test_usage_budget_prevents_provider_and_unknown_recovery_survives_reference_sweep(
        self,
    ) -> None:
        _, _, speech = self.complete_speech()
        policy = Policy().model_dump()
        policy["tenant"]["voice_seconds"] = 59
        self.store.data[f"tenants/{self.tenant}/usage_policy/current"] = policy
        self.assertEqual(self.fetch(speech).status_code, 429)
        self.assertEqual(self.synth.calls, [])
        policy["tenant"]["voice_seconds"] = 60
        original = self.speech.store.run

        def lost(work: Any) -> Any:
            result = original(work)
            if isinstance(result, tuple) and isinstance(result[0], str):
                raise PersistenceUnavailable()
            return result

        with patch.object(self.speech.store, "run", side_effect=lost):
            with self.assertRaises(PersistenceUnavailable):
                asyncio.run(
                    self.speech.synthesize(self.identity(), self.tenant, None, speech, connected)
                )
        doc = self.store.data[speech_path(self.tenant, speech)]
        attempt = ResourceId(doc["active_attempt"])
        self.assertEqual(self.synth.calls, [])
        self.clock.advance(300)
        self.speech.expire(self.tenant)
        self.assertEqual(self.store.data[self.slot], {"active": 1})
        UsageReconciler(self.jobs).reconcile(self.tenant, attempt)
        UsageReconciler(self.jobs).reconcile(self.tenant, attempt)
        self.assertEqual(self.store.data[self.slot], {"active": 0})
        self.assertEqual(
            self.store.data[reservation_path(self.tenant, attempt, "speech")]["state"], "unknown"
        )
        self.assertEqual(self.store.data[self.bucket]["voice_seconds"], 60)

    def test_timeout_disconnect_malformed_output_and_settlement_revocation(self) -> None:
        _, _, speech = self.complete_speech()

        async def delayed() -> None:
            await asyncio.sleep(10)

        self.synth.hook = delayed
        real_timeout = asyncio.timeout
        with patch(
            "brainoid_api.speech_outputs.asyncio.timeout", side_effect=lambda _: real_timeout(0.01)
        ):
            self.assertEqual(self.fetch(speech).status_code, 503)
        self.assertTrue(self.synth.cancelled)
        self.synth.hook = None
        settle = self.speech.usage.settle

        def revoke(*args: Any, **kwargs: Any) -> None:
            settle(*args, **kwargs)
            self.store.data[lifecycle_account_path(self.uid)] = {"state": "pending"}

        with patch.object(self.speech.usage, "settle", side_effect=revoke):
            response = self.fetch(speech)
        self.assertIn(response.status_code, (401, 403, 404))
        self.assertEqual(self.store.data[self.slot], {"active": 0})

    def test_release_guard_catches_expiry_after_authorization_transaction(self) -> None:
        _, _, speech = self.complete_speech()
        result = self.speech.authorize(self.identity(), self.tenant, None, speech)
        self.clock.advance(300)
        with self.assertRaises(APIError) as expired:
            result.release()
        self.assertEqual(expired.exception.code, ErrorCode.RESOURCE_EXPIRED)

    def test_malformed_audio_and_disconnect_are_not_success(self) -> None:
        _, run, speech = self.complete_speech()
        self.synth.audio = b"SYNTHETIC INVALID AUDIO"
        response = self.fetch(speech)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("SYNTHETIC", response.text)
        self.assertEqual(self.store.data[self.bucket]["voice_seconds"], 60)
        second = ResourceId(self.references(run)[1]["id"])

        async def disconnected() -> bool:
            return True

        with self.assertRaises(APIError) as error:
            asyncio.run(
                self.speech.synthesize(self.identity(), self.tenant, None, second, disconnected)
            )
        self.assertEqual(error.exception.code, ErrorCode.CONFLICT)
        self.assertEqual(len(self.synth.calls), 1)
        self.assertEqual(self.store.data[self.bucket]["voice_seconds"], 60)
        self.assertEqual(self.store.data[self.slot], {"active": 0})

    def test_live_cancel_api_fences_late_audio_and_worker_completion(self) -> None:
        release = Event()

        class Model:
            async def stream(
                self, request: ModelRequest, *, control: CallControl
            ) -> AsyncGenerator[ModelEvent]:
                yield TextDelta(text="最初の文です。")
                while not release.is_set():
                    await asyncio.sleep(0.01)
                yield Completed()

        self.handler.model = Model()
        run = self.accept_speech(self.conversation())
        with ThreadPoolExecutor(max_workers=1) as pool:
            worker = pool.submit(self.execute, run)
            try:
                deadline = time.monotonic() + 5
                while not self.references(run) and time.monotonic() < deadline:
                    time.sleep(0.01)
                speech = ResourceId(self.references(run)[0]["id"])

                async def cancel() -> None:
                    self.conversations.cancel_run(
                        self.identity(), self.tenant, run, "speech-live-cancel-key"
                    )

                self.synth.hook = cancel
                self.assertEqual(self.fetch(speech).status_code, 404)
            finally:
                release.set()
            worker.result(timeout=10)
        self.assertEqual(self.get_run(run).json()["status"], "cancelled")
        self.assertEqual(self.store.data[self.slot], {"active": 0})

    def test_shared_pi_output_and_revoked_family(self) -> None:
        credentials = self.credentials()
        device = self.repo.authenticate(credentials.access_token)
        self.store.data[f"tenants/{self.tenant}/devices/{device.sub}"]["capabilities"] = ["display"]
        run = self.accept_speech(self.conversation(), output_device_id=device.sub)
        self.execute(run)
        speech = ResourceId(self.references(run)[0]["id"])
        self.app.dependency_overrides[reader_identity] = lambda: device
        self.assertEqual(self.fetch(speech).status_code, 200)
        self.store.data[f"device_credential_families/{device.family_id}"]["status"] = "revoked"
        self.assertIn(self.fetch(speech).status_code, (401, 403, 404))
        self.assertEqual(len(self.synth.calls), 1)

    def test_device_origin_private_session_is_bound_through_worker_and_get(self) -> None:
        credentials = self.credentials()
        device = self.repo.authenticate(credentials.access_token)
        self.store.data[f"tenants/{self.tenant}/devices/{device.sub}"]["capabilities"] = ["display"]
        session = self.sessions.start(
            self.identity(),
            self.tenant,
            device.sub,
            PrivateSessionCreate(acknowledge_shared_space=True, allowed_scopes=["conversation"]),
            "device-origin-session",
            "device-origin-trace",
        )
        conversation = self.conversation(private=True, display=DisplayPolicy.AUTHENTICATED_SESSION)
        run = self.conversations.turn(
            device,
            self.tenant,
            conversation,
            TurnCreate(text="Synthetic question", output_mode="speech"),
            "device-speech-turn-key",
            session.id,
        ).run_id
        self.execute(run)
        self.app.dependency_overrides[reader_identity] = lambda: device
        speech = ResourceId(self.references(run)[0]["id"])
        self.assertEqual(self.fetch(speech, session.id).status_code, 200)
        self.store.data[f"device_credential_families/{device.family_id}"]["status"] = "revoked"
        self.assertIn(self.fetch(speech, session.id).status_code, (401, 403, 404))


class GoogleTTSTests(unittest.IsolatedAsyncioTestCase):
    async def token(self) -> SecretStr:
        return SecretStr("synthetic-tts-token")

    def provider(self, transport: Any) -> GoogleTTS:
        return GoogleTTS(
            TTSSettings(project="demo-brainoid", location="test-region", voice="ja-JP-Synthetic-A"),
            self.token,
            transport=httpx.MockTransport(transport),
        )

    async def test_wire_inline_japanese_voice_project_and_redacted_failures(self) -> None:
        calls = []

        async def transport(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            self.assertEqual(request.url.host, "test-region-texttospeech.googleapis.com")
            self.assertEqual(request.headers["x-goog-user-project"], "demo-brainoid")
            self.assertEqual(
                json.loads(request.content),
                {
                    "input": {"text": "こんにちは。"},
                    "voice": {"languageCode": "ja-JP", "name": "ja-JP-Synthetic-A"},
                    "audioConfig": {"audioEncoding": "LINEAR16", "sampleRateHertz": 16000},
                },
            )
            return httpx.Response(200, json={"audioContent": base64.b64encode(wav()).decode()})

        provider = self.provider(transport)
        try:
            self.assertEqual(await provider.synthesize("こんにちは。"), wav())
            self.assertEqual(len(calls), 1)
        finally:
            await provider.close()

    async def test_http_invalid_oversize_and_timeout_never_retry(self) -> None:
        for status, body, code in (
            (429, b"PRIVATE", ErrorCode.RATE_LIMITED),
            (403, b"PRIVATE", ErrorCode.TEMPORARILY_UNAVAILABLE),
            (500, b"PRIVATE", ErrorCode.TEMPORARILY_UNAVAILABLE),
            (302, b"PRIVATE", ErrorCode.TEMPORARILY_UNAVAILABLE),
            (200, b"{}", ErrorCode.TEMPORARILY_UNAVAILABLE),
            (200, b'{"audioContent":"%%%PRIVATE"}', ErrorCode.TEMPORARILY_UNAVAILABLE),
            (200, b"x" * 2_600_001, ErrorCode.TEMPORARILY_UNAVAILABLE),
        ):
            calls = 0

            async def transport(
                request: httpx.Request, status: int = status, body: bytes = body
            ) -> httpx.Response:
                nonlocal calls
                calls += 1
                return httpx.Response(status, content=body)

            provider = self.provider(transport)
            try:
                with self.assertRaises(APIError) as error:
                    await provider.synthesize("こんにちは。")
                self.assertEqual(error.exception.code, code)
                self.assertNotIn("PRIVATE", repr(error.exception))
                self.assertEqual(calls, 1)
            finally:
                await provider.close()

    async def test_transport_cancellation_and_absolute_deadline(self) -> None:
        cancelled = Event()

        async def transport(request: httpx.Request) -> httpx.Response:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            raise AssertionError

        provider = self.provider(transport)
        try:
            task = asyncio.create_task(provider.synthesize("こんにちは。"))
            await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(cancelled.is_set())
            real_timeout = asyncio.timeout
            with patch(
                "brainoid_api.adapters.google_tts.asyncio.timeout",
                side_effect=lambda _: real_timeout(0.01),
            ):
                with self.assertRaises(APIError) as error:
                    await provider.synthesize("こんにちは。")
            self.assertEqual(error.exception.code, ErrorCode.TEMPORARILY_UNAVAILABLE)
        finally:
            await provider.close()


class SpeechSettingsTests(unittest.TestCase):
    def test_default_disabled_explicit_settings_no_local_paid_calls(self) -> None:
        self.assertEqual(load_settings(LOCAL_ENV).tts_provider, "disabled")
        for env in (LOCAL_ENV, CLOUD_ENV):
            with self.assertRaises(ConfigurationError):
                load_settings({**env, "TTS_PROVIDER": "google-v1"})
        with self.assertRaises(ConfigurationError):
            load_settings(
                {
                    **LOCAL_ENV,
                    "TTS_PROVIDER": "google-v1",
                    "TTS_LOCATION": "global",
                    "TTS_VOICE": "ja-JP-Synthetic-A",
                }
            )
        self.assertEqual(
            load_settings(
                {
                    **CLOUD_ENV,
                    "TTS_PROVIDER": "google-v1",
                    "TTS_LOCATION": "global",
                    "TTS_VOICE": "ja-JP-Synthetic-A",
                }
            ).tts_voice,
            "ja-JP-Synthetic-A",
        )


class SpeechPurgeTests(DeletionFixture):
    def test_member_purge_erases_owned_references_preserving_other_owner(self) -> None:
        own, other = (f"tenants/{self.tenant}/speech_outputs/{uuid4()}" for _ in range(2))
        for path, owner in ((own, USER), (other, OWNER)):
            self.store.data[path] = {
                "tenant_id": self.tenant,
                "owner_user_id": owner,
                "visibility": "private",
                "run_id": str(uuid4()),
                "created_at": self.now.isoformat(),
            }
        operation = self.accept()
        self.drain(operation, self.tenant)
        self.assertNotIn(own, self.store.data)
        self.assertIn(other, self.store.data)
