"""VOICE-01: real HTTP/WAV, authority, quota and normal worker boundaries."""

import asyncio
import copy
import json
import struct
import unittest
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import httpx
from brainoid_agent_core.types.speech import SpeechRecognition
from brainoid_api.adapters.google_speech import GoogleSpeech, SpeechSettings
from brainoid_api.audio_inputs import (
    MAX_BYTES,
    AudioInputs,
    Recognition,
    audio_path,
    receive_audio,
    wav_request,
)
from brainoid_api.auth.runtime import AuthServices, get_auth_services
from brainoid_api.errors import APIError, ErrorCode
from brainoid_api.routes.agents import reader_identity
from brainoid_api.services.private_sessions import PrivateSessionCreate
from brainoid_api.settings import ConfigurationError, load_settings
from brainoid_contracts import DisplayPolicy, ResourceId
from brainoid_contracts.deletion import lifecycle_account_path, purge_barrier_path
from brainoid_usage import Policy, bucket_path, scope_key
from brainoid_worker.usage_reconciler import UsageReconciler
from pydantic import SecretStr
from test_api_foundation import CLOUD_ENV, LOCAL_ENV
from test_deletion_requests import OWNER, USER, DeletionFixture
from test_turn_run import TurnRunFixture


def wav(frames: int = 16000, *, silent: bool = False, before: bytes = b"") -> bytes:
    fmt = struct.pack("<4sIHHIIHH", b"fmt ", 16, 1, 1, 16000, 32000, 2, 16)
    pcm = (b"\0\0" if silent else b"\x12\x34") * frames
    body = b"WAVE" + before + fmt + struct.pack("<4sI", b"data", len(pcm)) + pcm
    return b"RIFF" + struct.pack("<I", len(body)) + body


async def connected() -> bool:
    return False


class FakeRecognizer:
    def __init__(self) -> None:
        self.calls = 0
        self.hook: Callable[[], Awaitable[None]] | None = None
        self.result = Recognition("明日の予定を教えてください", 1)
        self.cancelled = False

    async def recognize(self, request: SpeechRecognition) -> Recognition:
        self.calls += 1
        assert request.locale == "ja-JP" and request.format.encoding == "pcm_s16le"
        try:
            if self.hook:
                await self.hook()
            return self.result
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class AudioFixture(TurnRunFixture):
    def setUp(self) -> None:
        super().setUp()
        self.speech = FakeRecognizer()
        self.audio = AudioInputs(self.conversations, self.speech)
        self.conversations.audio_inputs = self.audio
        self.app.dependency_overrides[get_auth_services] = lambda: AuthServices(
            tenants=self.tenants,
            devices=self.repo,
            conversations=self.conversations,
            audio_inputs=self.audio,
        )
        self.audio_url = f"/v1/tenants/{self.tenant}/audio-inputs"
        self.bucket = bucket_path(
            self.tenant, self.clock.now().strftime("%Y-%m"), scope_key("tenant")
        )
        self.slot = f"tenants/{self.tenant}/usage_slots/{scope_key('tenant')}"

    def upload(
        self,
        data: bytes | None = None,
        *,
        key: str = "audio-fixture-request",
        session: ResourceId | None = None,
    ) -> Any:
        headers = {"Content-Type": "audio/wav", "Idempotency-Key": key}
        if session:
            headers["X-Private-Session"] = session
        return self.client.post(
            self.audio_url, content=wav() if data is None else data, headers=headers
        )

    def audio_turn(
        self,
        conversation: ResourceId,
        audio: str,
        *,
        key: str = "audio-turn-fixture-key",
        session: ResourceId | None = None,
    ) -> Any:
        headers = {"Idempotency-Key": key}
        if session:
            headers["X-Private-Session"] = session
        return self.client.post(
            self.url + f"/{conversation}/turns",
            json={"audio_input_id": audio, "output_mode": "text"},
            headers=headers,
        )


