"""Owner proposal preview/rejection; no confirmation or provider write endpoint."""

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
from brainoid_memory.sources import CanonicalSources
from brainoid_policy.models import DeviceSubject, Subject, UserSubject
from brainoid_runs.confirmation_decision import prepare_decision
from brainoid_runs.memory_sources import SavedMessages
from brainoid_runs.state import RunState

from brainoid_api.auth.authorization import transaction_policy
from brainoid_api.auth.context import AuthenticatedIdentity
from brainoid_api.repositories.agents import AgentIdentity
from brainoid_api.repositories.conversations import Conversations, ReadResult, policy_call
from brainoid_api.services.calendar_events import CalendarEvents


class Proposals:
    def __init__(self, events: CalendarEvents, conversations: Conversations | None = None) -> None:
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
