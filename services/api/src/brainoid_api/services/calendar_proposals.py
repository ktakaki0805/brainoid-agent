"""Owner proposal preview/rejection; no confirmation or provider write endpoint."""

from typing import Annotated, Literal

from brainoid_contracts import ResourceId
from brainoid_contracts.deletion import purge_epoch
from brainoid_contracts.errors import APIError, ErrorCode
from brainoid_contracts.guards import release_guard
from brainoid_contracts.idempotency import Idempotency, MutationResult
from brainoid_contracts.transactions import Document, Transaction
from brainoid_memory.calendar_proposal_access import check_origin
from brainoid_memory.calendar_proposals import (
    CalendarProposals,
    Change,
    ProposalView,
    proposal_path,
    view,
)
from brainoid_memory.forget import Operation
from brainoid_memory.models import MemoryValue
from brainoid_memory.sources import CanonicalSources
from brainoid_policy.models import DeviceSubject, Subject, UserSubject
from brainoid_runs.confirmation_decision import prepare_decision
from brainoid_runs.memory_sources import SavedMessages
from brainoid_runs.state import RunState
from pydantic import Field, field_validator

from brainoid_api.auth.authorization import transaction_policy
from brainoid_api.auth.context import AuthenticatedIdentity
from brainoid_api.repositories.agents import AgentIdentity
from brainoid_api.repositories.conversations import Conversations, ReadResult, policy_call
from brainoid_api.services.calendar_events import CalendarEvents


class CalendarConfirm(MemoryValue):
    args_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    confirmed: Literal[True]

    @field_validator("confirmed", mode="before")
    @classmethod
    def explicit_true(cls, value: object) -> object:
        if value is not True:
            raise ValueError("Explicit confirmation required")
        return value


