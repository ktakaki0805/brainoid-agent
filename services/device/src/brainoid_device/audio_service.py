"""Daemon-owned capture and playback; browser requests only issue typed controls."""

import asyncio
import time
from collections.abc import Coroutine
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4

from brainoid_contracts import ResourceId

from brainoid_device.audio_backend import BLOCK, AudioBackend, AudioStream
from brainoid_device.audio_capture import MAX_PCM, erase, pcm_from_wav, wav
from brainoid_device.audio_cloud import AudioCloud, Transcript
from brainoid_device.audio_playback import PlaybackQueue, Sentence
from brainoid_device.cloud_client import CloudClient
from brainoid_device.cloud_models import CloudError
from brainoid_device.local_api import Sessions


@dataclass(frozen=True)
class Binding:
    owner: str
    session: ResourceId | None
    cloud_generation: int
    output_generation: int
    generation: int
    deadline: float
    monotonic_deadline: float


class AudioService:
    def __init__(self, client: CloudClient, sessions: Sessions, backend: AudioBackend) -> None:
        self.client, self.sessions, self.backend = client, sessions, backend
        self.cloud = AudioCloud(client)
        self.input_device: str | None = None
        self.output_device: str | None = None
        self.generation = 0
        self.binding: Binding | None = None
        self.run_id: ResourceId | None = None
        self.after_seq = 0
        self.first_sentence = False
        self.waiting: tuple[frozenset[str], float] | None = None
        self.capture_state = "idle"
        self.playback_state = "idle"
        self.notice: str | None = None
        self.pcm = bytearray()
        self.playback_pcm = bytearray()
        self.observed_at = client.now()
        self.queue = PlaybackQueue()
        self.capture_stream: AudioStream | None = None
        self.playback_stream: AudioStream | None = None
        self.capture_task: asyncio.Task[None] | None = None
        self.tasks: set[asyncio.Task[None]] = set()
        self.sentence_deadline: float | None = None
        self.changed = asyncio.Event()
        client.runtime.register_private_cleanup(self.clear)
        client.output_cleanup.append(self.clear)
        sessions.cleanup.append(self.session_ended)

    def session_ended(self, owner: str) -> None:
        if self.binding and self.binding.owner == owner:
            self.clear()

    def clear(self) -> None:
        self.generation += 1
        self.binding = None
        self.run_id = None
        self.waiting = None
        self.after_seq = 0
        self.first_sentence = False
        self.capture_state = self.playback_state = "idle"
        self.notice = None
        self.queue.clear()
        self.sentence_deadline = None
        erase(self.pcm)
        erase(self.playback_pcm)
        for stream in (self.capture_stream, self.playback_stream):
            if stream:
                stream.abort()
        self.capture_stream = self.playback_stream = None
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        for task in tuple(self.tasks):
            if task is not current:
                task.cancel()
        self.changed.set()

    def bind(self, owner: str, session: ResourceId | None, seconds: float) -> Binding:
        self.clear()
        if len(self.tasks) >= 4:
            raise CloudError("audio_busy", 429)
        self.client.check_output(session)
        if not self.sessions.valid(owner):
            raise CloudError("session_required", 401)
        binding = Binding(
            owner,
            session,
            self.client.generation,
            self.client.output_generation,
            self.generation,
            self.client.now() + seconds,
            time.monotonic() + seconds,
        )
        self.observed_at = self.client.now()
        self.binding = binding
        return binding

    def check(self, binding: Binding) -> None:
        self.client.check(binding.cloud_generation)
        self.client.check_output(binding.session, binding.output_generation)
        if (
            self.binding != binding
            or self.generation != binding.generation
            or not self.sessions.valid(binding.owner)
            or self.client.now() >= binding.deadline
            or self.client.now() < self.observed_at
            or time.monotonic() >= binding.monotonic_deadline
            or (self.sentence_deadline is not None and self.client.now() >= self.sentence_deadline)
        ):
            raise CloudError("audio_expired", 410)
        self.observed_at = self.client.now()
        preferences = self.client.storage.snapshot().preferences
        if self.capture_state == "recording" and preferences and preferences.microphone_muted:
            raise CloudError("microphone_muted", 403)

    def owned(self, work: Coroutine[Any, Any, None], binding: Binding) -> asyncio.Task[None]:
        async def run() -> None:
            try:
                await work
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self.binding == binding:
                    self.clear()
                    self.notice = exc.code if isinstance(exc, CloudError) else "audio_device_failed"

        task = self.client.runtime.start_task(run())
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        task.add_done_callback(lambda _: work.close())
        return task

    async def maintain(self) -> None:
        while self.client.active:
            binding = self.binding
            if binding:
                try:
                    self.check(binding)
                    if any(
                        item.expires <= self.client.now() for item in self.queue.pending.values()
                    ):
                        raise CloudError("audio_expired", 410)
                except CloudError as exc:
                    self.clear()
                    self.notice = exc.code
            await asyncio.sleep(0.05)

    def state(self, owner: str) -> dict[str, Any]:
        preferences = self.client.storage.snapshot().preferences
        if self.binding:
            try:
                self.check(self.binding)
            except CloudError:
                self.clear()
            if self.binding and self.binding.owner != owner:
                return {"enabled": True, "capture": "busy", "playback": "busy"}
        return {
            "enabled": True,
            "capture": self.capture_state,
            "playback": self.playback_state,
            "notice": self.notice,
            "input_device": self.input_device,
            "output_device": self.output_device,
            "queued": len(self.queue.pending),
            "muted": bool(preferences is None or preferences.microphone_muted),
            "waiting": self.waiting is not None,
        }

    async def select(self, input_device: str, output_device: str, owner: str) -> None:
        self.clear()
        generation = self.generation
        async with asyncio.timeout(4):
            devices = await self.backend.devices()
        self.client.check()
        if generation != self.generation or not self.sessions.valid(owner):
            raise CloudError("audio_context_changed", 409)
        if {"id": input_device, "direction": "input"} not in devices or {
            "id": output_device,
            "direction": "output",
        } not in devices:
            raise CloudError("audio_device_invalid", 400)
        self.input_device, self.output_device = input_device, output_device

    def start_capture(self, owner: str, session: ResourceId | None) -> None:
        preferences = self.client.storage.snapshot().preferences
        if preferences is None or preferences.microphone_muted:
            raise CloudError("microphone_muted", 403)
        if self.input_device is None:
            raise CloudError("audio_device_required", 409)
        binding = self.bind(owner, session, 120)
        self.capture_state = "recording"
        self.capture_task = self.owned(self.record(binding, self.input_device), binding)

    async def record(self, binding: Binding, device: str) -> None:
        stream: AudioStream | None = None
        try:
            async with asyncio.timeout(3):
                stream = await self.backend.open(device, True)
            self.check(binding)
            if self.capture_state != "recording":
                return
            self.capture_stream = stream
            deadline = time.monotonic() + 60
            while self.capture_state == "recording":
                self.check(binding)
                remaining = deadline - time.monotonic()
                if remaining <= 0 or len(self.pcm) >= MAX_PCM:
                    self.finish_capture()
                    self.notice = "capture_limit"
                    return
                try:
                    async with asyncio.timeout(min(2, remaining)):
                        block = await stream.read()
                except TimeoutError:
                    if time.monotonic() >= deadline:
                        self.finish_capture()
                        self.notice = "capture_limit"
                        return
                    raise
                self.check(binding)
                if self.capture_state != "recording":
                    return
                if not block or len(block) > BLOCK:
                    raise CloudError("audio_device_failed", 503)
                self.pcm.extend(block[: MAX_PCM - len(self.pcm)])
        finally:
            if stream:
                stream.abort()
            if self.capture_stream is stream:
                self.capture_stream = None

    def finish_capture(self) -> None:
        if self.capture_state != "recording":
            raise CloudError("audio_not_recording", 409)
        self.capture_state = "ready"
        if self.capture_stream:
            self.capture_stream.abort()
            self.capture_stream = None
        if self.capture_task and self.capture_task is not asyncio.current_task():
            self.capture_task.cancel()
        if len(self.pcm) % 2:
            del self.pcm[-1:]
        if not self.pcm or not any(self.pcm):
            erase(self.pcm)
            self.capture_state = "idle"
            self.notice = "audio_silent"

    async def send(self, owner: str) -> Transcript:
        binding = self.binding
        if binding is None or binding.owner != owner or self.capture_state != "ready":
            raise CloudError("audio_not_ready", 409)
        self.check(binding)
        audio = wav(self.pcm)
        erase(self.pcm)
        self.capture_state = "sending"
        results: list[Transcript] = []

        async def upload() -> None:
            async with asyncio.timeout(60):
                result = await self.cloud.upload(
                    audio, ResourceId(str(uuid4())), binding.session, lambda: self.check(binding)
                )
            self.check(binding)
            self.capture_state = "idle"
            results.append(result)

        task = self.owned(upload(), binding)
        await asyncio.shield(task)
        self.check(binding)
        if not results:
            raise CloudError("audio_cloud_unavailable", 503)
        return results[0]

    def begin_run(
        self,
        run_id: ResourceId,
        session: ResourceId | None,
        owner: str,
        *,
        after_seq: int = 0,
        first_sentence: bool = False,
    ) -> None:
        if self.output_device is None:
            return
        binding = self.bind(owner, session, 300)
        self.run_id = run_id
        self.after_seq = after_seq
        self.first_sentence = first_sentence
        self.owned(self.play(binding, self.output_device), binding)

    def event(self, event: dict[str, Any], session: ResourceId | None) -> None:
        binding = self.binding
        if binding is None or event["run_id"] != self.run_id or session != binding.session:
            return
        try:
            self.check(binding)
            if event["type"] in {"run.cancelled", "run.failed"}:
                self.clear()
            elif event["type"] == "speech.ready" and event["seq"] > self.after_seq:
                payload = event["payload"]
                if self.first_sentence:
                    self.queue.next_index = payload["sentence_index"]
                    self.first_sentence = False
                self.queue.add(
                    Sentence(
                        ResourceId(payload["speech_id"]),
                        payload["sentence_index"],
                        datetime.fromisoformat(payload["expires_at"]).timestamp(),
                    ),
                    self.client.now(),
                )
                self.changed.set()
        except (CloudError, ValueError):
            self.clear()
            self.notice = "audio_expired_or_invalid"

    def stop_run(self, run_id: ResourceId) -> None:
        if self.run_id == run_id:
            self.clear()

    async def play(self, binding: Binding, device: str) -> None:
        while True:
            self.check(binding)
            self.changed.clear()
            sentence = self.queue.take(self.client.now())
            if sentence is None:
                self.playback_state = "idle"
                await self.changed.wait()
                continue
            self.sentence_deadline = sentence.expires
            self.playback_state = "loading"
            async with asyncio.timeout(60):
                audio = await self.cloud.fetch(
                    sentence.identity, binding.session, lambda: self.check(binding)
                )
            self.check(binding)
            pcm = bytearray(pcm_from_wav(audio))
            self.playback_pcm = pcm
            del audio
            stream: AudioStream | None = None
            try:
                async with asyncio.timeout(3):
                    stream = await self.backend.open(device, False)
                self.check(binding)
                self.playback_stream = stream
                self.playback_state = "playing"
                async with asyncio.timeout(65):
                    for offset in range(0, len(pcm), BLOCK):
                        self.check(binding)
                        await stream.write(bytes(pcm[offset : offset + BLOCK]))
                    self.check(binding)
                    await stream.finish()
                    self.check(binding)
            finally:
                erase(pcm)
                if stream:
                    stream.abort()
                if self.playback_stream is stream:
                    self.playback_stream = None
            self.sentence_deadline = None