class AudioTests(AudioFixture):
    def test_http_upload_reference_to_normal_worker_and_idempotent_replay(self) -> None:
        conversation = self.conversation(private=True)
        self.store.retry_once = True
        response = self.upload()
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["transcript"], self.speech.result.text)
        self.assertEqual(self.store.data[self.bucket]["turns"], 0)
        self.assertEqual(self.store.data[self.bucket]["voice_seconds"], 1)
        self.assertEqual(self.store.data[self.slot], {"active": 0})
        self.assertEqual(self.upload().json(), response.json())
        self.assertEqual(self.speech.calls, 1)
        accepted = self.audio_turn(conversation, response.json()["audio_input_id"])
        self.assertEqual(accepted.status_code, 202, accepted.text)
        run = ResourceId(accepted.json()["run_id"])
        self.assertEqual(
            self.store.data[f"tenants/{self.tenant}/runs/{run}"]["input_text"],
            self.speech.result.text,
        )
        self.execute(run)
        self.assertEqual(self.get_run(run).json()["status"], "completed")
        self.assertEqual(self.store.data[self.bucket]["turns"], 1)
        self.clock.advance(900)
        self.assertEqual(
            self.audio_turn(conversation, response.json()["audio_input_id"]).json(), accepted.json()
        )
        self.assertEqual(
            self.audio_turn(
                conversation, response.json()["audio_input_id"], key="new-expired-turn-key"
            ).status_code,
            410,
        )
        self.assertEqual(self.upload().status_code, 410)
        self.assertEqual(self.speech.calls, 1)

    def test_invalid_real_wav_and_limits_never_call_provider(self) -> None:
        good = wav()
        malformed = [
            b"",
            b"RIFF",
            good[:-1],
            good + b"x",
            wav(0),
            wav(silent=True),
            good[:20] + struct.pack("<H", 3) + good[22:],
            good[:22] + struct.pack("<H", 2) + good[24:],
            good[:24] + struct.pack("<I", 8000) + good[28:],
            good[:28] + struct.pack("<I", 1) + good[32:],
            good[:32] + struct.pack("<H", 4) + good[34:],
            good[:34] + struct.pack("<H", 24) + good[36:],
            good[:40] + struct.pack("<I", 99999999) + good[44:],
        ]
        for i, data in enumerate(malformed):
            with self.subTest(i=i):
                self.assertEqual(
                    self.upload(data, key=f"invalid-audio-key-{i:03}").status_code, 422
                )
        for data in (wav(960001), b"x" * (MAX_BYTES + 1)):
            self.assertEqual(self.upload(data).status_code, 413)
        self.assertEqual(self.speech.calls, 0)
        self.assertNotIn(self.slot, self.store.data)

    def test_exact_wav_boundaries_and_extra_chunks(self) -> None:
        request, seconds = wav_request(wav(960000))
        self.assertEqual(len(request.audio), 1920000)
        self.assertEqual(seconds, 60)
        self.assertEqual(wav_request(wav(16001))[1], 2)
        extra = b"JUNK" + struct.pack("<I", 1) + b"x\0"
        self.assertEqual(wav_request(wav(before=extra))[1], 1)
        full = wav(
            before=b"JUNK" + struct.pack("<I", MAX_BYTES - 32052) + b"x" * (MAX_BYTES - 32052)
        )
        self.assertEqual(len(full), MAX_BYTES)
        self.assertEqual(wav_request(full)[1], 1)
        self.assertNotIn("audio=", repr(request))

    def test_other_actor_tenant_and_exact_expiry_cannot_use_reference(self) -> None:
        conversation = self.conversation()
        audio = self.upload().json()["audio_input_id"]
        self.uid = "demo-a-admin"
        self.assertEqual(self.audio_turn(conversation, audio).status_code, 404)
        self.uid = "demo-a-owner"
        self.clock.advance(899.999)
        self.audio.result(self.identity(), self.tenant, None, ResourceId(audio)).release()
        self.clock.advance(0.001)
        self.assertEqual(self.audio_turn(conversation, audio).status_code, 410)
        self.uid = "demo-b-owner"
        other = AudioInputs(self.conversations, self.speech)
        with self.assertRaises(APIError) as denied:
            other.result(self.identity(), self.other, None, ResourceId(audio))
        self.assertEqual(denied.exception.code, ErrorCode.RESOURCE_NOT_FOUND)

    def test_device_and_session_binding_and_revocation(self) -> None:
        credentials = self.credentials()
        device = self.repo.authenticate(credentials.access_token)
        self.store.data[f"tenants/{self.tenant}/devices/{device.sub}"]["capabilities"] = ["display"]
        session = self.sessions.start(
            self.identity(),
            self.tenant,
            device.sub,
            PrivateSessionCreate(acknowledge_shared_space=True, allowed_scopes=["conversation"]),
            "audio-session-request",
            "audio-request",
        )
        conversation = self.conversation(private=True, display=DisplayPolicy.AUTHENTICATED_SESSION)
        self.app.dependency_overrides[reader_identity] = lambda: device
        response = self.upload(session=session.id)
        self.assertEqual(response.status_code, 201, response.text)
        audio = response.json()["audio_input_id"]
        self.assertEqual(self.audio_turn(conversation, audio).status_code, 404)
        self.app.dependency_overrides[reader_identity] = lambda: self.identity()
        self.assertEqual(self.audio_turn(conversation, audio).status_code, 404)
        self.app.dependency_overrides[reader_identity] = lambda: device
        self.clock.advance(60)
        self.assertIn(
            self.audio_turn(conversation, audio, session=session.id).status_code, (403, 404)
        )
        self.assertEqual(self.speech.calls, 1)

    def test_private_session_changes_and_expiry_during_provider_discard_text(self) -> None:
        credentials = self.credentials()
        device = self.repo.authenticate(credentials.access_token)
        self.store.data[f"tenants/{self.tenant}/devices/{device.sub}"]["capabilities"] = ["display"]
        first = self.sessions.start(
            self.identity(),
            self.tenant,
            device.sub,
            PrivateSessionCreate(acknowledge_shared_space=True, allowed_scopes=["conversation"]),
            "audio-first-session-key",
            "audio-request",
        )
        conversation = self.conversation(private=True, display=DisplayPolicy.AUTHENTICATED_SESSION)
        self.app.dependency_overrides[reader_identity] = lambda: device
        uploaded = self.upload(session=first.id)
        self.assertEqual(uploaded.status_code, 201)
        self.sessions.end(
            self.identity(), self.tenant, first.id, "audio-end-first-session", '"1"', "end-request"
        )
        second = self.sessions.start(
            self.identity(),
            self.tenant,
            device.sub,
            PrivateSessionCreate(acknowledge_shared_space=True, allowed_scopes=["conversation"]),
            "audio-second-session-key",
            "audio-request",
        )
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(
            self.audio_turn(
                conversation, uploaded.json()["audio_input_id"], session=second.id
            ).status_code,
            404,
        )
        self.clock.advance(55)

        async def expire() -> None:
            self.clock.advance(5)

        self.speech.hook = expire
        response = self.upload(key="audio-expiring-session", session=second.id)
        self.assertIn(response.status_code, (403, 404))
        self.assertEqual(self.store.data[self.slot], {"active": 0})
        self.assertFalse(
            any(
                "transcript" in doc
                for _, doc in self.store.data.items()
                if doc.get("status") == "failed" and "/audio_inputs/" in _
            )
        )

    def test_voice_quota_and_concurrency_fail_before_provider_without_writes(self) -> None:
        for field in ("voice_seconds", "concurrent_runs"):
            with self.subTest(field=field):
                self.store.data[f"tenants/{self.tenant}/usage_policy/current"] = (
                    Policy().model_dump()
                )
                self.store.data[f"tenants/{self.tenant}/usage_policy/current"]["tenant"][field] = 0
                before = copy.deepcopy(self.store.data)
                self.assertEqual(self.upload().status_code, 429)
                self.assertEqual(self.store.data, before)
        self.assertEqual(self.speech.calls, 0)

    def test_provider_failure_silence_unknown_usage_and_no_sensitive_logs(self) -> None:
        async def fail() -> None:
            raise RuntimeError("SYNTHETIC-RAW-TEXT-TOKEN-SECRET")

        self.speech.hook = fail
        with self.assertLogs("brainoid_api", level="INFO") as logs:
            response = self.upload()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("SYNTHETIC", response.text + str(logs.output))
        self.assertEqual(self.store.data[self.slot], {"active": 0})
        reservation = next(d for p, d in self.store.data.items() if "/usage_reservations/" in p)
        self.assertEqual(reservation["state"], "unknown")
        self.assertFalse(reservation["known"]["voice_seconds"])
        self.assertEqual(self.store.data[self.bucket]["voice_seconds"], 1)
        self.assertEqual(self.upload().status_code, 409)
        self.assertEqual(self.speech.calls, 1)
        self.speech.hook = None
        self.speech.result = Recognition(" ", 1)
        self.assertEqual(self.upload(key="provider-silence-key").status_code, 422)
        self.assertFalse(
            any("transcript" in d for p, d in self.store.data.items() if "/audio_inputs/" in p)
        )

    def test_delayed_provider_fences_revoke_purge_deadline_and_cancel(self) -> None:
        async def scenario(kind: str) -> None:
            async def delayed() -> None:
                if kind == "revoke":
                    self.store.data[lifecycle_account_path(self.uid)] = {"state": "pending"}
                elif kind == "purge":
                    self.store.data[purge_barrier_path(self.tenant)] = {"epoch": 1}
                elif kind == "deadline":
                    self.clock.advance(60)
                    saved = next(
                        d
                        for p, d in self.store.data.items()
                        if "/audio_inputs/" in p and d["status"] == "processing"
                    )
                    UsageReconciler(self.jobs).reconcile(self.tenant, ResourceId(saved["id"]))
                await asyncio.sleep(0.3)

            self.speech.hook = delayed

            async def disconnected() -> bool:
                return kind == "disconnect" and self.speech.calls > 0

            with self.assertRaises(APIError):
                await self.audio.transcribe(
                    self.identity(), self.tenant, None, wav(), "delay-request-" + kind, disconnected
                )
            self.assertEqual(self.store.data[self.slot], {"active": 0})
            self.assertFalse(
                any("transcript" in d for p, d in self.store.data.items() if "/audio_inputs/" in p)
            )

        for kind in ("revoke", "purge", "deadline", "disconnect"):
            with self.subTest(kind=kind):
                before = copy.deepcopy(self.store.data)
                self.speech.calls = 0
                asyncio.run(scenario(kind))
                self.store.data = before

    def test_cancel_task_closes_provider_and_releases_unknown_reservation(self) -> None:
        async def scenario() -> None:
            started = asyncio.Event()

            async def wait() -> None:
                started.set()
                await asyncio.Event().wait()

            self.speech.hook = wait
            task = asyncio.create_task(
                self.audio.transcribe(
                    self.identity(), self.tenant, None, wav(), "cancel-task-key-001", connected
                )
            )
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())
        self.assertTrue(self.speech.cancelled)
        self.assertEqual(self.store.data[self.slot], {"active": 0})
        self.assertEqual(self.store.data[self.bucket]["voice_seconds"], 1)

    def test_no_raw_persistence_and_bounded_expiry_sweep(self) -> None:
        with patch("tempfile.SpooledTemporaryFile", side_effect=AssertionError("no spooling")):
            response = self.upload()
        self.assertEqual(response.status_code, 201)
        encoded = json.dumps(self.store.data, default=str)
        self.assertNotIn("RIFF", encoded)
        self.assertNotIn("EjQSNBI0", encoded)
        self.assertFalse(
            any(
                isinstance(value, bytes | bytearray)
                for doc in self.store.data.values()
                for value in doc.values()
            )
        )
        self.clock.advance(900)
        self.audio.expire(self.tenant)
        self.assertNotIn(
            audio_path(self.tenant, ResourceId(response.json()["audio_input_id"])), self.store.data
        )

    def test_turn_expiry_crossing_during_transaction_rolls_back_every_write(self) -> None:
        conversation = self.conversation()
        audio = self.upload().json()["audio_input_id"]
        self.clock.advance(899)
        before = copy.deepcopy(self.store.data)
        original = self.jobs.add_pending

        def expire(*args: Any, **kwargs: Any) -> Any:
            result = original(*args, **kwargs)
            self.clock.advance(1)
            return result

        with patch.object(self.jobs, "add_pending", side_effect=expire):
            self.assertEqual(self.audio_turn(conversation, audio).status_code, 410)
        self.assertEqual(self.store.data, before)

    def test_revocation_during_settlement_blocks_http_result_release(self) -> None:
        settle = self.audio.usage.settle

        def revoke(*args: Any, **kwargs: Any) -> None:
            settle(*args, **kwargs)
            self.store.data[lifecycle_account_path(self.uid)] = {"state": "pending"}

        with patch.object(self.audio.usage, "settle", side_effect=revoke):
            response = self.upload()
        self.assertIn(response.status_code, (401, 403, 404))
        self.assertNotIn(self.speech.result.text, response.text)
        self.assertEqual(self.store.data[self.slot], {"active": 0})

    def test_pre_provider_revocation_refunds_only_proven_unused_seconds(self) -> None:
        with patch.object(self.audio, "saved", side_effect=APIError(ErrorCode.RESOURCE_NOT_FOUND)):
            self.assertEqual(self.upload().status_code, 404)
        self.assertEqual(self.speech.calls, 0)
        self.assertEqual(self.store.data[self.bucket]["voice_seconds"], 0)
        self.assertEqual(self.store.data[self.slot], {"active": 0})

    def test_provider_timeout_cancels_transport_and_keeps_unknown_usage(self) -> None:
        real_timeout = asyncio.timeout

        async def wait() -> None:
            await asyncio.sleep(10)

        self.speech.hook = wait
        with patch(
            "brainoid_api.audio_inputs.asyncio.timeout",
            side_effect=lambda seconds: real_timeout(0.01 if seconds == 45 else seconds),
        ):
            self.assertEqual(self.upload().status_code, 503)
        self.assertTrue(self.speech.cancelled)
        self.assertEqual(self.store.data[self.bucket]["voice_seconds"], 1)
        self.assertEqual(self.store.data[self.slot], {"active": 0})

    def test_http_streamed_oversize_lying_length_and_mime(self) -> None:
        async def scenario() -> None:
            consumed = 0

            async def chunks() -> Any:
                nonlocal consumed
                for _ in range(7):
                    consumed += 1
                    yield b"x" * (1024 * 1024)

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app), base_url="http://testserver"
            ) as client:
                headers = {
                    "Content-Type": "audio/wav",
                    "Idempotency-Key": "stream-test-key-001",
                    "Content-Length": "1",
                }
                response = await client.post(self.audio_url, content=chunks(), headers=headers)
                self.assertEqual(response.status_code, 413)
                self.assertEqual(consumed, 6)
                response = await client.post(self.audio_url, content=wav(), headers=headers)
                self.assertEqual(response.status_code, 422)
                headers["Content-Type"] = "audio/mpeg"
                response = await client.post(self.audio_url, content=wav(), headers=headers)
                self.assertEqual(response.status_code, 422)

        asyncio.run(scenario())
        self.assertEqual(self.speech.calls, 0)

    def test_upload_body_mismatch_and_existing_turn_replay_still_require_authority(self) -> None:
        conversation = self.conversation()
        first = self.upload()
        self.assertEqual(self.upload(wav(2)).status_code, 409)
        audio = first.json()["audio_input_id"]
        accepted = self.audio_turn(conversation, audio)
        self.assertEqual(accepted.status_code, 202)
        self.store.data[lifecycle_account_path(self.uid)] = {"state": "pending"}
        self.assertIn(self.audio_turn(conversation, audio).status_code, (401, 403, 404))
        self.assertEqual(self.speech.calls, 1)

    def test_disabled_service_and_text_audio_validation(self) -> None:
        self.app.dependency_overrides[get_auth_services] = lambda: AuthServices(
            conversations=self.conversations
        )
        self.assertEqual(self.upload().status_code, 503)
        conversation = self.conversation()
        for body in (
            {"text": "x", "audio_input_id": str(uuid4())},
            {"text": None},
            {"audio_input_id": None},
        ):
            response = self.client.post(
                self.url + f"/{conversation}/turns",
                json={**body, "output_mode": "text"},
                headers={"Idempotency-Key": "invalid-turn-key-001"},
            )
            self.assertEqual(response.status_code, 422)


class AudioStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_stops_before_reading_remaining_chunks(self) -> None:
        consumed = 0

        async def chunks() -> Any:
            nonlocal consumed
            for _ in range(7):
                consumed += 1
                yield b"x" * (1024 * 1024)

        with self.assertRaises(APIError) as error:
            await receive_audio(chunks())
        self.assertEqual(error.exception.code, ErrorCode.PAYLOAD_TOO_LARGE)
        self.assertEqual(consumed, 6)

    async def test_google_rest_serialization_unknown_and_billed_usage(self) -> None:
        requests = []

        async def transport(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            body = json.loads(request.content)
            self.assertEqual(
                body["config"]["explicitDecodingConfig"],
                {"encoding": "LINEAR16", "sampleRateHertz": 16000, "audioChannelCount": 1},
            )
            self.assertEqual(body["configMask"], "*")
            self.assertNotIn("uri", body)
            self.assertEqual(request.url.host, "test-region-speech.googleapis.com")
            return httpx.Response(
                200,
                json={
                    "results": [{"alternatives": [{"transcript": "こんにちは"}]}],
                    "metadata": {"totalBilledDuration": "1.001s"},
                },
            )

        async def token() -> SecretStr:
            return SecretStr("synthetic-only-token")

        provider = GoogleSpeech(
            SpeechSettings(
                project="demo-brainoid",
                location="test-region",
                recognizer="_",
                model="explicit-test-model",
            ),
            token,
            transport=httpx.MockTransport(transport),
        )
        try:
            result = await provider.recognize(wav_request(wav())[0])
            self.assertEqual(result, Recognition("こんにちは", 2))
            self.assertNotIn("こんにちは", repr(result))
        finally:
            await provider.close()
        self.assertEqual(len(requests), 1)

    async def test_google_errors_and_unknown_usage_never_retry_or_expose_payload(self) -> None:
        async def token() -> SecretStr:
            return SecretStr("synthetic-token")

        for status, payload, expected in (
            (429, b"SYNTHETIC-SECRET", ErrorCode.RATE_LIMITED),
            (403, b"SYNTHETIC-SECRET", ErrorCode.TEMPORARILY_UNAVAILABLE),
            (500, b"SYNTHETIC-SECRET", ErrorCode.TEMPORARILY_UNAVAILABLE),
            (200, b"not-json-SYNTHETIC-SECRET", ErrorCode.TEMPORARILY_UNAVAILABLE),
            (200, b"x" * 131073, ErrorCode.TEMPORARILY_UNAVAILABLE),
            (200, b'{"results":[]}', None),
        ):
            with self.subTest(status=status, size=len(payload)):
                calls = 0

                async def transport(
                    request: httpx.Request, status: int = status, payload: bytes = payload
                ) -> httpx.Response:
                    nonlocal calls
                    calls += 1
                    return httpx.Response(status, content=payload)

                provider = GoogleSpeech(
                    SpeechSettings(
                        project="demo-brainoid",
                        location="global",
                        recognizer="_",
                        model="test-model",
                    ),
                    token,
                    transport=httpx.MockTransport(transport),
                )
                try:
                    if expected:
                        with self.assertRaises(APIError) as error:
                            await provider.recognize(wav_request(wav())[0])
                        self.assertEqual(error.exception.code, expected)
                        self.assertNotIn("SECRET", repr(error.exception))
                    else:
                        self.assertEqual(
                            await provider.recognize(wav_request(wav())[0]), Recognition("", None)
                        )
                    self.assertEqual(calls, 1)
                finally:
                    await provider.close()

    async def test_real_adapter_cancellation_and_error_redaction(self) -> None:
        cancelled = False

        async def transport(request: httpx.Request) -> httpx.Response:
            nonlocal cancelled
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled = True
                raise
            raise AssertionError

        async def token() -> SecretStr:
            return SecretStr("synthetic-token")

        provider = GoogleSpeech(
            SpeechSettings(
                project="demo-brainoid", location="global", recognizer="_", model="explicit-test"
            ),
            token,
            transport=httpx.MockTransport(transport),
        )
        try:
            task = asyncio.create_task(provider.recognize(wav_request(wav())[0]))
            await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(cancelled)
        finally:
            await provider.close()


class AudioSettingsTests(unittest.TestCase):
    def test_no_implicit_provider_or_local_paid_calls(self) -> None:
        self.assertEqual(load_settings(LOCAL_ENV).stt_provider, "disabled")
        for env in (LOCAL_ENV, CLOUD_ENV):
            with self.assertRaises(ConfigurationError):
                load_settings({**env, "STT_PROVIDER": "google-v2"})
        with self.assertRaises(ConfigurationError):
            load_settings(
                {
                    **LOCAL_ENV,
                    "STT_PROVIDER": "google-v2",
                    "STT_LOCATION": "global",
                    "STT_MODEL": "test",
                    "STT_RECOGNIZER": "_",
                }
            )


class AudioPurgeTests(DeletionFixture):
    def test_member_purge_erases_only_owned_transcripts(self) -> None:
        own = f"tenants/{self.tenant}/audio_inputs/{uuid4()}"
        other = f"tenants/{self.tenant}/audio_inputs/{uuid4()}"
        self.store.data[own] = {
            "tenant_id": self.tenant,
            "owner_user_id": USER,
            "visibility": "private",
            "transcript": "synthetic-private-audio",
            "created_at": self.now.isoformat(),
        }
        self.store.data[other] = {
            "tenant_id": self.tenant,
            "owner_user_id": OWNER,
            "visibility": "private",
            "transcript": "synthetic-other-audio",
            "created_at": self.now.isoformat(),
        }
        operation = self.accept()
        self.drain(operation, self.tenant)
        self.assertNotIn(own, self.store.data)
        self.assertIn(other, self.store.data)
