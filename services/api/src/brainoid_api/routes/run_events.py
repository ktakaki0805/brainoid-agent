"""Authenticated single-connection SSE; each persisted event is a delivery batch."""

import asyncio
import re
from collections.abc import AsyncGenerator
from dataclasses import dataclass

from brainoid_contracts import ResourceId
from brainoid_contracts.transactions import Transaction
from brainoid_jobs import Job
from brainoid_jobs.models import job_id
from brainoid_jobs.repository import path as job_path
from brainoid_persistence.firestore import PersistenceUnavailable
from brainoid_policy.accounts import AccountUnavailable, account_stamp
from brainoid_policy.models import ResourceKind, ResourceRef, UserSubject
from brainoid_runs.events import EVENT, RunEvent, event_path, expires_at
from brainoid_runs.state import KIND, run_path
from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import StreamingResponse

from brainoid_api.auth.context import AuthenticatedIdentity
from brainoid_api.errors import APIError, ErrorCode
from brainoid_api.repositories.agents import AgentIdentity
from brainoid_api.repositories.conversations import Conversations, policy_call
from brainoid_api.routes.agents import Reader, Session
from brainoid_api.routes.conversations import ERRORS, Repository
from brainoid_api.routes.tenants import _header

router = APIRouter(tags=["runs"])
POLL_SECONDS = 0.1
HEARTBEAT_SECONDS = 15.0


@dataclass(frozen=True)
class Batch:
    event: RunEvent | None
    terminal: bool


def next_batch(
    repo: Conversations,
    identity: AgentIdentity,
    tenant: ResourceId,
    run: ResourceId,
    session: ResourceId | None,
    after: int,
) -> Batch:
    # Reconcile infrastructure-terminal jobs even when no worker commit ran.
    repo.get_run(identity, tenant, run, session)

    def read(tx: Transaction, *, delivery: bool = False) -> Batch:
        doc = tx.get(run_path(tenant, run))
        if doc is None or doc.get("tenant_id") != tenant or doc.get("run_id") != run:
            raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
        job_doc = tx.get(job_path(tenant, job_id(tenant, KIND, run)))
        if job_doc is None:
            raise APIError(ErrorCode.RESOURCE_EXPIRED)
        job = Job.model_validate(job_doc)
        output = job.output
        if output is None:
            raise APIError(ErrorCode.RESOURCE_EXPIRED)
        if isinstance(output, UserSubject):
            try:
                if account_stamp(tx, output.user_id) != job.account_stamps.get(output.user_id):
                    raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
            except AccountUnavailable:
                raise APIError(ErrorCode.RESOURCE_NOT_FOUND) from None
        # Current read authority, not the old generation's model-execution grant.
        _, current = repo.access(tx, identity, tenant, doc["conversation_id"], session)
        destination = policy_call(
            lambda: repo.jobs._policy(tx).resource(
                tenant,
                output,
                ResourceRef(
                    tenant_id=tenant, kind=ResourceKind.CONVERSATION, id=doc["conversation_id"]
                ),
                output=output,
            )
        )
        output_scope = repo.scope(
            destination.principal, None, destination.scope, destination.owner_user_id
        )
        if doc.get("events_version") != 1 or repo.now() >= expires_at(doc):
            raise APIError(ErrorCode.RESOURCE_EXPIRED)
        latest = doc.get("last_event_seq", 0)
        if after > latest:
            raise APIError(ErrorCode.CONFLICT)
        event = None
        if after < latest and not delivery:
            stored = tx.get(event_path(doc, after + 1))
            if stored is None:
                raise APIError(ErrorCode.RESOURCE_EXPIRED)
            event = EVENT.validate_python({k: v for k, v in stored.items() if k != "expires_at"})
            if event.seq != after + 1 or event.run_id != run:
                raise APIError(ErrorCode.RESOURCE_EXPIRED)
        now = repo.now()
        current.ensure_live(now)
        output_scope.ensure_live(now)
        if now >= expires_at(doc):
            raise APIError(ErrorCode.RESOURCE_EXPIRED)
        if isinstance(identity, AuthenticatedIdentity):
            expired = now >= identity.expires_at or now < identity.issued_at
        else:
            expired = now.timestamp() >= identity.exp or now.timestamp() < identity.iat
        if expired:
            raise APIError(ErrorCode.AUTHENTICATION_REQUIRED)
        return Batch(event, doc["status"] in ("completed", "failed") and after >= latest)

    batch = repo.store.run(read)
    # Event retrieval may block. A fresh authority transaction after that I/O is
    # the delivery boundary; never yield a previously authorized buffer wholesale.
    repo.store.run(lambda tx: read(tx, delivery=True))
    return batch


async def stream(
    repo: Conversations,
    identity: AgentIdentity,
    tenant: ResourceId,
    run: ResourceId,
    session: ResourceId | None,
    after: int,
) -> AsyncGenerator[str]:
    heartbeat_at = asyncio.get_running_loop().time() + HEARTBEAT_SECONDS
    while True:
        try:
            batch = await run_in_threadpool(next_batch, repo, identity, tenant, run, session, after)
        except (APIError, PersistenceUnavailable):
            # Headers have already been sent: end without private error payloads.
            # The client reconnects explicitly; preflight then returns 4xx/503.
            return
        if batch.event is not None:
            event = batch.event
            yield f"id: {event.seq}\nevent: {event.type}\ndata: {event.model_dump_json()}\n\n"
            after = event.seq
        elif batch.terminal:
            return
        elif asyncio.get_running_loop().time() >= heartbeat_at:
            yield ": heartbeat\n\n"
            heartbeat_at = asyncio.get_running_loop().time() + HEARTBEAT_SECONDS
        else:
            await asyncio.sleep(POLL_SECONDS)


@router.get(
    "/tenants/{tenant_id}/runs/{run_id}/events",
    operation_id="streamRunEvents",
    response_class=StreamingResponse,
    responses={**ERRORS, 200: {"content": {"text/event-stream": {"schema": {"type": "string"}}}}},
)
async def run_events(
    tenant_id: ResourceId,
    run_id: ResourceId,
    identity: Reader,
    repo: Repository,
    session: Session,
    request: Request,
) -> StreamingResponse:
    value = _header(request, "Last-Event-ID")
    if value is not None and (len(value) > 16 or re.fullmatch(r"[0-9]+", value) is None):
        raise APIError(ErrorCode.INVALID_REQUEST)
    after = int(value or "0")
    try:
        await run_in_threadpool(next_batch, repo, identity, tenant_id, run_id, session, after)
    except PersistenceUnavailable:
        raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE) from None
    return StreamingResponse(
        stream(repo, identity, tenant_id, run_id, session, after),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
