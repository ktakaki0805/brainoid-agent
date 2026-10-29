"""Bounded, memory-only speech admission and short-lived, actor-bound transcripts."""

import asyncio
import hashlib
import struct
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Annotated, Literal, Protocol
from uuid import uuid4

from brainoid_agent_core.types.speech import AudioFormat, SpeechRecognition
from brainoid_contracts import DisplayPolicy, ResourceId, Scope, UtcTimestamp, Visibility
from brainoid_contracts.deletion import fenced_document, purge_epoch
from brainoid_contracts.guards import GuardedTransactions, protect
from brainoid_contracts.transactions import Document, Transaction
from brainoid_policy.models import DeviceSubject
from brainoid_usage import Settlement
from brainoid_usage.ledger import live_run
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from brainoid_api.common.access import AuthorizedScope
from brainoid_api.common.idempotency import Idempotency, MutationResult
from brainoid_api.errors import APIError, ErrorCode
from brainoid_api.repositories.agents import AgentIdentity
from brainoid_api.repositories.conversations import Conversations, ReadResult, policy_call

MAX_BYTES = 5 * 1024 * 1024
MAX_FRAMES = 60 * 16000


class AudioInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    audio_input_id: ResourceId
    transcript: Annotated[str, Field(min_length=1, max_length=8000, repr=False)]
    language: Literal["ja-JP"] = "ja-JP"
    expires_at: UtcTimestamp


@dataclass(frozen=True)
class Recognition:
    text: str = field(repr=False)
    # Absent provider metadata means unknown, never zero.
    billed_seconds: int | None = None


class Recognizer(Protocol):
    async def recognize(self, request: SpeechRecognition) -> Recognition: ...


def wav_request(audio: bytes) -> tuple[SpeechRecognition, int]:
    """Parse the complete RIFF extent; never trust MIME, claimed duration or chunk size."""
    if len(audio) > MAX_BYTES:
        raise APIError(ErrorCode.PAYLOAD_TOO_LARGE)
    if (
        len(audio) < 12
        or audio[:4] != b"RIFF"
        or audio[8:12] != b"WAVE"
        or struct.unpack_from("<I", audio, 4)[0] != len(audio) - 8
    ):
        raise APIError(ErrorCode.VALIDATION_FAILED)
    offset, chunks = 12, 0
    fmt = None
    pcm = None
    while offset < len(audio):
        chunks += 1
        if chunks > 1024 or offset + 8 > len(audio):
            raise APIError(ErrorCode.VALIDATION_FAILED)
        kind, size = struct.unpack_from("<4sI", audio, offset)
        start, end = offset + 8, offset + 8 + size
        if end + (size % 2) > len(audio):
            raise APIError(ErrorCode.VALIDATION_FAILED)
        if kind == b"fmt ":
            if fmt is not None or pcm is not None or size not in (16, 18):
                raise APIError(ErrorCode.VALIDATION_FAILED)
            fmt = struct.unpack_from("<HHIIHH", audio, start)
            if fmt != (1, 1, 16000, 32000, 2, 16) or (
                size == 18 and audio[end - 2 : end] != b"\0\0"
            ):
                raise APIError(ErrorCode.VALIDATION_FAILED)
        elif kind == b"data":
            if pcm is not None or fmt is None or size == 0 or size % 2:
                raise APIError(ErrorCode.VALIDATION_FAILED)
            if size // 2 > MAX_FRAMES:
                raise APIError(ErrorCode.PAYLOAD_TOO_LARGE)
            pcm = audio[start:end]
        offset = end + size % 2
    if pcm is None or not any(pcm):
        raise APIError(ErrorCode.VALIDATION_FAILED)
    return SpeechRecognition(
        audio=pcm, format=AudioFormat(encoding="pcm_s16le", sample_rate_hz=16000), locale="ja-JP"
    ), (len(pcm) + 31999) // 32000


async def receive_audio(stream: AsyncIterator[bytes]) -> bytes:
    buffer = bytearray()
    try:
        async with asyncio.timeout(15):
            async for chunk in stream:
                if len(chunk) > MAX_BYTES - len(buffer):
                    raise APIError(ErrorCode.PAYLOAD_TOO_LARGE)
                buffer.extend(chunk)
        return bytes(buffer)
    except TimeoutError:
        raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE) from None
    finally:
        buffer.clear()


def audio_path(tenant: ResourceId, identity: ResourceId) -> str:
    return f"tenants/{tenant}/audio_inputs/{identity}"


