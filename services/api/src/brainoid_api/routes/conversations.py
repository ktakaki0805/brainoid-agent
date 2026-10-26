"""Conversation and durable text-turn HTTP contracts."""

from typing import Annotated, Any

from brainoid_contracts import ResourceId
from brainoid_runs.models import (
    Conversation,
    ConversationCreate,
    ConversationPage,
    ConversationPatch,
    MessagePage,
    Run,
    RunAccepted,
    TurnCreate,
)
from fastapi import APIRouter, Depends, Query, Request, Response
from starlette.concurrency import run_in_threadpool

from brainoid_api.auth.runtime import AuthServices, get_auth_services
from brainoid_api.common.versioning import etag
from brainoid_api.errors import APIError, ErrorCode, ErrorResponse
from brainoid_api.repositories.conversations import Conversations
from brainoid_api.routes.agents import Reader, Session
from brainoid_api.routes.tenants import Identity, _header

router = APIRouter(tags=["conversations"])


def repository(services: Annotated[AuthServices, Depends(get_auth_services)]) -> Conversations:
    if services.conversations is None:
        raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)
    return services.conversations


Repository = Annotated[Conversations, Depends(repository)]
Limit = Annotated[int, Query(ge=1, le=100)]
Cursor = Annotated[str | None, Query(max_length=2048)]
ERRORS: dict[int | str, dict[str, Any]] = {"default": {"model": ErrorResponse}}


@router.get(
    "/tenants/{tenant_id}/conversations",
    response_model=ConversationPage,
    operation_id="listConversations",
    responses=ERRORS,
)
async def list_conversations(
    tenant_id: ResourceId,
    identity: Reader,
    repo: Repository,
    session: Session,
    limit: Limit = 20,
    cursor: Cursor = None,
) -> ConversationPage:
    return await run_in_threadpool(
        repo.list, identity, tenant_id, session, limit=limit, cursor=cursor
    )


@router.post(
    "/tenants/{tenant_id}/conversations",
    response_model=Conversation,
    response_model_exclude_none=True,
    status_code=201,
    operation_id="createConversation",
    responses=ERRORS,
)
async def create_conversation(
    tenant_id: ResourceId,
    body: ConversationCreate,
    identity: Reader,
    repo: Repository,
    session: Session,
    request: Request,
    response: Response,
) -> Conversation:
    result = await run_in_threadpool(
        repo.create, identity, tenant_id, body, _header(request, "Idempotency-Key"), session
    )
    response.headers["ETag"] = etag(result.version)
    return result


@router.get(
    "/tenants/{tenant_id}/conversations/{conversation_id}",
    response_model=Conversation,
    response_model_exclude_none=True,
    operation_id="getConversation",
    responses=ERRORS,
)
async def get_conversation(
    tenant_id: ResourceId,
    conversation_id: ResourceId,
    identity: Reader,
    repo: Repository,
    session: Session,
    response: Response,
) -> Conversation:
    result = await run_in_threadpool(repo.get, identity, tenant_id, conversation_id, session)
    response.headers["ETag"] = etag(result.version)
    return result


@router.patch(
    "/tenants/{tenant_id}/conversations/{conversation_id}",
    response_model=Conversation,
    response_model_exclude_none=True,
    operation_id="updateConversation",
    responses=ERRORS,
)
async def update_conversation(
    tenant_id: ResourceId,
    conversation_id: ResourceId,
    body: ConversationPatch,
    identity: Identity,
    repo: Repository,
    request: Request,
    response: Response,
) -> Conversation:
    if _header(request, "X-Private-Session") is not None:
        raise APIError(ErrorCode.INVALID_REQUEST)
    result = await run_in_threadpool(
        repo.update,
        identity,
        tenant_id,
        conversation_id,
        body,
        _header(request, "Idempotency-Key"),
        _header(request, "If-Match"),
    )
    response.headers["ETag"] = etag(result.version)
    return result


@router.get(
    "/tenants/{tenant_id}/conversations/{conversation_id}/messages",
    response_model=MessagePage,
    operation_id="listMessages",
    responses=ERRORS,
)
async def list_messages(
    tenant_id: ResourceId,
    conversation_id: ResourceId,
    identity: Reader,
    repo: Repository,
    session: Session,
    limit: Limit = 20,
    cursor: Cursor = None,
) -> MessagePage:
    return await run_in_threadpool(
        repo.messages, identity, tenant_id, conversation_id, session, limit=limit, cursor=cursor
    )


@router.post(
    "/tenants/{tenant_id}/conversations/{conversation_id}/turns",
    response_model=RunAccepted,
    status_code=202,
    operation_id="createTurn",
    responses=ERRORS,
)
async def create_turn(
    tenant_id: ResourceId,
    conversation_id: ResourceId,
    body: TurnCreate,
    identity: Reader,
    repo: Repository,
    session: Session,
    request: Request,
) -> RunAccepted:
    return await run_in_threadpool(
        repo.turn,
        identity,
        tenant_id,
        conversation_id,
        body,
        _header(request, "Idempotency-Key"),
        session,
    )


@router.get(
    "/tenants/{tenant_id}/runs/{run_id}",
    response_model=Run,
    response_model_exclude_none=True,
    operation_id="getRun",
    responses=ERRORS,
)
async def get_run(
    tenant_id: ResourceId,
    run_id: ResourceId,
    identity: Reader,
    repo: Repository,
    session: Session,
    response: Response,
) -> Run:
    result = await run_in_threadpool(repo.get_run, identity, tenant_id, run_id, session)
    response.headers["ETag"] = etag(result.version)
    return result


@router.post(
    "/tenants/{tenant_id}/runs/{run_id}:cancel",
    response_model=Run,
    response_model_exclude_none=True,
    operation_id="cancelRun",
    responses=ERRORS,
)
async def cancel_run(
    tenant_id: ResourceId,
    run_id: ResourceId,
    identity: Reader,
    repo: Repository,
    session: Session,
    request: Request,
    response: Response,
) -> Run:
    result = await run_in_threadpool(
        repo.cancel_run, identity, tenant_id, run_id, _header(request, "Idempotency-Key"), session
    )
    response.headers["ETag"] = etag(result.version)
    return result
