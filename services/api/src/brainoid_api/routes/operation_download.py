"""Authenticated ZIP release checks at headers and every ASGI body boundary."""

from typing import Annotated

from brainoid_contracts import ResourceId
from fastapi import APIRouter, Depends
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response
from starlette.types import Receive, Scope, Send

from brainoid_api.auth.context import AuthenticatedIdentity
from brainoid_api.auth.runtime import AuthServices, get_auth_services
from brainoid_api.errors import APIError, ErrorCode, ErrorResponse
from brainoid_api.routes.tenants import Identity
from brainoid_api.services.export_requests import ExportRequests

router = APIRouter(tags=["operations"])


class ExportResponse(Response):
    media_type = "application/zip"

    def __init__(
        self,
        content: bytes,
        repository: ExportRequests,
        identity: AuthenticatedIdentity,
        tenant: ResourceId,
        operation: ResourceId,
    ) -> None:
        super().__init__(
            content,
            headers={
                "Content-Disposition": 'attachment; filename="brainoid-export.zip"',
                "Cache-Control": "no-store",
            },
        )
        self.repository, self.identity, self.tenant, self.operation = (
            repository,
            identity,
            tenant,
            operation,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def release() -> None:
            guard = await run_in_threadpool(
                self.repository.release, self.identity, self.tenant, self.operation
            )
            # There is no await between the token/deadline check and the ASGI send.
            guard.finish()

        await release()
        await send({"type": "http.response.start", "status": 200, "headers": self.raw_headers})
        for offset in range(0, len(self.body), 65536):
            await release()
            await send(
                {
                    "type": "http.response.body",
                    "body": self.body[offset : offset + 65536],
                    "more_body": True,
                }
            )
        await release()
        await send({"type": "http.response.body", "body": b"", "more_body": False})


@router.get(
    "/tenants/{tenant_id}/operations/{operation_id}/download",
    operation_id="downloadExport",
    response_class=Response,
    responses={
        200: {"content": {"application/zip": {"schema": {"type": "string", "format": "binary"}}}},
        "default": {"model": ErrorResponse},
    },
)
async def download_export(
    tenant_id: ResourceId,
    operation_id: ResourceId,
    identity: Identity,
    services: Annotated[AuthServices, Depends(get_auth_services)],
) -> Response:
    if services.exports is None:
        raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)
    content = await run_in_threadpool(services.exports.download, identity, tenant_id, operation_id)
    return ExportResponse(content, services.exports, identity, tenant_id, operation_id)
