"""Authenticated, global user profile; membership is irrelevant to this route."""

from typing import Annotated

from fastapi import APIRouter, Depends, Response
from starlette.concurrency import run_in_threadpool

from brainoid_api.auth.context import AuthenticatedIdentity
from brainoid_api.auth.profiles import UserProfile
from brainoid_api.auth.runtime import AuthServices, authenticated_identity, get_auth_services
from brainoid_api.common.versioning import etag
from brainoid_api.errors import APIError, ErrorCode, ErrorResponse

router = APIRouter(tags=["users"])


@router.get(
    "/users/me",
    response_model=UserProfile,
    operation_id="getCurrentUser",
    responses={"default": {"model": ErrorResponse}},
)
async def current_user(
    response: Response,
    identity: Annotated[AuthenticatedIdentity, Depends(authenticated_identity)],
    services: Annotated[AuthServices, Depends(get_auth_services)],
) -> UserProfile:
    if services.profiles is None:
        raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)
    profile = await run_in_threadpool(services.profiles.get, identity.user_id)
    response.headers["ETag"] = etag(profile.version)
    return profile
