"""Bounded current-authority recovery; page cursors carry no resource grants."""

from datetime import datetime, timedelta
from functools import partial
from typing import Any

from brainoid_contracts import DisplayPolicy, ResourceId, Scope, Visibility
from brainoid_contracts.access import AuthorizedScope, fingerprint
from brainoid_contracts.device_snapshot import (
    DeviceSnapshot,
    DeviceSnapshotState,
    PrivateSession,
    RunAssignment,
    SnapshotSelection,
)
from brainoid_contracts.guards import protect
from brainoid_contracts.transactions import Document, Transaction
from brainoid_jobs import Job
from brainoid_jobs.models import job_id
from brainoid_memory.calendar_access import check_calendar_sources
from brainoid_memory.commands import PRE_START
from brainoid_memory.notifications import NotificationView, public_notification
from brainoid_memory.sources import CanonicalSources
from brainoid_policy.models import DeviceSubject
from brainoid_policy.scopes import PolicyDenied
from brainoid_runs.events import expires_at
from brainoid_runs.memory_sources import SavedMessages
from brainoid_runs.state import KIND, run_path
from pydantic import JsonValue

from brainoid_api.auth.device_tokens import DeviceIdentity
from brainoid_api.device_commands import DeviceCommands
from brainoid_api.device_leases import ConnectionBinding
from brainoid_api.errors import APIError, ErrorCode
from brainoid_api.repositories.conversations import Conversations, ReadResult, policy_call
from brainoid_api.repositories.memories import Memories
from brainoid_api.services.private_sessions import PrivateSessions

PAGE_SIZE = 20
MAX_ITEMS = 128


