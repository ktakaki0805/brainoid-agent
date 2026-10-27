"""Current Calendar source authority, shared by HTTP, workers and export."""

from brainoid_contracts import DeviceActorContext, DisplayPolicy, ResourceId, Scope, Visibility
from brainoid_contracts.access import AuthorizedScope, fingerprint
from brainoid_contracts.deletion import fenced_document
from brainoid_contracts.transactions import Document, Transaction
from brainoid_policy.accounts import account_stamp
from brainoid_policy.evaluator import Evaluator
from brainoid_policy.models import UserSubject
from brainoid_policy.scopes import PolicyDenied, Principal

from brainoid_memory.calendar_oauth import READ, WRITE
from brainoid_memory.calendar_reader import integration_path, integration_stamp
from brainoid_memory.sources import SourceResolver

CALENDAR_SCOPE = Scope(
    visibility=Visibility.PRIVATE, display_policy=DisplayPolicy.AUTHENTICATED_SESSION
)


def current_integration(
    tx: Transaction,
    policy: Evaluator,
    principal: Principal,
    integration: ResourceId,
    *,
    calendar: str | None = None,
    operation: str = "calendar.read",
) -> tuple[Document, AuthorizedScope]:
    tenant = principal.context.tenant_id
    actor = principal.context.actor
    if principal.user_id is None or (
        isinstance(actor, DeviceActorContext)
        and (actor.private_session is None or operation not in actor.allowed_scopes)
    ):
        raise PolicyDenied()
    user = policy.principal(tenant, UserSubject(user_id=principal.user_id))
    owner = AuthorizedScope(
        context=user.context,
        scope=Scope(visibility=Visibility.PRIVATE, display_policy=DisplayPolicy.NEVER),
        owner_user_id=user.user_id,
        authorization_stamp=fingerprint([user.stamp, account_stamp(tx, principal.user_id)]),
    )
    record = tx.get(integration_path(tenant, integration))
    if (
        record is None
        or tx.get(f"tenants/{tenant}/integration_deletion_fences/{integration}") is not None
    ):
        raise PolicyDenied()
    record = fenced_document(tx, tenant, "integrations", integration, record)
    authorization = tx.get(
        f"tenants/{tenant}/calendar_authorizations/{record.get('authorization_id')}"
    )
    if (
        record.get("id") != integration
        or record.get("tenant_id") != tenant
        or record.get("owner_user_id") != principal.user_id
        or record.get("provider") != "google_calendar"
        or record.get("visibility") != "private"
        or record.get("status") != "active"
        or record.get("deleted") is not False
        or record.get("suppress_reuse", False)
        or record.get("binding") != owner.binding()
        or not isinstance(record.get("granted_scopes"), list)
        or not set(record["granted_scopes"]).intersection((READ, WRITE))
        or authorization is None
        or authorization.get("status") != "connected"
        or authorization.get("integration_id") != integration
        or authorization.get("owner_user_id") != principal.user_id
        or authorization.get("tenant_id") != tenant
        or authorization.get("binding") != record["binding"]
        or authorization.get("secret_ref") != record.get("secret_ref")
        or authorization.get("granted_scopes") != record["granted_scopes"]
        or (calendar is not None and calendar not in record.get("allowed_calendar_ids", []))
    ):
        raise PolicyDenied()
    return record, AuthorizedScope(
        context=principal.context,
        scope=CALENDAR_SCOPE,
        owner_user_id=principal.user_id,
        authorization_stamp=fingerprint(
            [owner.binding(), principal.stamp, integration_stamp(record)]
        ),
    )


def check_calendar_sources(
    tx: Transaction,
    policy: Evaluator,
    principal: Principal,
    document: Document,
    *,
    credential_epoch: ResourceId | None = None,
    sources: SourceResolver | None = None,
    for_model: bool = False,
) -> None:
    """Fresh bounded current-source pass; no authorization cache survives this call."""
    from brainoid_memory.calendar_proposal_access import _SourceChecks

    _SourceChecks(
        tx,
        policy,
        now=policy._now,
        credential_epoch=credential_epoch,
        sources=sources,
        for_model=for_model,
    ).check_sources(principal, document)