class Proposals:
    def __init__(
        self,
        events: CalendarEvents,
        conversations: Conversations | None = None,
        *,
        write_enabled: bool = False,
    ) -> None:
        self.write_enabled = write_enabled
        self.events, self.conversations = events, conversations
        self.service = CalendarProposals(events.reader)

    def epoch(self) -> ResourceId | None:
        sessions = self.events.sessions
        return None if sessions is None else sessions._credentials.credential_epoch

    def policy(self, tx: Transaction):  # type: ignore[no-untyped-def]
        tenants = self.events.authorization.tenants
        return transaction_policy(
            tx, now=tenants._now, ledger=tenants._ledger, allow_untracked_bootstrap=True
        )

    def create(
        self,
        identity: AgentIdentity,
        tenant: ResourceId,
        integration: ResourceId,
        change: Change,
        key: str | None,
        session: ResourceId | None = None,
    ) -> ProposalView:
        return self.create_for_release(
            identity, tenant, integration, change, key, session
        ).release()

    def create_for_release(
        self,
        identity: AgentIdentity,
        tenant: ResourceId,
        integration: ResourceId,
        change: Change,
        key: str | None,
        session: ResourceId | None = None,
    ) -> ReadResult[ProposalView]:
        def provenance(tx: Transaction) -> Document:
            subject: Subject
            if isinstance(identity, AuthenticatedIdentity):
                subject = UserSubject(user_id=identity.user_id)
                credential = None
            else:
                subject = DeviceSubject(
                    device_id=identity.sub,
                    connection_generation=identity.connection_generation,
                    private_session_id=session,
                )
                credential = {
                    "tenant_id": tenant,
                    "device_id": identity.sub,
                    "family_id": identity.family_id,
                    "credential_epoch": identity.credential_epoch,
                    "authorization_version": identity.authorization_version,
                    "connection_generation": identity.connection_generation,
                }
            return {
                "origin": {
                    "subject": subject.model_dump(mode="json"),
                    "credential": credential,
                    "purge_epoch": purge_epoch(tx, tenant),
                }
            }

        record, guard = policy_call(
            lambda: self.service.create_for_release(
                lambda tx: self.events.current(
                    tx,
                    identity,
                    tenant,
                    integration,
                    session,
                    calendar=change.calendar_id,
                    operation="calendar.propose",
                ),
                change,
                key,
                provenance=provenance,
                verify_origin=self.verify_origin,
            )
        )
        return ReadResult(view(record), guard)

    def verify_origin(self, tx: Transaction, record: Document) -> None:
        policy_call(
            lambda: check_origin(
                tx,
                self.policy(tx),
                record,
                now=self.events.reader.now,
                credential_epoch=self.epoch(),
                sources=CanonicalSources(SavedMessages()),
            )
        )

    def current(
        self,
        tx: Transaction,
        identity: AuthenticatedIdentity,
        tenant: ResourceId,
        proposal: ResourceId,
    ) -> Document:
        record = self.service.read(tx, tenant, proposal)
        integration, scope = self.events.current(
            tx,
            identity,
            tenant,
            ResourceId(record["integration_id"]),
            calendar=record["normalized_request"]["calendar_id"],
            operation="calendar.propose",
        )
        self.service.authorized(record, integration, scope)
        self.verify_origin(tx, record)
        return record

    def get(
        self, identity: AuthenticatedIdentity, tenant: ResourceId, proposal: ResourceId
    ) -> ProposalView:
        return self.get_for_release(identity, tenant, proposal).release()

    def get_for_release(
        self, identity: AuthenticatedIdentity, tenant: ResourceId, proposal: ResourceId
    ) -> ReadResult[ProposalView]:
        from brainoid_contracts import normalize_utc

        def read(tx: Transaction) -> ReadResult[ProposalView]:
            record = self.current(tx, identity, tenant, proposal)
            if (
                record["status"] == "pending"
                and normalize_utc(record["expires_at"]) <= self.events.reader.now()
            ):

                def write() -> None:
                    pass

                if record.get("run_id") is not None:
                    if self.conversations is None:
                        raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)
                    jobs = self.conversations.jobs
                    state = RunState(
                        jobs.ledger,
                        now=jobs.now,
                        credential_epoch=self.epoch(),
                        allow_untracked_bootstrap=jobs.allow_untracked,
                    )
                    write = policy_call(
                        lambda: prepare_decision(tx, jobs, state, record, "expired")
                    )
                write()
                record = {**record, "status": "expired", "version": record["version"] + 1}
                tx.set(proposal_path(tenant, proposal), record)
            return ReadResult(view(record), release_guard(tx))

        return policy_call(lambda: self.service.store.run(read))

    def reject(
        self,
        identity: AuthenticatedIdentity,
        tenant: ResourceId,
        proposal: ResourceId,
        key: str | None,
    ) -> ProposalView:
        return self.reject_for_release(identity, tenant, proposal, key).release()

    def reject_for_release(
        self,
        identity: AuthenticatedIdentity,
        tenant: ResourceId,
        proposal: ResourceId,
        key: str | None,
    ) -> ReadResult[ProposalView]:
        from brainoid_contracts import normalize_utc

        def authorize(tx: Transaction, replay: MutationResult | None):  # type: ignore[no-untyped-def]
            self.current(tx, identity, tenant, proposal)
            return self.events.authorization.authority(tx, identity, tenant)

        def mutate(tx: Transaction, scope):  # type: ignore[no-untyped-def]
            record = self.current(tx, identity, tenant, proposal)
            if record["status"] not in ("pending", "declined", "expired"):
                raise APIError(ErrorCode.CONFLICT)
            if record["status"] == "pending":
                decision = (
                    "expired"
                    if normalize_utc(record["expires_at"]) <= self.events.reader.now()
                    else "declined"
                )

                def write() -> None:
                    pass

                if record.get("run_id") is not None:
                    if self.conversations is None:
                        raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)
                    jobs = self.conversations.jobs
                    state = RunState(
                        jobs.ledger,
                        now=jobs.now,
                        credential_epoch=self.epoch(),
                        allow_untracked_bootstrap=jobs.allow_untracked,
                    )
                    write = policy_call(lambda: prepare_decision(tx, jobs, state, record, decision))
                write()
                tx.set(
                    proposal_path(tenant, proposal),
                    {**record, "status": decision, "version": record["version"] + 1},
                )
            return MutationResult(status_code=200, resource_id=proposal)

        policy_call(
            lambda: Idempotency(self.service.store, now=self.events.reader.now).execute(
                operation="calendar.reject:" + proposal,
                key=key,
                body={},
                authorize=authorize,
                mutate=mutate,
            )
        )
        return self.get_for_release(identity, tenant, proposal)

    def confirm_for_release(
        self,
        identity: AuthenticatedIdentity,
        tenant: ResourceId,
        proposal: ResourceId,
        body: "CalendarConfirm",
        key: str | None,
    ) -> "ReadResult[Operation]":
        from brainoid_contracts import normalize_utc
        from brainoid_jobs import Jobs
        from brainoid_memory.calendar_writer import KIND
        from brainoid_memory.forget import operation_path
        from brainoid_policy.models import Action, ResourceKind, ResourceRef
        from brainoid_runs.calendar_confirmation import prepare_admission

        if not isinstance(identity, AuthenticatedIdentity):
            raise APIError(ErrorCode.OPERATION_FORBIDDEN)
        if not self.write_enabled:
            raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)
        tenants = self.events.authorization.tenants
        if tenants._ledger is None:
            raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)
        jobs = Jobs(
            self.service.store,
            tenants._ledger,
            now=self.events.reader.now,
            allow_untracked_bootstrap=True,
        )

        def authorize(tx: Transaction, replay: MutationResult | None):  # type: ignore[no-untyped-def]
            record = self.current(tx, identity, tenant, proposal)
            if record["args_hash"] != body.args_hash or body.confirmed is not True:
                raise APIError(ErrorCode.OPERATION_FORBIDDEN)
            return self.events.authorization.authority(tx, identity, tenant)

        def mutate(tx: Transaction, scope):  # type: ignore[no-untyped-def]
            record = self.current(tx, identity, tenant, proposal)
            if record.get("operation_id") is not None:
                existing_operation = tx.get(operation_path(tenant, proposal))
                if (
                    existing_operation is None
                    or existing_operation.get("args_hash") != body.args_hash
                ):
                    raise APIError(ErrorCode.OPERATION_FORBIDDEN)
                return MutationResult(status_code=202, resource_id=proposal)
            if record["status"] != "pending" or normalize_utc(record["expires_at"]) <= jobs.now():
                raise APIError(ErrorCode.CONFLICT)
            state = RunState(
                jobs.ledger,
                now=jobs.now,
                credential_epoch=self.epoch(),
                allow_untracked_bootstrap=True,
            )
            claim, join = prepare_admission(tx, jobs, state, record)
            if tx.get(operation_path(tenant, proposal)) is not None:
                raise APIError(ErrorCode.CONFLICT)
            grant = self.policy(tx).resource(
                tenant,
                UserSubject(user_id=identity.user_id),
                ResourceRef(tenant_id=tenant, kind=ResourceKind.CALENDAR_PROPOSAL, id=proposal),
                action=Action.WRITE,
            )
            updated = {
                **record,
                "status": "confirmed",
                "operation_id": proposal,
                "version": record["version"] + 1,
                "external_claim": claim,
            }
            now = jobs.now()
            operation = Operation(operation_id=proposal, kind=KIND, created_at=now, updated_at=now)
            jobs.add_pending(
                tx, operation_id=proposal, kind=KIND, grant=grant, source_replacement=updated
            )
            join()
            tx.create(
                operation_path(tenant, proposal),
                {
                    "tenant_id": tenant,
                    "requested_by": identity.user_id,
                    "proposal_id": proposal,
                    "args_hash": body.args_hash,
                    "operation": operation.model_dump(mode="json"),
                },
            )
            return MutationResult(status_code=202, resource_id=proposal)

        policy_call(
            lambda: Idempotency(self.service.store, now=self.events.reader.now).execute(
                operation="calendar.confirm:" + proposal,
                key=key,
                body=body.model_dump(mode="json"),
                authorize=authorize,
                mutate=mutate,
            )
        )
        return self.operation_for_release(identity, tenant, proposal)

    def operation_for_release(
        self,
        identity: AuthenticatedIdentity,
        tenant: ResourceId,
        operation: ResourceId,
    ) -> "ReadResult[Operation]":
        result = self.find_operation_for_release(identity, tenant, operation)
        if result is None:
            raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
        return result

    def find_operation_for_release(
        self,
        identity: AuthenticatedIdentity,
        tenant: ResourceId,
        operation: ResourceId,
    ) -> "ReadResult[Operation] | None":
        """None means a different operation kind; source denials remain errors."""
        from brainoid_memory.forget import operation_path

        def read(tx: Transaction) -> "ReadResult[Operation] | None":
            saved = tx.get(operation_path(tenant, operation))
            if saved is None or saved.get("operation", {}).get("kind") != "calendar_write":
                return None
            record = self.current(tx, identity, tenant, ResourceId(saved["proposal_id"]))
            if (
                record.get("operation_id") != operation
                or saved["args_hash"] != record["args_hash"]
                or saved.get("requested_by") != identity.user_id
            ):
                raise APIError(ErrorCode.OPERATION_FORBIDDEN)
            return ReadResult(Operation.model_validate(saved["operation"]), release_guard(tx))

        return policy_call(lambda: self.service.store.run(read))