class DeviceSnapshots:
    def __init__(self, conversations: Conversations, memories: Memories, sessions: PrivateSessions):
        self.conversations, self.memories, self.sessions = conversations, memories, sessions
        self.commands = DeviceCommands(memories)

    def before(self, expiry: datetime) -> None:
        if self.conversations.now() >= expiry:
            raise APIError(ErrorCode.RESOURCE_EXPIRED)

    def _live_scope(self, current: AuthorizedScope) -> None:
        current.ensure_live(self.conversations.now())

    def run(
        self,
        tx: Transaction,
        identity: DeviceIdentity,
        session: PrivateSession | None,
        run: ResourceId,
        document: Document | None = None,
    ) -> RunAssignment | None:
        repo, tenant = self.conversations, identity.tenant_id
        if document is None:
            document = tx.get(f"tenants/{tenant}/jobs/{job_id(tenant, KIND, run)}")
        if document is None:
            return None
        job = Job.model_validate(document)
        output = job.output
        if (
            job.kind != KIND
            or job.tenant_id != tenant
            or job.operation_id != run
            or not isinstance(output, DeviceSubject)
        ):
            return None
        if output != DeviceSubject(
            device_id=identity.sub,
            connection_generation=identity.connection_generation,
            private_session_id=output.private_session_id,
        ) or (
            output.private_session_id is not None
            and (session is None or output.private_session_id != session.id)
        ):
            return None
        doc = tx.get(run_path(tenant, run))
        if (
            doc is None
            or doc.get("tenant_id") != tenant
            or doc.get("run_id") != run
            or repo.now() >= expires_at(doc)
        ):
            return None
        try:
            _, current = repo.access(
                tx, identity, tenant, doc["conversation_id"], output.private_session_id
            )
            # Preserve BOTH current original initiator and immutable destination gates,
            # including every Calendar source, exactly as at the run/SSE boundary.
            for subject in (job.grant.principal.subject, output):
                grant = repo.jobs._policy(tx).resource(
                    tenant, subject, job.grant.ref, output=output
                )
                check_calendar_sources(
                    tx,
                    repo.jobs._policy(tx),
                    grant.principal,
                    doc,
                    credential_epoch=repo.credentials.credential_epoch
                    if repo.credentials
                    else None,
                    sources=CanonicalSources(SavedMessages()),
                )
            protect(tx, partial(self._live_scope, current))
            protect(tx, partial(self.before, expires_at(doc)))
        except PolicyDenied:
            return None
        except APIError as error:
            if error.code not in {ErrorCode.RESOURCE_NOT_FOUND, ErrorCode.OPERATION_FORBIDDEN}:
                raise
            return None
        return RunAssignment(
            run_id=run,
            conversation_id=doc["conversation_id"],
            output_mode=doc.get("output_mode", "text"),
            assignment_generation=1,
            last_event_seq=doc.get("last_event_seq", 0),
            private_session_id=output.private_session_id,
        )

    def command(
        self, tx: Transaction, identity: DeviceIdentity, command: ResourceId
    ) -> ResourceId | None:
        try:
            record, access = self.commands._open(
                tx, ConnectionBinding(identity, "snapshot", 1), command
            )
            if (
                record.status in PRE_START | {"running", "outcome_unknown"}
                and self.conversations.now() < record.expires_at
            ):
                protect(tx, partial(self.before, record.expires_at))
                access.finish()
                return record.id
        except PolicyDenied:
            pass
        return None

    def notification(
        self,
        tx: Transaction,
        identity: DeviceIdentity,
        session: PrivateSession | None,
        notification: ResourceId,
        document: Document | None = None,
    ) -> NotificationView | None:
        if document is None:
            document = tx.get(f"tenants/{identity.tenant_id}/notifications/{notification}")
        if document is None or document.get("target", {}).get("id") != identity.sub:
            return None
        selected = session.id if session and document.get("visibility") == "private" else None
        if document.get("visibility") == "private" and selected is None:
            return None
        try:
            access = self.memories._open(tx, identity, identity.tenant_id, selected)
            value = NotificationView.model_validate(public_notification(access, notification))
            if (
                value.target_kind == "device"
                and str(value.target_id) == identity.sub
                and value.status not in {"displayed", "acknowledged", "expired"}
            ):
                protect(tx, access.finish)
                return value
        except PolicyDenied:
            pass
        return None

    def base(self, tx: Transaction, identity: DeviceIdentity) -> DeviceSnapshot:
        repo, tenant = self.conversations, identity.tenant_id
        principal, _ = repo.principal(tx, identity, tenant, None)
        session = self.sessions.snapshot(tx, identity, persist=False)
        if session:
            protect(tx, partial(self.before, min(session.expires_at, session.idle_expires_at)))
        scope = repo.scope(
            principal,
            None,
            Scope(visibility=Visibility.SHARED, display_policy=DisplayPolicy.NEVER),
            None,
        )
        stamp = fingerprint(
            [
                scope.binding(),
                identity.family_id,
                identity.credential_epoch,
                None if session is None else [session.id, session.version],
            ]
        )
        counter = tx.get(f"tenants/{tenant}/device_delivery_counters/{identity.sub}")
        return DeviceSnapshot(
            device_id=identity.sub,
            tenant_id=tenant,
            connection_generation=identity.connection_generation,
            snapshot_id=stamp,
            active_private_session=session,
            assigned_runs=[],
            pending_command_ids=[],
            pending_notification_ids=[],
            delivery_cursor=counter["sequence"] if counter else 0,
            next_cursor=None,
        )

    def collect(
        self, tx: Transaction, identity: DeviceIdentity, cursor: str | None = None
    ) -> DeviceSnapshot:
        result = self.base(tx, identity)

        class Bound:
            def binding(self) -> str:
                return result.snapshot_id

            def ensure_live(self, now: Any) -> None:
                if not identity.iat <= now.timestamp() < identity.exp:
                    raise APIError(ErrorCode.AUTHENTICATION_REQUIRED)
                session = result.active_private_session
                if session and now >= min(session.expires_at, session.idle_expires_at):
                    raise APIError(ErrorCode.OPERATION_FORBIDDEN)

        bound = Bound()
        query: JsonValue = {
            "operation": "device.snapshot",
            "device": identity.sub,
            "tenant": identity.tenant_id,
        }
        position = (
            "0:"
            if cursor is None
            else self.conversations.cursors.read(cursor, query=query, authorize=lambda: bound)
        )
        phase_text, separator, after = position.partition(":")
        if not separator or phase_text not in {"0", "1", "2"} or "/" in after:
            raise APIError(ErrorCode.INVALID_REQUEST)
        phase = int(phase_text)
        collections = (
            ("jobs", "output.device_id"),
            ("commands", "device_id"),
            ("notifications", "target.id"),
        )
        remaining = PAGE_SIZE
        runs, commands, notifications = [], [], []
        while phase < len(collections) and remaining:
            collection, field = collections[phase]
            # One extra row proves continuation. The SDK applies limit BEFORE
            # streaming/materialization; at most 23 candidates per request.
            rows = tx.query_page(
                f"tenants/{identity.tenant_id}/{collection}",
                {field: identity.sub},
                after=after or None,
                limit=remaining + 1,
            )
            for path, document in rows[:remaining]:
                after = path.rsplit("/", 1)[1]
                remaining -= 1
                if phase == 0:
                    run = self.run(
                        tx,
                        identity,
                        result.active_private_session,
                        ResourceId(document["operation_id"]),
                        document,
                    )
                    if run:
                        runs.append(run)
                elif phase == 1:
                    command = self.command(tx, identity, ResourceId(document["id"]))
                    if command:
                        commands.append(command)
                else:
                    notification = self.notification(
                        tx,
                        identity,
                        result.active_private_session,
                        ResourceId(document["id"]),
                        document,
                    )
                    if notification:
                        notifications.append(notification.id)
            if len(rows) > 0 and remaining == 0:
                break
            phase, after = phase + 1, ""
        next_cursor = (
            self.conversations.cursors.issue(
                position=f"{phase}:{after}",
                query=query,
                authorize=lambda: bound,
                lifetime=timedelta(minutes=5),
            )
            if phase < len(collections)
            else None
        )
        protect(tx, lambda: bound.ensure_live(self.conversations.now()))
        # Derived expiry writes must follow every read.
        self.sessions.snapshot(tx, identity)
        return result.model_copy(
            update={
                "assigned_runs": runs,
                "pending_command_ids": commands,
                "pending_notification_ids": notifications,
                "next_cursor": next_cursor,
            }
        )

    def page(
        self, identity: DeviceIdentity, cursor: str | None = None
    ) -> ReadResult[DeviceSnapshot]:
        return policy_call(
            lambda: self.conversations.current_read(lambda tx: self.collect(tx, identity, cursor))
        )

    def validate(
        self, identity: DeviceIdentity, selection: SnapshotSelection
    ) -> ReadResult[DeviceSnapshotState]:
        def read(tx: Transaction) -> DeviceSnapshotState:
            result = self.base(tx, identity)
            if result.snapshot_id != selection.snapshot_id:
                raise APIError(ErrorCode.INVALID_REQUEST)
            session = result.active_private_session
            # Combined request limit is 128, including all classes. Only exact
            # canonical IDs are read; no query/history scan happens here.
            runs = [
                value
                for run in selection.run_ids
                if (value := self.run(tx, identity, session, run))
            ]
            commands = [
                command_value
                for command in selection.command_ids
                if (command_value := self.command(tx, identity, command))
            ]
            notifications = [
                notification
                for notification in selection.notification_ids
                if self.notification(tx, identity, session, notification)
            ]
            self.sessions.snapshot(tx, identity)
            return DeviceSnapshotState.model_validate(
                result.model_dump()
                | {
                    "assigned_runs": runs,
                    "pending_command_ids": commands,
                    "pending_notification_ids": notifications,
                }
            )

        return policy_call(lambda: self.conversations.current_read(read))
