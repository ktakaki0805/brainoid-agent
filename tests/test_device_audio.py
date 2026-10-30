"""VOICE04 / T006 T007 T021: synthetic PCM, daemon ownership and actual local boundary."""

import asyncio
import json
import os
import secrets
import socket
import struct
import tempfile
import time
import unittest
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
from brainoid_contracts import ResourceId
from brainoid_device.audio_backend import BLOCK, AlsaBackend, AudioStream
from brainoid_device.audio_capture import MAX_PCM, pcm_from_wav, wav
from brainoid_device.audio_playback import PlaybackQueue, Sentence
from brainoid_device.audio_routes import audio_routes
from brainoid_device.audio_service import AudioService
from brainoid_device.cloud_client import CloudClient
from brainoid_device.cloud_models import CloudError, Credentials
from brainoid_device.config import CloudSettings, Settings
from brainoid_device.ledger import DeviceStorage, Preferences
from brainoid_device.local_api import COOKIE, LocalBoundary, Session, Sessions
from brainoid_device.owned_child import OwnedChild
from brainoid_device.runtime import Runtime
from brainoid_device.snapshot_models import DeviceSnapshotState, PrivateSession
from brainoid_device.snapshot_sync import SnapshotSync
from brainoid_device.storage import CredentialStore
from brainoid_device.ui_bridge import UIBridge
from starlette.applications import Starlette


def identity() -> ResourceId:
    return ResourceId(str(uuid4()))


async def until(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.005)


class Stream:
    def __init__(self) -> None:
        self.blocks: asyncio.Queue[bytes] = asyncio.Queue()
        self.writes: list[bytes] = []
        self.closed = False
        self.finished = False
        self.fail = False
        self.release: asyncio.Event | None = None

    async def read(self) -> bytes:
        return await self.blocks.get()

    async def write(self, pcm: bytes) -> None:
        if self.fail:
            raise OSError("synthetic device disconnected")
        self.writes.append(pcm)
        if self.release:
            await self.release.wait()

    async def finish(self) -> None:
        self.finished = True

    def abort(self) -> None:
        self.closed = True


class Backend:
    def __init__(self) -> None:
        self.streams: list[Stream] = []
        self.opened = asyncio.Event()
        self.release: asyncio.Event | None = None
        self.ignore_cancel = False
        self.write_release: asyncio.Event | None = None
        self.fail = False
        self.fail_write = False

    async def devices(self) -> list[dict[str, str]]:
        return [
            {"id": "hw:CARD=Synthetic,DEV=0", "direction": direction}
            for direction in ("input", "output")
        ]

    async def open(self, device: str, capture: bool) -> AudioStream:
        stream = Stream()
        stream.release = self.write_release
        stream.fail = self.fail_write
        self.streams.append(stream)
        self.opened.set()
        if self.release:
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                if not self.ignore_cancel:
                    raise
                await self.release.wait()
        if self.fail:
            raise OSError("synthetic permission/device failure")
        return stream


class AudioTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="ba-voice-04-writer-unit-")
        self.root = Path(self.directory.name)
        for name in ("credentials", "state", "static"):
            (self.root / name).mkdir(mode=0o700)
        self.store = CredentialStore(self.root / "credentials")
        self.runtime = Runtime(0.2)
        self.runtime.running = True
        self.storage = DeviceStorage(
            self.root / "state", self.store, cleanup_private=self.runtime.clear_private
        )
        self.clock = float(int(time.time()))
        self.calls: list[httpx.Request] = []
        self.request_entered = asyncio.Event()
        self.request_release: asyncio.Event | None = None
        self.ignore_http_cancel = False
        self.response_code = 200
        self.audio_bytes = wav(b"\x01\x00" * 1600)
        self.cloud = CloudClient(
            CloudSettings("http://127.0.0.1:18777", local_test=True),
            self.runtime,
            self.storage,
            self.store,
            now=lambda: self.clock,
            transport=httpx.MockTransport(self.respond),
        )
        self.tenant, self.device = identity(), identity()
        self.storage.register(self.tenant, self.device)
        self.cloud.journal.credentials = Credentials(
            device_id=self.device,
            tenant_id=self.tenant,
            access_token="synthetic-access-" * 4,
            refresh_token="synthetic-refresh-" * 4,
            expires_in=900,
            token_type="Bearer",
        )
        self.cloud.journal.access_until = self.clock + 900
        self.cloud.journal.family_until = self.clock + 86400
        self.sessions = Sessions()
        self.owner, self.csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self.sessions._sessions[self.owner] = Session(self.csrf, time.monotonic() + 1800)
        self.backend = Backend()
        self.audio = AudioService(self.cloud, self.sessions, self.backend)
        await self.audio.select("hw:CARD=Synthetic,DEV=0", "hw:CARD=Synthetic,DEV=0", self.owner)
        self.runtime.start_task(self.audio.maintain())
        self.run_id = identity()

    async def asyncTearDown(self) -> None:
        if self.request_release:
            self.request_release.set()
        if self.backend.release:
            self.backend.release.set()
        if self.backend.write_release:
            self.backend.write_release.set()
        await self.runtime.close()
        await self.cloud.close()
        self.storage.close()
        self.store.close()
        self.directory.cleanup()

    async def respond(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        self.request_entered.set()
        if self.request_release:
            try:
                await self.request_release.wait()
            except asyncio.CancelledError:
                if not self.ignore_http_cancel:
                    raise
                await self.request_release.wait()
        if self.response_code != 200:
            return httpx.Response(self.response_code)
        if request.method == "POST":
            return httpx.Response(
                201,
                json={
                    "audio_input_id": str(identity()),
                    "transcript": "合成テスト",
                    "language": "ja-JP",
                    "expires_at": self.iso(self.clock + 900),
                },
            )
        return httpx.Response(200, content=self.audio_bytes, headers={"Content-Type": "audio/wav"})

    @staticmethod
    def iso(value: float) -> str:
        return datetime.fromtimestamp(value, UTC).isoformat()

    def event(
        self,
        index: int = 0,
        *,
        speech: ResourceId | None = None,
        expires: float | None = None,
        run: ResourceId | None = None,
    ) -> dict[str, Any]:
        return {
            "run_id": run or self.run_id,
            "seq": index + 1,
            "type": "speech.ready",
            "occurred_at": self.iso(self.clock),
            "payload": {
                "speech_id": speech or identity(),
                "sentence_index": index,
                "expires_at": self.iso(expires or self.clock + 100),
            },
        }

    async def record(self, pcm: bytes = b"\x01\x00" * 100) -> Stream:
        self.audio.start_capture(self.owner, None)
        await until(lambda: self.audio.capture_stream is not None)
        stream = self.backend.streams[-1]
        await stream.blocks.put(pcm)
        await until(lambda: len(self.audio.pcm) == len(pcm))
        return stream

    async def test_finish_stops_ingestion_and_does_not_upload_discard_uploads_zero(self) -> None:
        stream = await self.record()
        self.audio.finish_capture()
        self.assertTrue(stream.closed)
        await stream.blocks.put(b"\x03\x00" * 20)
        await asyncio.sleep(0.02)
        self.assertEqual(len(self.audio.pcm), 200)
        self.assertEqual(self.audio.capture_state, "ready")
        self.assertEqual(self.calls, [])
        self.audio.clear()
        self.assertEqual(self.audio.pcm, b"")
        self.assertEqual(self.calls, [])

    async def test_explicit_send_uses_contract_and_cannot_upload_twice(self) -> None:
        await self.record()
        self.audio.finish_capture()
        result = await self.audio.send(self.owner)
        self.assertEqual(result.transcript, "合成テスト")
        self.assertEqual(len(self.calls), 1)
        request = self.calls[0]
        self.assertTrue(request.url.path.endswith("/audio-inputs"))
        self.assertEqual(request.headers["content-type"], "audio/wav")
        self.assertEqual(len(request.headers["idempotency-key"]), 36)
        self.assertEqual(pcm_from_wav(request.content), b"\x01\x00" * 100)
        self.assertEqual(self.audio.pcm, b"")
        with self.assertRaises(CloudError):
            await self.audio.send(self.owner)
        self.assertEqual(len(self.calls), 1)
        for path in self.root.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"RIFF", path.read_bytes())
                self.assertNotIn("合成テスト".encode(), path.read_bytes())

    async def test_capture_limit_is_exact_60_seconds_of_pcm(self) -> None:
        await self.record(b"\x01\x00" * (BLOCK // 2))
        stream = self.backend.streams[-1]
        for _ in range(MAX_PCM // BLOCK):
            stream.blocks.put_nowait(b"\x01\x00" * (BLOCK // 2))
        await until(lambda: self.audio.capture_state == "ready")
        self.assertEqual(len(self.audio.pcm), MAX_PCM)
        self.assertEqual(len(wav(self.audio.pcm)), MAX_PCM + 44)
        self.assertTrue(stream.closed)
        self.assertEqual(self.calls, [])

    async def test_silence_permission_denial_unplug_and_mute_report_safe_states(self) -> None:
        await self.record(b"\0" * 100)
        self.audio.finish_capture()
        self.assertEqual(self.audio.notice, "audio_silent")
        self.assertEqual(self.audio.pcm, b"")
        self.backend.fail = True
        self.audio.start_capture(self.owner, None)
        await until(lambda: self.audio.notice == "audio_device_failed")
        self.backend.fail = False
        await self.record()
        self.backend.streams[-1].blocks.put_nowait(b"")
        await until(lambda: self.audio.notice == "audio_device_failed")
        self.assertEqual(self.audio.pcm, b"")
        self.storage.save_preferences(Preferences(microphone_muted=True))
        with self.assertRaisesRegex(CloudError, "microphone_muted"):
            self.audio.start_capture(self.owner, None)
        self.assertFalse(self.runtime.resource_failed)

    async def test_missing_samples_times_out_without_blocking_daemon(self) -> None:
        self.audio.start_capture(self.owner, None)
        await until(lambda: self.audio.capture_stream is not None)
        await until(lambda: self.audio.notice is not None)
        self.assertTrue(self.backend.streams[-1].closed)
        self.assertTrue(self.runtime.running)

    async def test_late_open_after_stop_cannot_capture_into_new_generation(self) -> None:
        self.backend.release = asyncio.Event()
        self.backend.ignore_cancel = True
        self.audio.start_capture(self.owner, None)
        await self.backend.opened.wait()
        old = self.backend.streams[-1]
        self.audio.clear()
        self.backend.release.set()
        await until(lambda: old.closed)
        self.assertEqual(self.audio.pcm, b"")
        self.assertIsNone(self.audio.capture_stream)

    async def test_queue_orders_and_deduplicates_live_sentences(self) -> None:
        self.audio.begin_run(self.run_id, None, self.owner)
        second, first = self.event(1), self.event(0)
        self.audio.event(second, None)
        await asyncio.sleep(0.02)
        self.assertEqual(self.calls, [])
        self.audio.event(first, None)
        self.audio.event(first, None)
        await until(lambda: len(self.backend.streams) == 2 and self.backend.streams[-1].finished)
        self.assertEqual(
            [str(call.url).split("/")[-2] for call in self.calls],
            [first["payload"]["speech_id"], second["payload"]["speech_id"]],
        )
        self.audio.event(first, None)
        await asyncio.sleep(0.02)
        self.assertEqual(len(self.calls), 2)

    async def test_stop_during_playback_erases_queue_without_cancelling_cloud_run(self) -> None:
        self.backend.write_release = asyncio.Event()
        self.audio.begin_run(self.run_id, None, self.owner)
        self.audio.event(self.event(), None)
        self.audio.event(self.event(1), None)
        await until(lambda: self.audio.playback_state == "playing")
        stream = self.backend.streams[-1]
        self.audio.clear()
        self.assertTrue(stream.closed)
        self.assertEqual(self.audio.playback_pcm, b"")
        self.assertEqual(self.audio.queue.pending, {})
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(all(call.method == "GET" for call in self.calls))

    async def test_playback_failure_is_bounded_and_clears_pending_audio(self) -> None:
        self.backend.fail_write = True
        self.audio.begin_run(self.run_id, None, self.owner)
        self.audio.event(self.event(), None)
        self.audio.event(self.event(1), None)
        await until(lambda: self.audio.notice == "audio_device_failed")
        self.assertTrue(self.backend.streams[-1].closed)
        self.assertEqual(self.audio.queue.pending, {})
        self.assertEqual(len(self.calls), 1)

    async def test_expiry_of_pending_or_playing_sentence_clears_immediately(self) -> None:
        self.backend.write_release = asyncio.Event()
        self.audio.begin_run(self.run_id, None, self.owner)
        self.audio.event(self.event(expires=self.clock + 1), None)
        await until(lambda: self.audio.playback_state == "playing")
        self.clock += 1
        await until(lambda: self.audio.binding is None)
        self.assertTrue(self.backend.streams[-1].closed)
        self.assertEqual(self.audio.queue.pending, {})

    async def test_late_tts_after_stop_or_run_replacement_never_opens_speaker(self) -> None:
        self.request_release = asyncio.Event()
        self.ignore_http_cancel = True
        self.audio.begin_run(self.run_id, None, self.owner)
        self.audio.event(self.event(), None)
        await self.request_entered.wait()
        self.audio.begin_run(identity(), None, self.owner)
        self.request_release.set()
        await asyncio.sleep(0.04)
        self.assertEqual(self.backend.streams, [])
        self.assertEqual(self.audio.playback_state, "idle")

    async def test_late_upload_after_discard_is_not_published(self) -> None:
        await self.record()
        self.audio.finish_capture()
        self.request_release = asyncio.Event()
        self.ignore_http_cancel = True
        task = asyncio.create_task(self.audio.send(self.owner))
        await self.request_entered.wait()
        self.audio.clear()
        self.request_release.set()
        with self.assertRaises((CloudError, asyncio.CancelledError)):
            await task
        self.assertEqual(self.audio.pcm, b"")
        self.assertEqual(self.audio.capture_state, "idle")

    async def test_output_disconnect_and_logout_clear_capture_synchronously(self) -> None:
        stream = await self.record()
        self.cloud.clear_output()
        self.assertTrue(stream.closed)
        self.assertEqual(self.audio.pcm, b"")
        await until(lambda: not self.audio.tasks)
        stream = await self.record()
        self.sessions.remove(self.owner)
        self.assertTrue(stream.closed)
        self.assertEqual(self.audio.pcm, b"")

    async def test_local_session_expiry_stops_without_another_http_request(self) -> None:
        stream = await self.record()
        self.sessions._sessions[self.owner] = Session(self.csrf, time.monotonic())
        await until(lambda: stream.closed)
        self.assertIsNone(self.audio.binding)

    async def test_snapshot_private_expiry_and_replacement_block_old_audio(self) -> None:
        sync = SnapshotSync(self.cloud)
        session_id = identity()
        private = PrivateSession.model_validate(
            {
                "id": session_id,
                "tenant_id": self.tenant,
                "device_id": self.device,
                "version": 1,
                "user_id": "synthetic-user",
                "allowed_scopes": ["conversation"],
                "status": "active",
                "created_at": self.iso(self.clock),
                "updated_at": self.iso(self.clock),
                "expires_at": self.iso(self.clock + 30),
                "idle_expires_at": self.iso(self.clock + 1),
            }
        )
        sync.state = DeviceSnapshotState.model_validate(
            {
                "device_id": self.device,
                "tenant_id": self.tenant,
                "connection_generation": 1,
                "snapshot_id": "a" * 64,
                "active_private_session": private,
                "assigned_runs": [],
                "pending_command_ids": [],
                "pending_notification_ids": [],
                "delivery_cursor": 0,
                "next_cursor": None,
            }
        )
        sync.ready = True
        self.request_release = asyncio.Event()
        self.ignore_http_cancel = True
        self.audio.begin_run(self.run_id, session_id, self.owner)
        self.audio.event(self.event(), session_id)
        await self.request_entered.wait()
        self.clock += 1
        self.assertIsNone(sync.session())
        self.assertIsNone(self.audio.binding)
        self.request_release.set()
        await asyncio.sleep(0.03)
        self.assertEqual(self.backend.streams, [])
        self.assertEqual(self.calls[0].headers["x-private-session"], session_id)

    async def test_cloud_failure_is_not_retried_or_persisted(self) -> None:
        self.response_code = 503
        await self.record()
        self.audio.finish_capture()
        with self.assertRaises(CloudError):
            await self.audio.send(self.owner)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.audio.notice, "audio_cloud_refused")
        self.assertEqual(self.audio.pcm, b"")

    async def test_runtime_shutdown_fences_audio_and_closes_stream(self) -> None:
        stream = await self.record()
        await self.runtime.close()
        self.assertTrue(stream.closed)
        self.assertEqual(self.audio.pcm, b"")
        self.assertFalse(self.audio.tasks)

    async def test_local_routes_keep_loopback_origin_session_csrf_and_typed_body(self) -> None:
        settings = Settings(
            "127.0.0.1", 18778, self.root / "static", self.root / "credentials", self.root / "state"
        )
        app = LocalBoundary(Starlette(routes=audio_routes(self.audio)), settings)
        headers = {
            "Origin": settings.origin,
            "Cookie": f"{COOKIE}={self.owner}",
            "X-CSRF-Token": self.csrf,
        }
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=settings.origin
        ) as browser:
            for changed, expected in (
                ({"Origin": "https://foreign.invalid"}, 403),
                ({"X-CSRF-Token": ""}, 403),
                ({"Cookie": ""}, 401),
            ):
                result = await browser.post(
                    "/local/audio", headers={**headers, **changed}, json={"operation": "start"}
                )
                self.assertEqual(result.status_code, expected)
            result = await browser.post(
                "/local/audio",
                headers=headers,
                json={"operation": "start", "url": "https://foreign"},
            )
            self.assertEqual(result.status_code, 400)
            result = await browser.post(
                "/local/audio", headers=headers, json={"operation": "start"}
            )
            self.assertEqual(result.status_code, 200)
            await until(lambda: self.audio.capture_stream is not None)
            # A fresh UI request owns no microphone resource; existing daemon capture survives.
            state = await browser.get("/local/audio", headers=headers)
            self.assertEqual(state.json()["capture"], "recording")
            self.assertNotIn("pcm", state.text)
            stopped = await browser.post(
                "/local/audio", headers=headers, json={"operation": "stop"}
            )
            self.assertEqual(stopped.json(), {"stopped": True, "run_cancelled": False})
            self.assertTrue(self.backend.streams[-1].closed)
            devices = await browser.get("/local/audio/devices", headers=headers)
            self.assertEqual(len(devices.json()["devices"]), 2)
            query = await browser.get("/local/audio?url=bad", headers=headers)
            self.assertEqual(query.status_code, 400)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("192.0.2.1", 5000)),
            base_url=settings.origin,
        ) as browser:
            self.assertEqual((await browser.get("/local/audio", headers=headers)).status_code, 403)

    async def test_live_sse_drives_daemon_but_committed_replay_does_not(self) -> None:
        event = self.event()
        wire = (f"id: 1\nevent: speech.ready\ndata: {json.dumps(event)}\n\n").encode()

        class Live(httpx.AsyncByteStream):
            async def __aiter__(self) -> AsyncIterator[bytes]:
                yield wire
                await asyncio.Event().wait()

        async def events(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/events"):
                return httpx.Response(
                    200, stream=Live(), headers={"Content-Type": "text/event-stream"}
                )
            return await self.respond(request)

        bridge = UIBridge(self.cloud, self.sessions, self.audio)
        self.audio.begin_run(self.run_id, None, self.owner)
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(8)
        with patch.object(self.cloud.http, "_transport", httpx.MockTransport(events)):
            task = asyncio.create_task(bridge.events(self.run_id, None, queue))
            await until(lambda: len(self.backend.streams) == 1 and self.backend.streams[0].finished)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.cloud.journal.cursors[0].seq, 1)
        self.assertEqual(len(self.calls), 1)
        self.assertIn(b'"type":"speech.ready"', queue.get_nowait() or b"")
        with patch.object(
            bridge,
            "history",
            AsyncMock(
                return_value={
                    "run": {"status": "completed", "last_event_seq": 1},
                    "play_speech": False,
                }
            ),
        ):
            await bridge.events(self.run_id, None, queue)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.backend.streams), 1)
        self.assertIn(b'"play_speech":false', queue.get_nowait() or b"")

    async def test_late_turn_receipt_cannot_override_new_audio_intention(self) -> None:
        for action in ("stop", "discard", "select", "start", "new_turn"):
            with self.subTest(action=action):
                await self.turn_receipt_race(action)

    async def turn_receipt_race(self, action: str) -> None:
        bridge = UIBridge(self.cloud, self.sessions, self.audio)
        settings = Settings(
            "127.0.0.1", 18778, self.root / "static", self.root / "credentials", self.root / "state"
        )
        app = LocalBoundary(Starlette(routes=bridge.routes() + audio_routes(self.audio)), settings)
        headers = {
            "Origin": settings.origin,
            "Cookie": f"{COOKIE}={self.owner}",
            "X-CSRF-Token": self.csrf,
        }
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=settings.origin
        ) as browser:
            self.audio.clear()
            await until(lambda: not self.audio.tasks)
            entered, release = asyncio.Event(), asyncio.Event()
            next_run = identity()

            async def delayed(operation: Any) -> dict[str, Any]:
                if operation.turn.text == "old":
                    entered.set()
                    await release.wait()
                    return {"run_id": self.run_id, "status": "queued"}
                return {"run_id": next_run, "status": "queued"}

            def turn(text: str) -> dict[str, Any]:
                return {
                    "operation": "create_turn",
                    "operation_id": identity(),
                    "conversation_id": identity(),
                    "turn": {"text": text, "output_mode": "speech"},
                }

            with patch.object(bridge, "operation", side_effect=delayed):
                pending = asyncio.create_task(
                    browser.post("/local/cloud/operations", headers=headers, json=turn("old"))
                )
                try:
                    await asyncio.wait_for(entered.wait(), 2)
                    if action == "new_turn":
                        changed = await browser.post(
                            "/local/cloud/operations", headers=headers, json=turn("new")
                        )
                    else:
                        control = {"operation": action}
                        if action == "select":
                            control.update(
                                input_device="hw:CARD=Synthetic,DEV=0",
                                output_device="hw:CARD=Synthetic,DEV=0",
                            )
                        changed = await browser.post("/local/audio", headers=headers, json=control)
                    self.assertEqual(changed.status_code, 200)
                    if action == "start":
                        await until(lambda: self.audio.capture_stream is not None)
                    generation, binding = self.audio.generation, self.audio.binding
                    release.set()
                    result = await asyncio.wait_for(pending, 2)
                    # The accepted cloud run remains an honest receipt, never cancelled.
                    self.assertEqual(result.status_code, 200)
                    self.assertEqual(result.json()["run_id"], self.run_id)
                    self.assertEqual(result.json()["status"], "queued")
                    self.assertEqual(self.audio.generation, generation)
                    self.assertIs(self.audio.binding, binding)
                    self.audio.event(self.event(), None)
                    await asyncio.sleep(0.01)
                    self.assertEqual(self.calls, [])
                    if action == "start":
                        self.assertEqual(self.audio.capture_state, "recording")
                    elif action == "new_turn":
                        self.assertEqual(self.audio.run_id, next_run)
                    else:
                        self.assertIsNone(self.audio.binding)
                finally:
                    release.set()
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)

    async def test_sse_interruption_clears_audio_and_reconnects_text_without_backlog_speech(
        self,
    ) -> None:
        for interruption in ("read_error", "eof", "reload"):
            with self.subTest(interruption=interruption):
                await self.sse_interruption(interruption)

    async def sse_interruption(self, interruption: str) -> None:
        self.audio.clear()
        await until(lambda: not self.audio.tasks)
        self.run_id = identity()
        self.calls.clear()
        self.backend.streams.clear()
        self.backend.write_release = asyncio.Event()
        fixture = self
        connected = 0
        cut, opened = asyncio.Event(), asyncio.Event()
        first, queued, missed = self.event(0), self.event(1), self.event(2)
        text_event = {
            "run_id": self.run_id,
            "seq": 4,
            "type": "text.delta",
            "occurred_at": self.iso(self.clock),
            "payload": {"text": "synthetic restored text"},
        }

        def wire(event: dict[str, Any]) -> bytes:
            return (
                f"id: {event['seq']}\nevent: {event['type']}\ndata: {json.dumps(event)}\n\n"
            ).encode()

        class First(httpx.AsyncByteStream):
            async def __aiter__(self) -> AsyncIterator[bytes]:
                yield wire(first) + wire(queued)
                await until(lambda: fixture.audio.playback_state == "playing")
                opened.set()
                await cut.wait()
                fixture.clock += 10
                if interruption == "read_error":
                    raise httpx.ReadError("synthetic disconnect")

        class Resumed(httpx.AsyncByteStream):
            async def __aiter__(self) -> AsyncIterator[bytes]:
                yield wire(missed) + wire(text_event)
                await asyncio.Event().wait()

        async def respond(request: httpx.Request) -> httpx.Response:
            nonlocal connected
            if request.url.path.endswith("/events"):
                connected += 1
                return httpx.Response(
                    200,
                    stream=First() if connected == 1 else Resumed(),
                    headers={"Content-Type": "text/event-stream"},
                )
            return await self.respond(request)

        bridge = UIBridge(self.cloud, self.sessions, self.audio)
        self.audio.begin_run(self.run_id, None, self.owner)
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(8)
        history = {"run": {"status": "running", "last_event_seq": 2}, "play_speech": False}
        with (
            patch.object(self.cloud.http, "_transport", httpx.MockTransport(respond)),
            patch.object(bridge, "history", AsyncMock(return_value=history)),
        ):
            task = asyncio.create_task(bridge.events(self.run_id, None, queue))
            try:
                await asyncio.wait_for(opened.wait(), 2)
                self.assertEqual(len(self.audio.queue.pending), 1)
                stream = self.backend.streams[0]
                if interruption == "reload":
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    self.assertIsNone(self.audio.binding)
                    task = asyncio.create_task(bridge.events(self.run_id, None, queue))
                else:
                    cut.set()
                await until(
                    lambda: any(
                        cursor.run_id == self.run_id and cursor.seq == 4
                        for cursor in self.cloud.journal.cursors
                    )
                )
                self.assertEqual(connected, 2)
                self.assertTrue(stream.closed)
                self.assertIsNone(self.audio.binding)
                self.assertEqual(self.audio.queue.pending, {})
                self.assertEqual(self.audio.playback_pcm, b"")
                self.assertEqual(len(self.calls), 1)
                self.assertEqual(len(self.backend.streams), 1)
                messages = []
                while not queue.empty():
                    messages.append(queue.get_nowait() or b"")
                self.assertIn(b"synthetic restored text", b"".join(messages))
            finally:
                cut.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                self.backend.write_release.set()

    async def test_pending_gap_expiry_draft_expiry_and_clock_rollback(self) -> None:
        self.audio.begin_run(self.run_id, None, self.owner)
        self.audio.event(self.event(1, expires=self.clock + 1), None)
        self.clock += 1
        await until(lambda: self.audio.binding is None)
        self.assertEqual(self.calls, [])
        await self.record()
        self.audio.finish_capture()
        self.clock += 120
        await until(lambda: self.audio.binding is None)
        self.assertEqual(self.audio.pcm, b"")
        await self.record()
        self.clock -= 1
        await until(lambda: self.audio.binding is None)
        self.assertEqual(self.audio.pcm, b"")

    async def test_failed_or_cancelled_run_stops_current_audio_only(self) -> None:
        self.audio.begin_run(self.run_id, None, self.owner)
        self.audio.event({"run_id": identity(), "type": "run.cancelled"}, None)
        self.assertIsNotNone(self.audio.binding)
        self.audio.event({"run_id": self.run_id, "type": "run.cancelled"}, None)
        self.assertIsNone(self.audio.binding)
        self.audio.event(self.event(), None)
        self.assertEqual(self.calls, [])

    async def test_mute_change_and_credential_revocation_stop_current_capture(self) -> None:
        stream = await self.record()
        self.storage.save_preferences(Preferences(microphone_muted=True))
        await until(lambda: stream.closed)
        self.storage.save_preferences(Preferences(microphone_muted=False))
        stream = await self.record()
        self.cloud.stop()
        self.assertTrue(stream.closed)
        self.assertEqual(self.audio.pcm, b"")

    def voice_bridge(
        self, *, created: float | None = None, seq: int = 10
    ) -> tuple[UIBridge, dict[str, Any], dict[str, Any]]:
        conversation_id = identity()
        state = {
            "ready": True,
            "snapshot": {
                "tenant_id": self.tenant,
                "device_id": self.device,
                "active_private_session": None,
                "assigned_runs": [
                    {
                        "run_id": self.run_id,
                        "conversation_id": conversation_id,
                        "output_mode": "speech",
                        "assignment_generation": 1,
                    }
                ],
            },
        }
        run = {
            "run_id": self.run_id,
            "conversation_id": conversation_id,
            "status": "running",
            "created_at": self.iso(created if created is not None else self.clock + 1),
            "last_event_seq": seq,
        }
        conversation = {
            "id": conversation_id,
            "tenant_id": self.tenant,
            "visibility": "shared",
            "display_policy": "shared_display",
        }

        async def operation(value: Any) -> dict[str, Any]:
            return run if value.operation == "get_run" else conversation

        bridge = UIBridge(self.cloud, self.sessions, self.audio)
        patcher = patch.object(bridge, "operation", operation)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cloud.recovery = cast(
            Any,
            SimpleNamespace(
                publication=AsyncMock(return_value=(state, lambda: None)),
                observed_at=self.clock,
                check=lambda *_args: None,
            ),
        )
        return bridge, state, run

    async def test_ui_voice_post_routes_enforce_csrf_context_and_explicit_intent(self) -> None:
        bridge, state, _ = self.voice_bridge()
        state["snapshot"]["assigned_runs"] = []
        settings = Settings(
            "127.0.0.1", 18778, self.root / "static", self.root / "credentials", self.root / "state"
        )
        app = LocalBoundary(Starlette(routes=bridge.routes()), settings)
        headers = {
            "Origin": settings.origin,
            "Cookie": f"{COOKIE}={self.owner}",
            "X-CSRF-Token": self.csrf,
        }
        path = f"/local/cloud/runs/{self.run_id}/events"
        calls: list[Any] = []

        async def events(run_id: Any, session: Any, queue: Any, speech: Any) -> None:
            calls.append((run_id, session, speech))
            queue.put_nowait(b'event: run\ndata: {"type":"speech.ready","seq":1}\n\n')

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=settings.origin
        ) as browser:
            for route, body in (("/local/cloud/audio-wait", {}), (path, {"mode": "future"})):
                rejected = await browser.post(
                    route, headers={**headers, "X-CSRF-Token": "wrong"}, json=body
                )
                self.assertEqual(rejected.status_code, 403)
                self.assertIsNone(self.audio.waiting)
            accepted = await browser.post("/local/cloud/audio-wait", headers=headers, json={})
            self.assertEqual(accepted.status_code, 200)
            self.assertTrue(self.audio.state(self.owner)["waiting"])
            with patch.object(bridge, "events", events):
                mismatch = await browser.post(
                    path, headers=headers, json={"mode": "future", "private_session_id": identity()}
                )
                self.assertEqual(mismatch.status_code, 409)
                self.assertEqual(calls, [])
                generation = self.audio.generation
                attached = await browser.post(path, headers=headers, json={"mode": "future"})
                self.assertEqual(attached.status_code, 200)
                self.assertIn("speech.ready", attached.text)
                self.assertEqual(
                    calls, [(self.run_id, None, (self.owner, generation + 1, "future"))]
                )
                self.assertIsNone(self.audio.waiting)

    async def test_ui_wait_expiry_and_wrong_local_owner_cannot_consume_intent(self) -> None:
        bridge, state, run = self.voice_bridge()
        state["snapshot"]["assigned_runs"] = []
        await bridge.wait_for_speech(self.owner, None)
        state["snapshot"]["assigned_runs"] = [
            {
                "run_id": self.run_id,
                "conversation_id": run["conversation_id"],
                "output_mode": "speech",
            }
        ]
        with self.assertRaises(CloudError):
            await bridge.attach_speech(
                self.run_id, None, "wrong-owner", self.audio.generation, "waiting"
            )
        self.clock += 61
        with self.assertRaises(CloudError):
            await bridge.attach_speech(
                self.run_id, None, self.owner, self.audio.generation, "waiting"
            )
        self.assertIsNone(self.audio.run_id)

    async def test_ui_future_attach_skips_past_seq_and_starts_at_next_sentence_index(self) -> None:
        bridge, _, _ = self.voice_bridge()
        await bridge.attach_speech(self.run_id, None, self.owner, self.audio.generation, "future")
        old = self.event(4)
        old["seq"] = 10
        self.audio.event(old, None)
        await asyncio.sleep(0.02)
        self.assertFalse(self.calls)
        new = self.event(5)
        new["seq"] = 11
        self.audio.event(new, None)
        await until(lambda: bool(self.backend.streams) and self.backend.streams[-1].finished)
        self.audio.event(new, None)
        await asyncio.sleep(0.02)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.audio.queue.next_index, 6)
        self.audio.clear()
        self.audio.event(self.event(6), None)
        self.assertIsNone(self.audio.run_id)

    async def test_ui_attach_rejects_wrong_assignment_output_and_canonical_conversation(
        self,
    ) -> None:
        for variant in ("mode", "session", "conversation", "terminal"):
            with self.subTest(variant=variant):
                bridge, state, run = self.voice_bridge()
                assignment = state["snapshot"]["assigned_runs"][0]
                if variant == "mode":
                    assignment["output_mode"] = "text"
                if variant == "session":
                    assignment["private_session_id"] = identity()
                if variant == "conversation":
                    run["conversation_id"] = identity()
                if variant == "terminal":
                    run["status"] = "completed"
                with self.assertRaises(CloudError):
                    await bridge.attach_speech(
                        self.run_id, None, self.owner, self.audio.generation, "future"
                    )
                self.assertIsNone(self.audio.run_id)

    async def test_ui_wait_uses_complete_server_boundary_not_pi_clock_or_old_assignments(
        self,
    ) -> None:
        bridge, state, run = self.voice_bridge()
        # Baseline must exclude the future run. Device time is deliberately unrelated.
        state["snapshot"]["assigned_runs"] = []
        cast(Any, self.cloud.recovery).observed_at = self.clock + 500
        await bridge.wait_for_speech(self.owner, None)
        self.assertTrue(self.audio.state(self.owner)["waiting"])
        state["snapshot"]["assigned_runs"] = [
            {
                "run_id": self.run_id,
                "conversation_id": run["conversation_id"],
                "output_mode": "speech",
            }
        ]
        for created in (self.clock, self.clock + 500):
            run["created_at"] = self.iso(created)
            with self.assertRaises(CloudError):
                await bridge.attach_speech(
                    self.run_id, None, self.owner, self.audio.generation, "waiting"
                )
        run["created_at"] = self.iso(self.clock + 501)
        await bridge.attach_speech(self.run_id, None, self.owner, self.audio.generation, "waiting")
        self.assertEqual(self.audio.run_id, self.run_id)
        self.assertIsNone(self.audio.waiting)
        self.assertEqual(self.audio.after_seq, 0)
        self.audio.clear()
        cast(Any, self.cloud.recovery).observed_at = None
        with self.assertRaises(CloudError):
            await bridge.wait_for_speech(self.owner, None)
        self.assertIsNone(self.audio.waiting)

    async def test_ui_wait_baseline_existing_run_is_never_a_new_assignment(self) -> None:
        bridge, _, _ = self.voice_bridge(created=self.clock + 100)
        await bridge.wait_for_speech(self.owner, None)
        with self.assertRaises(CloudError):
            await bridge.attach_speech(
                self.run_id, None, self.owner, self.audio.generation, "waiting"
            )
        self.audio.clear()

    async def test_ui_late_wait_and_attach_cannot_survive_stop_or_new_intent(self) -> None:
        for kind in ("wait", "attach"):
            with self.subTest(kind=kind):
                bridge, state, _ = self.voice_bridge()
                entered, release = asyncio.Event(), asyncio.Event()

                async def publication(
                    entered: asyncio.Event = entered,
                    release: asyncio.Event = release,
                    state: dict[str, Any] = state,
                ) -> Any:
                    entered.set()
                    await release.wait()
                    return state, lambda: None

                cast(Any, self.cloud.recovery).publication = publication
                task = asyncio.create_task(
                    bridge.wait_for_speech(self.owner, None)
                    if kind == "wait"
                    else bridge.attach_speech(
                        self.run_id, None, self.owner, self.audio.generation, "future"
                    )
                )
                await entered.wait()
                self.audio.clear()
                release.set()
                with self.assertRaises(CloudError):
                    await task
                self.assertIsNone(self.audio.run_id)
                self.assertIsNone(self.audio.waiting)

    async def test_ui_explicit_attach_is_consumed_once_and_reconnect_cannot_play_backlog(
        self,
    ) -> None:
        bridge, _, _ = self.voice_bridge(seq=1)
        from brainoid_device.cloud_models import Cursor

        self.cloud.journal.cursors.append(Cursor(run_id=self.run_id, seq=1))
        entered = asyncio.Event()
        event = self.event(3)
        event["seq"] = 2
        wire = b"id: 2\nevent: speech.ready\ndata: " + json.dumps(event).encode() + b"\n\n"

        class Live(httpx.AsyncByteStream):
            async def __aiter__(self) -> AsyncIterator[bytes]:
                yield wire
                entered.set()
                await asyncio.Event().wait()

        async def respond(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/events"):
                return httpx.Response(
                    200, stream=Live(), headers={"Content-Type": "text/event-stream"}
                )
            return await self.respond(request)

        history = {"run": {"status": "running", "last_event_seq": 1}, "play_speech": False}
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(8)
        with (
            patch.object(bridge, "history", AsyncMock(return_value=history)),
            patch.object(self.cloud.http, "_transport", httpx.MockTransport(respond)),
        ):
            task = asyncio.create_task(
                bridge.events(
                    self.run_id, None, queue, (self.owner, self.audio.generation, "future")
                )
            )
            await entered.wait()
            await until(lambda: bool(self.backend.streams) and self.backend.streams[0].finished)
            with self.assertRaises(CloudError):
                await bridge.events(self.run_id, None, queue)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.assertIsNone(self.audio.run_id)
            before = len(self.calls)
            second = asyncio.create_task(bridge.events(self.run_id, None, queue))
            await asyncio.sleep(0.03)
            second.cancel()
            await asyncio.gather(second, return_exceptions=True)
            self.assertEqual(len(self.calls), before)
        self.assertEqual(self.audio.queue.pending, {})


class QueueAndWavTests(unittest.TestCase):
    def test_queue_limits_conflicts_gap_expiry_and_duplicate_identity(self) -> None:
        queue = PlaybackQueue()
        speech = identity()
        queue.add(Sentence(speech, 1, 10), 0)
        self.assertIsNone(queue.take(0))
        for item in (
            Sentence(speech, 2, 10),
            Sentence(identity(), 1, 10),
            Sentence(identity(), 64, 10),
            Sentence(identity(), 0, 0),
        ):
            with self.assertRaises(CloudError):
                queue.add(item, 0)
        with self.assertRaises(CloudError):
            queue.take(10)
        queue.clear()
        self.assertEqual(queue.seen, {})

    def test_wav_complete_extent_format_and_size_bounds(self) -> None:
        value = wav(b"\x01\0" * 32)
        self.assertEqual(pcm_from_wav(value), b"\x01\0" * 32)
        for changed in (value[:-1], value + b"x", b"garbage", value[:24] + b"\0" * 4 + value[28:]):
            with self.assertRaises(CloudError):
                pcm_from_wav(changed)
        for pcm in (b"", b"\0" * 100, b"x", b"\x01\0" * (MAX_PCM // 2 + 1)):
            with self.assertRaises(CloudError):
                wav(pcm)


class NativeAudioTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_unowned_local_connector_before_accepting_its_helper(self) -> None:
        runtime = Runtime(0.5)
        runtime.running = True
        original = runtime.start_child
        rogue = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        rogue.setblocking(False)

        async def start(argv: Sequence[str]) -> OwnedChild:
            await asyncio.get_running_loop().sock_connect(rogue, "\0" + argv[-4])
            await asyncio.get_running_loop().sock_sendall(rogue, b"!")
            return await original(argv)

        try:
            with patch.object(runtime, "start_child", side_effect=start):
                await AlsaBackend(runtime).devices()
            # Refused before reading any attacker-provided PCM or device list.
            try:
                self.assertEqual(await asyncio.get_running_loop().sock_recv(rogue, 1), b"")
            except ConnectionResetError:
                pass
        finally:
            rogue.close()
            await runtime.close()
        self.assertEqual(runtime._children, [])

    async def test_stuck_native_open_has_deadline_and_owned_force_cleanup(self) -> None:
        runtime = Runtime(0.5)
        runtime.running = True
        launch = """
import socket, sys, time, signal
signal.signal(signal.SIGTERM, signal.SIG_IGN)
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect('\\0' + sys.argv[-4])
time.sleep(30)
"""
        started = time.monotonic()
        try:
            with patch("brainoid_device.audio_backend.LAUNCH", launch):
                with self.assertRaises(CloudError):
                    await AlsaBackend(runtime).open("hw:CARD=Synthetic,DEV=0", True)
        finally:
            await runtime.close()
        self.assertLess(time.monotonic() - started, 6)
        self.assertEqual(runtime._children, [])

    async def test_real_owned_helper_enumerates_and_shuts_down_without_devices(self) -> None:
        runtime = Runtime(0.5)
        runtime.running = True
        backend = AlsaBackend(runtime)
        try:
            values = await backend.devices()
            self.assertTrue(all(item["direction"] in {"input", "output"} for item in values))
            # A deliberately nonexistent CARD exercises the real native open error.
            with self.assertRaises(CloudError):
                await backend.open("hw:CARD=BrainoidAbsent99,DEV=0", True)
            with self.assertRaises(CloudError):
                await backend.open("default", True)
        finally:
            await runtime.close()
        self.assertEqual(runtime._children, [])
        self.assertFalse(runtime.resource_failed)

    async def test_abstract_socket_peer_credentials_are_kernel_supplied(self) -> None:
        first, second = socket.socketpair()
        try:
            pid, uid, _ = struct.unpack(
                "3i", first.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            )
            self.assertEqual((pid, uid), (os.getpid(), os.getuid()))
        finally:
            first.close()
            second.close()