class AudioInputs:
    def __init__(self, conversations: Conversations, provider: Recognizer) -> None:
        self.conversations, self.provider = conversations, provider
        self.store = GuardedTransactions(conversations.store)
        self.now, self.usage = conversations.now, conversations.usage
        self.idempotency = Idempotency(self.store, now=self.now)

    def authority(
        self,
        tx: Transaction,
        identity: AgentIdentity,
        tenant: ResourceId,
        session: ResourceId | None,
    ) -> AuthorizedScope:
        principal, account = self.conversations.principal(tx, identity, tenant, session)
        if "conversation" not in principal.context.actor.allowed_scopes:
            raise APIError(ErrorCode.OPERATION_FORBIDDEN)
        private = principal.user_id is not None
        current = self.conversations.scope(
            principal,
            account,
            Scope(
                visibility=Visibility.PRIVATE if private else Visibility.SHARED,
                display_policy=DisplayPolicy.NEVER if private else DisplayPolicy.SHARED_DISPLAY,
            ),
            principal.user_id,
        )
        protect(tx, lambda: current.ensure_live(self.now()))
        return current

    def saved(
        self,
        tx: Transaction,
        identity: AgentIdentity,
        tenant: ResourceId,
        session: ResourceId | None,
        audio: ResourceId,
        *,
        running: bool = False,
    ) -> Document:
        current = self.authority(tx, identity, tenant, session)
        doc = tx.get(audio_path(tenant, audio))
        if doc is None or doc.get("tenant_id") != tenant or doc.get("binding") != current.binding():
            raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
        doc = fenced_document(tx, tenant, "audio_inputs", audio, doc)
        if doc.get("deleted") is not False or doc.get("purge_epoch") != purge_epoch(tx, tenant):
            raise APIError(ErrorCode.RESOURCE_NOT_FOUND)

        def unexpired() -> None:
            if self.now() >= datetime.fromisoformat(doc["expires_at"]):
                raise APIError(ErrorCode.RESOURCE_EXPIRED)

        protect(tx, unexpired)
        if running:
            quota = policy_call(lambda: live_run(tx, tenant, audio, now=self.now()))

            def execution_live() -> None:
                if self.now() >= datetime.fromisoformat(quota["expires_at"]):
                    raise APIError(ErrorCode.RESOURCE_EXPIRED)

            protect(tx, execution_live)
            if doc["status"] != "processing":
                raise APIError(ErrorCode.CONFLICT)
        return doc

    def resolve(
        self,
        tx: Transaction,
        identity: AgentIdentity,
        tenant: ResourceId,
        session: ResourceId | None,
        audio: ResourceId,
    ) -> str:
        doc = self.saved(tx, identity, tenant, session, audio)
        if doc["status"] != "ready":
            raise APIError(ErrorCode.CONFLICT)
        return AudioInput(
            audio_input_id=audio, transcript=doc["transcript"], expires_at=doc["expires_at"]
        ).transcript

    def result(
        self,
        identity: AgentIdentity,
        tenant: ResourceId,
        session: ResourceId | None,
        audio: ResourceId,
    ) -> ReadResult[AudioInput]:
        def read(tx: Transaction) -> AudioInput:
            text = self.resolve(tx, identity, tenant, session, audio)
            doc = tx.get(audio_path(tenant, audio))
            assert doc is not None
            return AudioInput(audio_input_id=audio, transcript=text, expires_at=doc["expires_at"])

        return self.conversations.current_read(read)

    async def transcribe(
        self,
        identity: AgentIdentity,
        tenant: ResourceId,
        session: ResourceId | None,
        audio: bytes,
        key: str | None,
        disconnected: Callable[[], Awaitable[bool]],
    ) -> AudioInput:
        request, seconds = wav_request(audio)
        candidate = ResourceId(str(uuid4()))

        def authorize(tx: Transaction, replay: MutationResult | None) -> AuthorizedScope:
            current = self.authority(tx, identity, tenant, session)
            if replay is not None:
                assert replay.resource_id is not None
                self.saved(tx, identity, tenant, session, replay.resource_id)
            return current

        def admit(tx: Transaction, current: AuthorizedScope) -> MutationResult:
            principal, _ = self.conversations.principal(tx, identity, tenant, session)
            epoch = purge_epoch(tx, tenant)
            reserve = policy_call(
                lambda: self.usage.prepare_admit(
                    tx,
                    tenant,
                    candidate,
                    user=principal.user_id,
                    device=principal.subject.device_id
                    if isinstance(principal.subject, DeviceSubject)
                    else None,
                    purpose="audio_input",
                    voice_seconds=seconds,
                )
            )
            now = self.now()
            reserve()
            tx.create(
                audio_path(tenant, candidate),
                {
                    "id": candidate,
                    "tenant_id": tenant,
                    "binding": current.binding(),
                    "owner_user_id": principal.user_id,
                    "visibility": current.scope.visibility.value,
                    "created_at": now.isoformat(),
                    "expires_at": (now + timedelta(minutes=15)).isoformat(),
                    "ttl_at": now + timedelta(minutes=15),
                    "status": "processing",
                    "deleted": False,
                    "deletion_epoch": 0,
                    "purge_epoch": epoch,
                },
            )
            return MutationResult(status_code=201, resource_id=candidate)

        receipt = await run_in_threadpool(
            self.idempotency.execute,
            operation="audio_inputs.create",
            key=key,
            body={"sha256": hashlib.sha256(audio).hexdigest()},
            authorize=authorize,
            mutate=admit,
        )
        del audio
        audio_id = receipt.resource_id
        assert audio_id is not None
        if audio_id != candidate:
            return (
                await run_in_threadpool(self.result, identity, tenant, session, audio_id)
            ).release()

        called = False
        actual = Settlement(voice_seconds=0)
        succeeded = False
        task: asyncio.Task[Recognition] | None = None
        try:
            # This check is after durable quota admission and immediately before the call.
            await run_in_threadpool(
                self.store.run,
                lambda tx: self.saved(tx, identity, tenant, session, audio_id, running=True),
            )
            if await disconnected():
                raise APIError(ErrorCode.CONFLICT)
            called = True
            actual = Settlement()
            task = asyncio.create_task(self.provider.recognize(request))
            async with asyncio.timeout(45):
                while not task.done():
                    await asyncio.wait({task}, timeout=0.25)
                    if await disconnected():
                        raise APIError(ErrorCode.CONFLICT)
                    await run_in_threadpool(
                        self.store.run,
                        lambda tx: self.saved(
                            tx, identity, tenant, session, audio_id, running=True
                        ),
                    )
                if task.cancelled():
                    raise APIError(ErrorCode.CONFLICT)
                recognized = task.result()
            actual = Settlement(voice_seconds=recognized.billed_seconds)
            text = recognized.text.strip()
            if not text:
                raise APIError(ErrorCode.VALIDATION_FAILED)
            result = AudioInput(
                audio_input_id=audio_id,
                transcript=text,
                expires_at=self.now() + timedelta(minutes=15),
            )

            def publish(tx: Transaction) -> None:
                doc = self.saved(tx, identity, tenant, session, audio_id, running=True)
                tx.set(
                    audio_path(tenant, audio_id),
                    {**doc, "status": "ready", "transcript": result.transcript},
                )

            await run_in_threadpool(self.store.run, publish)
            guarded = await run_in_threadpool(self.result, identity, tenant, session, audio_id)
            if await disconnected():
                raise APIError(ErrorCode.CONFLICT)
            result = guarded.release()
            succeeded = True
            return result
        except APIError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception:
            raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE) from None
        finally:
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

            # No provider fact after cancellation/error: retain reserved seconds conservatively.
            # Shield cleanup from request cancellation; no private result can be published here.
            def finish() -> None:
                self.usage.settle(
                    tenant, audio_id, "speech", actual if called else Settlement(voice_seconds=0)
                )

                def release(tx: Transaction) -> None:
                    doc = tx.get(audio_path(tenant, audio_id))
                    free = self.usage.prepare_release(tx, tenant, audio_id, abandon=True)
                    free()
                    if not succeeded and doc is not None:
                        tx.set(
                            audio_path(tenant, audio_id),
                            {
                                k: v
                                for k, v in {**doc, "status": "failed"}.items()
                                if k != "transcript"
                            },
                        )

                self.store.run(release)

            await asyncio.shield(run_in_threadpool(finish))

    def expire(
        self, tenant: ResourceId, *, after: str | None = None, limit: int = 100
    ) -> str | None:
        """Bounded sweeper; deploy scheduling is explicit, expiry checks never depend on it."""
        if not 1 <= limit <= 100 or (after is not None and "/" in after):
            raise ValueError("invalid_audio_page")

        def work(tx: Transaction) -> str | None:
            docs = tx.query_page(
                f"tenants/{tenant}/audio_inputs", {"tenant_id": tenant}, after=after, limit=limit
            )
            stale = [
                (path, doc)
                for path, doc in docs
                if self.now() >= datetime.fromisoformat(doc["expires_at"])
            ]
            for path, _ in stale:
                tx.delete(path)
            return docs[-1][0].rsplit("/", 1)[-1] if len(docs) == limit else None

        return self.store.run(work)
