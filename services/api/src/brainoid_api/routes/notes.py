"""Note CRUD over the same domain service used by typed conversation tools."""

from typing import Annotated

from brainoid_contracts import ResourceId
from brainoid_memory.notes import NoteCreate, NotePage, NotePatch, NoteView
from fastapi import APIRouter, Depends, Query, Request, Response
from starlette.concurrency import run_in_threadpool

from brainoid_api.auth.runtime import AuthServices, get_auth_services
from brainoid_api.common.versioning import etag
from brainoid_api.errors import APIError, ErrorCode, ErrorResponse
from brainoid_api.repositories.notes import NoteRequests
from brainoid_api.routes.agents import Reader, Session
from brainoid_api.routes.tenants import _header

router = APIRouter(tags=["notes"])


def repository(services: Annotated[AuthServices, Depends(get_auth_services)]) -> NoteRequests:
    if services.memories is None:
        raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)
    return NoteRequests(services.memories)


Repository = Annotated[NoteRequests, Depends(repository)]


@router.get(
    "/tenants/{tenant_id}/notes",
    response_model=NotePage,
    operation_id="listNotes",
    responses={"default": {"model": ErrorResponse}},
)
async def list_notes(
    tenant_id: ResourceId,
    identity: Reader,
    notes: Repository,
    session: Session,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
    query: Annotated[str, Query(max_length=200)] = "",
) -> NotePage:
    return await run_in_threadpool(
        notes.list, identity, tenant_id, session, query=query, limit=limit, cursor=cursor
    )


@router.post(
    "/tenants/{tenant_id}/notes",
    response_model=NoteView,
    status_code=201,
    response_model_exclude_none=True,
    operation_id="createNote",
    responses={"default": {"model": ErrorResponse}},
)
async def create_note(
    tenant_id: ResourceId,
    body: NoteCreate,
    identity: Reader,
    notes: Repository,
    session: Session,
    request: Request,
    response: Response,
) -> NoteView:
    result = await run_in_threadpool(
        notes.create, identity, tenant_id, body, _header(request, "Idempotency-Key"), session
    )
    response.headers["ETag"] = etag(result.version)
    return result


@router.get(
    "/tenants/{tenant_id}/notes/{note_id}",
    response_model=NoteView,
    response_model_exclude_none=True,
    operation_id="getNote",
    responses={"default": {"model": ErrorResponse}},
)
async def get_note(
    tenant_id: ResourceId,
    note_id: ResourceId,
    identity: Reader,
    notes: Repository,
    session: Session,
    response: Response,
) -> NoteView:
    result = await run_in_threadpool(notes.get, identity, tenant_id, note_id, session)
    response.headers["ETag"] = etag(result.version)
    return result


@router.patch(
    "/tenants/{tenant_id}/notes/{note_id}",
    response_model=NoteView,
    response_model_exclude_none=True,
    operation_id="updateNote",
    responses={"default": {"model": ErrorResponse}},
)
async def update_note(
    tenant_id: ResourceId,
    note_id: ResourceId,
    body: NotePatch,
    identity: Reader,
    notes: Repository,
    session: Session,
    request: Request,
    response: Response,
) -> NoteView:
    result = await run_in_threadpool(
        notes.update,
        identity,
        tenant_id,
        note_id,
        body,
        _header(request, "Idempotency-Key"),
        _header(request, "If-Match"),
        session,
    )
    response.headers["ETag"] = etag(result.version)
    return result
