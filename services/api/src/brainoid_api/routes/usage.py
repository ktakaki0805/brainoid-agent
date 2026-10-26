"""Current user aggregate or authorized tenant total, with no individual detail endpoint."""

from brainoid_contracts import ResourceId
from brainoid_contracts.transactions import Transaction
from brainoid_policy.models import Role
from brainoid_usage import UsageView
from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool

from brainoid_api.errors import APIError, ErrorCode
from brainoid_api.routes.conversations import ERRORS, Repository
from brainoid_api.routes.tenants import Identity

router = APIRouter(tags=["usage"])


@router.get(
    "/tenants/{tenant_id}/usage",
    response_model=UsageView,
    operation_id="getUsage",
    responses=ERRORS,
)
async def get_usage(
    tenant_id: ResourceId, identity: Identity, repo: Repository, request: Request
) -> UsageView:
    # Reject selectors instead of silently implying that another user's data was returned.
    if request.query_params or request.headers.get("X-Private-Session") is not None:
        raise APIError(ErrorCode.INVALID_REQUEST)

    def read() -> UsageView:
        def work(tx: Transaction) -> UsageView:
            current = repo.tenants.authorize_user(tx, identity)
            principal = repo.tenants._principal(tx, identity, tenant_id)
            result = repo.usage.view(
                tx,
                tenant_id,
                user=None if principal.role in (Role.OWNER, Role.ADMIN) else identity.user_id,
            )
            current.ensure_live(repo.now())
            return result

        return repo.store.run(work)

    return await run_in_threadpool(read)
