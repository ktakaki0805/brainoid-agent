"""Explicit user-facing export projections, with current canonical source checks."""

from brainoid_contracts import ResourceId, UtcTimestamp, Visibility
from brainoid_contracts.access import fingerprint
from brainoid_contracts.deletion import exportable
from brainoid_contracts.transactions import Document, Transaction
from brainoid_policy.models import ResourceKind, ResourceRef
from brainoid_policy.scopes import PolicyDenied, PolicyUnavailable
from pydantic import TypeAdapter

from brainoid_memory.calendar_access import check_calendar_sources
from brainoid_memory.dto import MemoryView
from brainoid_memory.export_manifest import ManifestEntry
from brainoid_memory.repository import FactTransaction

# Nested public structures are validated by MemoryView. Other values below are
# scalar fields (aliases is a bounded string list), never opaque dictionaries.
FIELDS = {
    "conversations": ("title", "agent_id", "created_at", "updated_at"),
    "messages": ("role", "text", "run_id", "created_at"),
    "entities": ("kind", "label", "aliases", "created_at", "updated_at"),
    "notes": ("title", "body", "created_at", "updated_at"),
}


def project(
    tx: Transaction,
    access: FactTransaction,
    path: str,
    *,
    scope: str,
) -> tuple[ManifestEntry, Document] | None:
    tenant = access.principal.context.tenant_id
    prefix = f"tenants/{tenant}/"
    if not path.startswith(prefix):
        raise PolicyDenied()
    parts = path[len(prefix) :].split("/")
    if len(parts) == 2:
        kind, identity = parts
        parent = None
    elif len(parts) == 4 and parts[0] == "conversations" and parts[2] == "messages":
        _, parent, kind, identity = parts
    else:
        raise PolicyDenied()
    item_id = TypeAdapter(ResourceId).validate_python(identity)
    parent_id = None if parent is None else TypeAdapter(ResourceId).validate_python(parent)
    document = tx.get(path)
    if document is None:
        return None
    if document.get("id") != identity or document.get("tenant_id") != tenant:
        raise PolicyDenied()
    if not exportable(tx, tenant, kind, item_id, document, parent=parent_id):
        return None
    if kind == "calendar_proposals":
        from brainoid_memory.calendar_proposal_access import check_origin
        from brainoid_memory.calendar_proposals import view as proposal_view

        if document.get("owner_user_id") != access.principal.user_id:
            return None
        try:
            check_origin(
                tx,
                access._authority.policy,
                document,
                now=access._repo._now,
                credential_epoch=access._repo._credential_epoch,
                sources=access._repo._sources,
            )
        except PolicyUnavailable:
            raise
        except PolicyDenied:
            return None
        public = proposal_view(document).model_dump(mode="json")
        access.finish()
        return ManifestEntry(
            kind="calendar_proposals",
            resource_id=item_id,
            version=document["version"],
            deletion_epoch=document["deletion_epoch"],
            fingerprint=fingerprint([public, document["args_hash"]]),
        ), public
    if kind == "integrations":
        from brainoid_contracts import DisplayPolicy, Scope
        from brainoid_contracts.access import AuthorizedScope
        from brainoid_policy.accounts import account_stamp

        from brainoid_memory.calendar_oauth import integration_view

        if access.principal.user_id is None:
            raise PolicyDenied()
        current = AuthorizedScope(
            context=access.principal.context,
            scope=Scope(visibility=Visibility.PRIVATE, display_policy=DisplayPolicy.NEVER),
            owner_user_id=access.principal.user_id,
            authorization_stamp=fingerprint(
                [access.principal.stamp, account_stamp(tx, access.principal.user_id)]
            ),
        )
        if (
            document.get("provider") != "google_calendar"
            or document.get("status") == "revoked"
            or document.get("visibility") != "private"
            or document.get("owner_user_id") != access.principal.user_id
            or document.get("binding") != current.binding()
        ):
            return None
        public = integration_view(document).model_dump(mode="json")
        access.finish()
        return ManifestEntry(
            kind="integrations",
            resource_id=item_id,
            version=document["version"],
            deletion_epoch=document["deletion_epoch"],
            fingerprint=fingerprint([public, document.get("binding"), document.get("secret_ref")]),
        ), public
    ref = ResourceRef(
        tenant_id=tenant,
        kind=ResourceKind.CONVERSATION if parent_id else ResourceKind(kind),
        id=parent_id or item_id,
    )
    policy = access._authority.policy
    try:
        grant = policy.resource(tenant, access.caller.subject, ref)
    except PolicyUnavailable:
        raise
    except PolicyDenied:
        return None
    if parent_id is not None and document.get("conversation_id") != parent_id:
        raise PolicyDenied()
    own = (
        grant.scope.visibility == Visibility.PRIVATE
        and grant.owner_user_id == access.principal.user_id
    )
    created = document.get("created_by") == access.principal.user_id
    if kind == "notifications":
        created = document.get("user_id") == access.principal.user_id
    if kind == "messages":
        try:
            check_calendar_sources(
                tx,
                policy,
                access.principal,
                document,
                credential_epoch=access._repo._credential_epoch,
                sources=access._repo._sources,
            )
        except PolicyUnavailable:
            raise
        except PolicyDenied:
            return None
    if kind == "messages" and document.get("role") == "assistant":
        from brainoid_jobs.models import job_id

        run_id = TypeAdapter(ResourceId).validate_python(document.get("run_id"))
        run_job = tx.get(f"tenants/{tenant}/jobs/{job_id(tenant, 'run_turn', run_id)}")
        # Shared assistant replies are the data of the initiator of that turn.
        created = (
            run_job is not None
            and run_job.get("grant", {}).get("principal", {}).get("user_id")
            == access.principal.user_id
        )
    # Parent metadata gives context to the user's contributions in conversations
    # created by somebody else. Shared message bodies still require own authorship.
    if scope == "my_data" and kind != "conversations" and not (own or created):
        return None
    if kind == "facts":
        record = access.get(item_id)
        public = MemoryView.model_validate(
            {
                k: v
                for k, v in record.model_dump(exclude_none=True).items()
                if k in MemoryView.model_fields
            }
        ).model_dump(mode="json")
    elif kind == "commands":
        from brainoid_memory.commands import Commands
        from brainoid_memory.commands import view as command_view

        try:
            record_command = Commands.get(access, item_id)
        except PolicyUnavailable:
            raise
        except PolicyDenied:
            return None
        public = command_view(record_command).model_dump(mode="json", exclude_none=True)
    elif kind == "notifications":
        from brainoid_memory.notifications import public_notification

        public = public_notification(access, item_id)
    elif kind == "reminders":
        from brainoid_memory.reminders import Reminders
        from brainoid_memory.reminders import view as reminder_view

        record_reminder, _ = Reminders.get(access, item_id)
        public = reminder_view(record_reminder).model_dump(mode="json")
    elif kind == "tasks":
        from brainoid_memory.tasks import Tasks, view

        record_task, _ = Tasks.get(access, item_id)
        public = view(record_task).model_dump(mode="json")
    else:
        public = {"id": item_id, "version": document.get("version")}
        if parent_id:
            public["conversation_id"] = parent_id
        else:
            public.update(
                visibility=grant.scope.visibility, display_policy=grant.scope.display_policy
            )
        for field in FIELDS[kind]:
            if field not in document:
                continue
            value = document[field]
            if field == "aliases":
                if (
                    not isinstance(value, list)
                    or len(value) > 10
                    or any(not isinstance(v, str) or len(v) > 200 for v in value)
                ):
                    raise PolicyDenied()
            elif field in ("created_at", "updated_at"):
                value = TypeAdapter(UtcTimestamp).validate_python(value).isoformat()
            elif not isinstance(value, str) or len(value) > 32000:
                raise PolicyDenied()
            public[field] = value
        if kind == "messages" and document.get("role") not in ("user", "assistant", "tool"):
            raise PolicyDenied()
        # Tool-role model protocol may contain management tokens/provider details;
        # user history exports include only the displayed user/assistant text.
        if kind == "messages" and document.get("role") == "tool":
            return None
    access.finish()
    entry = ManifestEntry(
        kind=kind,  # type: ignore[arg-type]
        resource_id=item_id,
        parent_id=parent_id,
        version=document["version"],
        deletion_epoch=document["deletion_epoch"],
        fingerprint=fingerprint(
            [
                public,
                grant.resource.model_dump(mode="json"),
                document.get("suppress_reuse"),
                document.get("source_refs", []),
                document.get("calendar_sources", []),
            ]
            + (
                [
                    document.get("binding"),
                    document.get("actor_stamp"),
                    document.get("account_stamp"),
                    document.get("actor_credential"),
                ]
                if kind == "commands"
                else []
            )
        ),
    )
    return entry, public
