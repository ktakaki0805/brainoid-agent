"""Authorized, bounded synthesis of immutable run sentence references; memory-only audio."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol
from uuid import uuid4

from brainoid_contracts import ResourceId
from brainoid_contracts.deletion import fenced_document, purge_epoch
from brainoid_contracts.guards import GuardedTransactions, protect
from brainoid_contracts.transactions import Document, Transaction
from brainoid_jobs import Job
from brainoid_jobs.models import job_id
from brainoid_jobs.repository import path as job_path
from brainoid_memory.calendar_access import check_calendar_sources
from brainoid_memory.sources import CanonicalSources
from brainoid_policy.accounts import AccountUnavailable, account_stamp
from brainoid_policy.devices import CredentialBinding, current_family
from brainoid_policy.models import DeviceSubject
from brainoid_runs.memory_sources import SavedMessages
from brainoid_runs.speech import MAX_SENTENCE_BYTES, speech_path
from brainoid_runs.state import KIND, failed_projection, run_path
from brainoid_usage import Settlement
from brainoid_usage.ledger import live_run
from starlette.concurrency import run_in_threadpool

from brainoid_api.audio_inputs import wav_request
from brainoid_api.auth.context import AuthenticatedIdentity
from brainoid_api.errors import APIError, ErrorCode
from brainoid_api.repositories.agents import AgentIdentity
from brainoid_api.repositories.conversations import Conversations, ReadResult, policy_call

MAX_ATTEMPTS = 2
RESERVED_SECONDS = 60


class Synthesizer(Protocol):
    async def synthesize(self, text: str) -> bytes: ...


@dataclass(frozen=True)
class SpeechAudio:
    audio: bytes = field(repr=False)
    # Retained even after settlement releases the execution slot. A later attempt
    # must never extend this response's deadline during the HTTP thread handoff.
    attempt_deadline: datetime


class SpeechOutputs:
    def __init__(self, conversations: Conversations, provider: Synthesizer) -> None:
        self.conversations, self.provider = conversations, provider
        self.store = GuardedTransactions(conversations.store)
        self.now, self.usage = conversations.now, conversations.usage

    def saved(
        self,
        tx: Transaction,
        identity: AgentIdentity,
        tenant: ResourceId,
        session: ResourceId | None,
        speech: ResourceId,
        *,
        attempt: ResourceId | None = None,
        attempt_deadline: datetime | None = None,
    ) -> tuple[Document, str]:
        repo = self.conversations
        doc = tx.get(speech_path(tenant, speech))
        if doc is None or doc.get("tenant_id") != tenant or doc.get("id") != speech:
            raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
        doc = fenced_document(tx, tenant, "speech_outputs", speech, doc)
        if doc.get("deleted") is not False or doc.get("purge_epoch") != purge_epoch(tx, tenant):
            raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
        run_id = ResourceId(doc["run_id"])
        run = tx.get(run_path(tenant, run_id))
        if (
            run is None
            or run.get("tenant_id") != tenant
            or run.get("conversation_id") != doc["conversation_id"]
            or run.get("output_mode") != "speech"
            or failed_projection(tx, run)["status"]
            not in {"running", "waiting_confirmation", "completed"}
        ):
            raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
        stored_job = tx.get(job_path(tenant, job_id(tenant, KIND, run_id)))
        if stored_job is None:
            raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
        job = Job.model_validate(stored_job)
        reader, current = repo.access(tx, identity, tenant, doc["conversation_id"], session)
        if reader.principal.subject != job.output:
            raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
        if (
            reader.scope.model_dump(mode="json") != run["speech_scope"]
            or reader.owner_user_id != doc["owner_user_id"]
        ):
            raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
        policy = repo.jobs._policy(tx)
        origin = policy_call(
            lambda: policy.resource(
                tenant, job.grant.principal.subject, job.grant.ref, output=job.output
            )
        )
        origin_scope = repo.scope(origin.principal, None, origin.scope, origin.owner_user_id)
        for user, stamp in job.account_stamps.items():
            try:
                if account_stamp(tx, user) != stamp:
                    raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
            except AccountUnavailable:
                raise APIError(ErrorCode.RESOURCE_NOT_FOUND) from None
        family_expiry = None
        if run.get("credential") is not None:
            family = policy_call(
                lambda: current_family(
                    tx,
                    CredentialBinding.model_validate(run["credential"]),
                    credential_epoch=repo.credentials.credential_epoch
                    if repo.credentials
                    else None,
                    now=self.now,
                )
            )
            family_expiry = family["expires_at"]
        policy_call(
            lambda: check_calendar_sources(
                tx,
                policy,
                reader.principal,
                run,
                credential_epoch=repo.credentials.credential_epoch if repo.credentials else None,
                sources=CanonicalSources(SavedMessages()),
            )
        )
        parent_quota = None
        if run["status"] != "completed":
            # A crashed worker may leave status=running/waiting indefinitely.
            # Reconciliation is cleanup, never the authority for this deadline.
            parent_quota = policy_call(lambda: live_run(tx, tenant, run_id, now=self.now()))
        quota = None
        if attempt is not None:
            if doc.get("active_attempt") != attempt:
                raise APIError(ErrorCode.CONFLICT)
            quota = policy_call(lambda: live_run(tx, tenant, attempt, now=self.now()))

        def finish() -> None:
            now = self.now()
            current.ensure_live(now)
            origin_scope.ensure_live(now)
            if now >= datetime.fromisoformat(doc["expires_at"]):
                raise APIError(ErrorCode.RESOURCE_EXPIRED)
            if quota is not None and now >= datetime.fromisoformat(quota["expires_at"]):
                raise APIError(ErrorCode.RESOURCE_EXPIRED)
            if attempt_deadline is not None and now >= attempt_deadline:
                raise APIError(ErrorCode.RESOURCE_EXPIRED)
            if parent_quota is not None and now >= datetime.fromisoformat(
                parent_quota["expires_at"]
            ):
                raise APIError(ErrorCode.RESOURCE_EXPIRED)
            if family_expiry is not None and now >= family_expiry:
                raise APIError(ErrorCode.AUTHENTICATION_REQUIRED)
            if isinstance(identity, AuthenticatedIdentity):
                invalid = now >= identity.expires_at or now < identity.issued_at
            else:
                invalid = now.timestamp() >= identity.exp or now.timestamp() < identity.iat
            if invalid:
                raise APIError(ErrorCode.AUTHENTICATION_REQUIRED)

        protect(tx, finish)
        public = run.get("streamed_text", "")
        if not 0 <= doc["start"] < doc["end"] <= len(public):
            raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
        text = public[doc["start"] : doc["end"]].strip()
        if not text or len(text.encode()) > MAX_SENTENCE_BYTES:
            raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
        return doc, text

    def authorize(
        self,
        identity: AgentIdentity,
        tenant: ResourceId,
        session: ResourceId | None,
        speech: ResourceId,
        *,
        attempt_deadline: datetime | None = None,
    ) -> ReadResult[None]:
        def read(tx: Transaction) -> None:
            self.saved(tx, identity, tenant, session, speech, attempt_deadline=attempt_deadline)

        return self.conversations.current_read(read)

    async def synthesize(
        self,
        identity: AgentIdentity,
        tenant: ResourceId,
        session: ResourceId | None,
        speech: ResourceId,
        disconnected: Callable[[], Awaitable[bool]],
    ) -> SpeechAudio:
        attempt = ResourceId(str(uuid4()))

        def admit(tx: Transaction) -> tuple[str, datetime]:
            doc, text = self.saved(tx, identity, tenant, session, speech)
            if doc["attempt_count"] >= MAX_ATTEMPTS:
                raise APIError(ErrorCode.RATE_LIMITED)
            if doc.get("active_attempt") is not None and self.now() < datetime.fromisoformat(
                doc["attempt_expires_at"]
            ):
                raise APIError(ErrorCode.CONFLICT)
            principal, _ = self.conversations.principal(tx, identity, tenant, session)
            # Capture before quota admission so this bound cannot be later than
            # the durable quota deadline, including transaction work/handoff.
            deadline = self.now() + timedelta(seconds=60)

            def unexpired() -> None:
                if self.now() >= deadline:
                    raise APIError(ErrorCode.RESOURCE_EXPIRED)

            protect(tx, unexpired)
            reserve = policy_call(
                lambda: self.usage.prepare_admit(
                    tx,
                    tenant,
                    attempt,
                    user=principal.user_id,
                    device=principal.subject.device_id
                    if isinstance(principal.subject, DeviceSubject)
                    else None,
                    purpose="speech_output",
                    voice_seconds=RESERVED_SECONDS,
                )
            )
            reserve()
            tx.set(
                speech_path(tenant, speech),
                {
                    **doc,
                    "attempt_count": doc["attempt_count"] + 1,
                    "active_attempt": attempt,
                    "attempt_expires_at": deadline.isoformat(),
                },
            )
            return text, deadline

        # An ambiguous commit consumes an attempt and quota, but never calls the provider.
        text, deadline = await run_in_threadpool(self.store.run, admit)
        called = False
        actual = Settlement(voice_seconds=0)
        task: asyncio.Task[bytes] | None = None
        audio: bytes | None = None
        try:
            await run_in_threadpool(
                self.store.run,
                lambda tx: self.saved(
                    tx,
                    identity,
                    tenant,
                    session,
                    speech,
                    attempt=attempt,
                    attempt_deadline=deadline,
                ),
            )
            if await disconnected():
                raise APIError(ErrorCode.CONFLICT)
            called, actual = True, Settlement()
            task = asyncio.create_task(self.provider.synthesize(text))
            del text
            async with asyncio.timeout(45):
                while not task.done():
                    await asyncio.wait({task}, timeout=0.25)
                    if await disconnected():
                        raise APIError(ErrorCode.CONFLICT)
                    await run_in_threadpool(
                        self.store.run,
                        lambda tx: self.saved(
                            tx,
                            identity,
                            tenant,
                            session,
                            speech,
                            attempt=attempt,
                            attempt_deadline=deadline,
                        ),
                    )
                if task.cancelled():
                    raise APIError(ErrorCode.CONFLICT)
                audio = task.result()
            # Exact output duration is an application usage fact, not Google's price.
            # Malformed/empty/overlong output never becomes a playable 200 response.
            try:
                _, seconds = wav_request(audio)
            except APIError:
                raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE) from None
            actual = Settlement(voice_seconds=seconds)
            await run_in_threadpool(
                self.store.run,
                lambda tx: self.saved(
                    tx,
                    identity,
                    tenant,
                    session,
                    speech,
                    attempt=attempt,
                    attempt_deadline=deadline,
                ),
            )
        except APIError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception:
            raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE) from None
        finally:
            if task is not None and not task.done():
                task.cancel()
                done, _ = await asyncio.wait({task}, timeout=0.25)
                if done:
                    if not task.cancelled():
                        task.exception()
                else:
                    task.add_done_callback(lambda t: None if t.cancelled() else t.exception())

            def finish() -> None:
                self.usage.settle(
                    tenant, attempt, "speech", actual if called else Settlement(voice_seconds=0)
                )

                def release(tx: Transaction) -> None:
                    doc = tx.get(speech_path(tenant, speech))
                    free = self.usage.prepare_release(tx, tenant, attempt, abandon=True)
                    free()
                    if doc is not None and doc.get("active_attempt") == attempt:
                        tx.set(speech_path(tenant, speech), {**doc, "active_attempt": None})

                self.store.run(release)

            await asyncio.shield(run_in_threadpool(finish))
        assert audio is not None
        # Settlement and thread handoff may cross revocation/expiry: a new read is required.
        guarded = await run_in_threadpool(
            self.authorize, identity, tenant, session, speech, attempt_deadline=deadline
        )
        if await disconnected():
            raise APIError(ErrorCode.CONFLICT)
        guarded.release()
        return SpeechAudio(audio, deadline)

    def expire(
        self, tenant: ResourceId, *, after: str | None = None, limit: int = 100
    ) -> str | None:
        """Physical reference cleanup only; usage recovery is a separate worker."""
        if not 1 <= limit <= 100 or (after is not None and (not after or "/" in after)):
            raise ValueError("invalid_speech_page")

        def work(tx: Transaction) -> str | None:
            docs = tx.query_page(
                f"tenants/{tenant}/speech_outputs", {"tenant_id": tenant}, after=after, limit=limit
            )
            for path, doc in docs:
                if self.now() >= datetime.fromisoformat(doc["expires_at"]):
                    tx.delete(path)
            return docs[-1][0].rsplit("/", 1)[-1] if len(docs) == limit else None

        return self.store.run(work)
