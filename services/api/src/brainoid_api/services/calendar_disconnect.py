"""Exact Calendar stop admission, immutable erasure intent and replayable cleanup."""

from uuid import UUID, uuid5

from brainoid_contracts import ResourceId
from brainoid_contracts.access import fingerprint
from brainoid_contracts.deletion import subject_id
from brainoid_contracts.transactions import Document, Transaction
from brainoid_jobs import Job
from brainoid_jobs.models import job_id
from brainoid_memory.calendar_export import barrier_path, checked_stop, state
from brainoid_memory.calendar_oauth import IntegrationView, integration_view
from brainoid_memory.calendar_reader import integration_path
from brainoid_memory.deletion_ledger import DeletionIntent, DeletionLedger, LifecycleScope
from brainoid_memory.forget import Operation, operation_path
from brainoid_memory.lifecycle import KIND, audit_grant, marker, scope_fence
from brainoid_policy.control_ledger import LedgerError

from brainoid_api.auth.context import AuthenticatedIdentity
from brainoid_api.common.versioning import require_if_match
from brainoid_api.errors import APIError, ErrorCode
from brainoid_api.services.calendar_authorization import CalendarAuthorization, path


class CalendarDisconnect:
    def __init__(self, calendar: CalendarAuthorization, ledger: DeletionLedger) -> None:
        self.calendar, self.ledger = calendar, ledger
        self.store, self.now = calendar.store, calendar.now

    def current(
        self,
        tx: Transaction,
        identity: AuthenticatedIdentity,
        tenant: ResourceId,
        integration: ResourceId,
    ) -> Document:
        scope = self.calendar.authority(tx, identity, tenant)
        saved = tx.get(integration_path(tenant, integration))
        if (
            saved is None
            or saved.get("tenant_id") != tenant
            or saved.get("id") != integration
            or saved.get("provider") != "google_calendar"
            or saved.get("visibility") != "private"
            or saved.get("owner_user_id") != identity.user_id
            or saved.get("binding") != scope.binding()
        ):
            raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
        return saved

    def get(
        self, identity: AuthenticatedIdentity, tenant: ResourceId, integration: ResourceId
    ) -> IntegrationView:
        return self.store.run(
            lambda tx: integration_view(self.current(tx, identity, tenant, integration))
        )

    def disconnect(
        self,
        identity: AuthenticatedIdentity,
        tenant: ResourceId,
        integration: ResourceId,
        key: str | None,
        if_match: str | None,
    ) -> IntegrationView:
        version = require_if_match(if_match)
        if key is None or not 16 <= len(key) <= 128:
            raise APIError(ErrorCode.INVALID_REQUEST)
        operation = ResourceId(
            str(uuid5(UUID(subject_id(identity.user_id)), f"calendar-disconnect:{tenant}:{key}"))
        )
        request_hash = fingerprint([tenant, integration, version])
        location = f"tenants/{tenant}/calendar_disconnect_requests/{operation}"

        def stop(tx: Transaction) -> None:
            saved = self.current(tx, identity, tenant, integration)
            prior = tx.get(location)
            if prior is not None:
                if (
                    prior.get("request_hash") != request_hash
                    or prior.get("requested_by") != identity.user_id
                ):
                    raise APIError(ErrorCode.IDEMPOTENCY_CONFLICT)
                return
            if saved["version"] != version:
                raise APIError(ErrorCode.VERSION_CONFLICT)
            if saved["status"] not in ("active", "reauth_required"):
                raise APIError(ErrorCode.CONFLICT)
            auth = tx.get(path(tenant, ResourceId(saved["authorization_id"])))
            if (
                auth is None
                or auth.get("id") != saved["authorization_id"]
                or auth.get("integration_id") != integration
                or auth.get("tenant_id") != tenant
                or auth.get("owner_user_id") != identity.user_id
                or auth.get("binding") != saved["binding"]
                or auth.get("secret_ref") != saved.get("secret_ref")
                or auth.get("status") != "connected"
                or auth.get("secret_binding") != self.calendar.secret_binding(auth)
            ):
                raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)
            barrier = state(tx, tenant, subject_id(identity.user_id))
            scope = LifecycleScope(
                kind="integration",
                tenant_id=tenant,
                resource_id=integration,
                version=version,
                deletion_epoch=saved.get("deletion_epoch", 0) + 1,
                authorization_id=ResourceId(auth["id"]),
                owner_subject_id=subject_id(identity.user_id),
                secret_slots=(ResourceId(auth["transient_slot"]), ResourceId(auth["token_slot"])),
                export_epoch=barrier["epoch"] + 1,
            )
            intent = DeletionIntent(
                kind="disconnect_calendar",
                operation_id=operation,
                tenant_id=tenant,
                request_hash=request_hash,
                source_policy="delete_source",
                created_at=self.now(),
                targets=(),
                scopes=(scope,),
            )
            if tx.get(scope_fence(scope)) is not None:
                raise APIError(ErrorCode.CONFLICT)
            previous = self.ledger.get(operation)
            if previous is not None:
                intent = intent.model_copy(update={"created_at": previous.created_at})
                if intent != previous:
                    raise APIError(ErrorCode.IDEMPOTENCY_CONFLICT)
            # The fence and private reservation commit before any external cleanup.
            # A failed ledger append still leaves the integration safely stopped.
            tx.create(
                location,
                {
                    "intent": intent.model_dump(mode="json"),
                    "request_hash": request_hash,
                    "requested_by": identity.user_id,
                },
            )
            tx.set(scope_fence(scope), marker(intent, scope))
            tx.set(
                barrier_path(tenant, subject_id(identity.user_id)),
                {
                    **barrier,
                    "epoch": scope.export_epoch,
                    "pending": {**barrier["pending"], operation: scope.export_epoch},
                },
            )
            tx.set(
                integration_path(tenant, integration),
                {
                    **saved,
                    "status": "revoked",
                    "allowed_calendar_ids": [],
                    "version": version + 1,
                    "updated_at": self.now(),
                    "stop_reason": "user_disconnected",
                    "cleanup_status": "pending",
                    "reconnect_allowed": True,
                    "provider_revocation": "not_attempted_scope_unsafe",
                    "provider_revocation_scope": "google_user_project",
                    "disconnect_operation_id": operation,
                },
            )

        try:
            self.store.run(stop)
            self.recover(tenant, operation)
        except LedgerError:
            raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE) from None
        return self.get(identity, tenant, integration)

    def recover(self, tenant: ResourceId, operation: ResourceId) -> None:
        """Worker/operator recovery of accepted stop; no new interactive authority."""
        location = f"tenants/{tenant}/calendar_disconnect_requests/{operation}"
        reserved = self.store.run(lambda tx: tx.get(location))
        if reserved is None:
            return
        intent = DeletionIntent.model_validate(reserved["intent"])
        if (
            intent.kind != "disconnect_calendar"
            or intent.tenant_id != tenant
            or intent.operation_id != operation
            or intent.request_hash != reserved.get("request_hash")
            or intent.scopes[0].owner_subject_id != subject_id(reserved["requested_by"])
        ):
            raise ValueError("Invalid Calendar disconnect reservation")
        self.ledger.append(intent)

        def accept(tx: Transaction) -> None:
            if tx.get(location) != reserved or tx.get(scope_fence(intent.scopes[0])) != marker(
                intent, intent.scopes[0]
            ):
                raise ValueError("Changed Calendar disconnect reservation")
            checked_stop(tx, intent)
            prior = tx.get(operation_path(tenant, operation))
            job = Job(
                id=job_id(tenant, KIND, operation),
                tenant_id=tenant,
                operation_id=operation,
                kind=KIND,
                grant=audit_grant(tenant, operation, reserved["requested_by"]),
                account_stamps={},
                created_at=intent.created_at,
                available_at=self.now(),
                dispatch_after=self.now(),
                status="waiting",
                attributes={"dispatch_queue": KIND},
            )
            existing_job = tx.get(f"tenants/{tenant}/jobs/{job.id}")
            if prior is not None:
                if prior.get("intent") != reserved["intent"]:
                    raise ValueError("Changed Calendar erasure intent")
                return
            completed = False
            if existing_job is not None:
                original = Job.model_validate(existing_job)
                if any(
                    getattr(original, field) != getattr(job, field)
                    for field in (
                        "id",
                        "tenant_id",
                        "operation_id",
                        "kind",
                        "grant",
                        "account_stamps",
                        "created_at",
                        "attributes",
                    )
                ):
                    raise ValueError("Unbound Calendar erasure job")
                completed = original.status == "succeeded"
                if completed:
                    if reserved.get("export_cleanup_complete") != intent.scopes[0].export_epoch:
                        raise ValueError("Missing Calendar completion receipt")
                elif original.status not in {"waiting", "pending", "running"}:
                    raise ValueError("Calendar job requires operator recovery")
            result = Operation(
                kind="disconnect_calendar",
                operation_id=operation,
                created_at=intent.created_at,
                updated_at=self.now(),
                status="succeeded" if completed else "pending",
            )
            tx.create(
                operation_path(tenant, operation),
                {**reserved, "tenant_id": tenant, "operation": result.model_dump(mode="json")},
            )
            if existing_job is None:
                tx.create(f"tenants/{tenant}/jobs/{job.id}", job.model_dump(mode="json"))

        self.store.run(accept)
