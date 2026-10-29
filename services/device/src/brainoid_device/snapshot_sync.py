"""All-page recovery with durable metadata only; notifications are an ID-keyed inbox."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Annotated, Any, Literal
from urllib.parse import urlencode

from brainoid_contracts import ResourceId
from pydantic import Field, ValidationError

from brainoid_device.cloud_client import CloudClient
from brainoid_device.cloud_models import CloudError, Record
from brainoid_device.cloud_wire import decode, encode
from brainoid_device.snapshot_models import (
    DeviceSnapshot,
    DeviceSnapshotState,
    NotificationView,
    PrivateSession,
    RunAssignment,
    SnapshotSelection,
)

JOURNAL = "snapshot-recovery-v1"
MAX_ITEMS = 128


class Progress(Record):
    snapshot_id: str
    cursor: str | None
    runs: Annotated[list[RunAssignment], Field(max_length=MAX_ITEMS)] = Field(default_factory=list)
    commands: Annotated[list[ResourceId], Field(max_length=MAX_ITEMS)] = Field(default_factory=list)
    notifications: Annotated[list[ResourceId], Field(max_length=MAX_ITEMS)] = Field(
        default_factory=list
    )


class Receipt(Record):
    id: ResourceId
    # Legacy audio deadline is accepted for journal compatibility, never used as TTL.
    expires: float = 0.0


class OutputStops(Record):
    api: str
    epoch: str
    device_id: ResourceId
    tenant_id: ResourceId
    observed_at: float
    sessions: Annotated[list[Receipt], Field(max_length=MAX_ITEMS)] = Field(default_factory=list)


class Recovery(Record):
    version: Literal[1] = 1
    api: str
    epoch: str
    device_id: ResourceId
    tenant_id: ResourceId
    observed_at: float = 0.0
    progress: Progress | None = None
    displayed: Annotated[list[Receipt], Field(max_length=MAX_ITEMS)] = Field(default_factory=list)


@dataclass
class NotificationRead:
    identity: ResourceId
    invalidated: bool = False


class SnapshotSync:
    def __init__(self, client: CloudClient) -> None:
        self.client = client
        self.state: DeviceSnapshotState | None = None
        self.notifications: dict[ResourceId, NotificationView] = {}
        self.recovery: Recovery | None = None
        self.changed = asyncio.Event()
        self.lock = asyncio.Lock()
        # One active notification read under the sync lock, and one scalar for
        # already-built local publications. No displayed-ID tombstone history.
        self.notification_read: NotificationRead | None = None
        self.display_generation = 0
        self.ready = False
        self.output_stops: OutputStops | None = None
        client.recovery = self
        client.runtime.register_private_cleanup(self.clear)

    def clear(self, *, persist: bool = True) -> None:
        self.client.clear_output()
        self.state = None
        self.notifications.clear()
        self.ready = False
        if persist and self.client.active and self.recovery is None:
            self.load()
        if self.recovery is not None:
            self.recovery.progress = None
            if persist and self.client.active:
                self.save()
        self.changed.set()

    def session_hint(self, kind: str, identity: ResourceId) -> None:
        current = self.session()
        # During initial/paged sync the response's session is not installed yet.
        # A committed revoke/replacement must still invalidate in-flight work.
        if current is None or (
            current.id == identity if kind == "session_revoked" else current.id != identity
        ):
            self.clear()
        self.changed.set()

    def fence(self, generation: int, output_generation: int) -> None:
        self.client.check(generation)
        self.session()  # Expiry also advances the output fence.
        if output_generation != self.client.output_generation:
            raise CloudError("cloud_unavailable")

    def session(self) -> PrivateSession | None:
        value = self.state.active_private_session if self.state else None
        if value and self.client.now() >= min(value.expires_at, value.idle_expires_at).timestamp():
            self.clear()
            self.changed.set()
            return None
        if value and self.stopped(value.id):
            return None
        return value

    def stops(self) -> OutputStops:
        recovery = self.load()
        if self.output_stops is None:
            try:
                stops = OutputStops.model_validate(
                    decode(self.client.store.read("private-output-stops-v1"))
                )
                if (stops.api, stops.epoch, stops.device_id, stops.tenant_id) != (
                    recovery.api,
                    recovery.epoch,
                    recovery.device_id,
                    recovery.tenant_id,
                ):
                    raise ValueError("wrong_binding")
                self.output_stops = stops
            except FileNotFoundError:
                self.output_stops = OutputStops(
                    api=recovery.api,
                    epoch=recovery.epoch,
                    device_id=recovery.device_id,
                    tenant_id=recovery.tenant_id,
                    observed_at=self.client.now(),
                )
            except (OSError, ValueError, RuntimeError):
                raise CloudError("cloud_storage_unavailable", 503) from None
        if self.client.now() < self.output_stops.observed_at:
            raise CloudError("clock_rollback", 409)
        return self.output_stops

    def stopped(self, identity: ResourceId) -> bool:
        stops = self.stops()
        now = self.client.now()
        expired = [item for item in stops.sessions if now >= item.expires]
        if expired:
            stops.sessions = [item for item in stops.sessions if now < item.expires]
            self.save_stops()
        return any(item.id == identity for item in stops.sessions)

    def save_stops(self) -> None:
        assert self.output_stops is not None
        self.output_stops.observed_at = self.client.now()
        try:
            self.client.store.write(
                "private-output-stops-v1", encode(self.output_stops.model_dump(mode="json"))
            )
        except (OSError, RuntimeError, ValueError):
            self.clear(persist=False)
            raise CloudError("cloud_storage_unavailable", 503) from None

    def stop_output(self, identity: ResourceId) -> None:
        # Device-local stop is not cloud delegation revocation. Persist metadata
        # before acknowledging, so a second tab or browser/service restart cannot
        # restore this session. Never perform a network read before stopping.
        stops = self.stops()
        if self.stopped(identity):
            self.save_stops()
            return
        current = self.state.active_private_session if self.state else None
        if current is None or current.id != identity:
            raise CloudError("private_session_unavailable", 409)
        if len(stops.sessions) >= MAX_ITEMS:
            self.clear()
            raise CloudError("cloud_storage_unavailable", 503)
        stops.sessions.append(Receipt(id=identity, expires=current.expires_at.timestamp()))
        self.clear(persist=False)
        self.save_stops()

    def check(self, session: ResourceId | None, generation: int | None = None) -> None:
        current = self.session()
        if generation is not None and generation != self.client.output_generation:
            raise CloudError("private_state_changed", 409)
        if session is not None and (not self.ready or current is None or current.id != session):
            raise CloudError("private_session_unavailable", 403)

    def load(self) -> Recovery:
        credentials = self.client.journal.credentials
        binding = self.client.storage.snapshot().registration
        if credentials is None or binding is None:
            raise CloudError("cloud_stopped", 409)
        if self.recovery is None:
            try:
                candidate = Recovery.model_validate(decode(self.client.store.read(JOURNAL)))
                if (candidate.api, candidate.epoch, candidate.device_id, candidate.tenant_id) != (
                    self.client.settings.api_url,
                    binding.epoch,
                    credentials.device_id,
                    credentials.tenant_id,
                ):
                    raise CloudError("invalid_cloud_response", 502)
                if self.client.now() < candidate.observed_at:
                    raise CloudError("clock_rollback", 409)
                self.recovery = candidate
            except FileNotFoundError:
                self.recovery = Recovery(
                    api=self.client.settings.api_url,
                    epoch=binding.epoch,
                    device_id=credentials.device_id,
                    tenant_id=credentials.tenant_id,
                )
            except (OSError, ValueError, RuntimeError):
                raise CloudError("cloud_storage_unavailable", 503) from None
        return self.recovery

    def save(self) -> None:
        self.client.check()
        assert self.recovery is not None
        if self.client.now() < self.recovery.observed_at:
            self.clear(persist=False)
            raise CloudError("clock_rollback", 409)
        self.recovery.observed_at = self.client.now()
        try:
            self.client.store.write(JOURNAL, encode(self.recovery.model_dump(mode="json")))
        except (OSError, RuntimeError, ValueError):
            self.clear(persist=False)
            raise CloudError("cloud_storage_unavailable", 503) from None

    async def sync(self) -> None:
        async with self.lock:
            client = self.client
            recovery = self.load()
            generation = client.generation
            output_generation = client.output_generation
            for _ in range(16):
                progress = recovery.progress
                if progress is not None and progress.cursor is None:
                    progress = recovery.progress = None
                path = f"/v1/devices/{recovery.device_id}/snapshot"
                if progress and progress.cursor:
                    path += "?" + urlencode({"cursor": progress.cursor})
                status, raw = await client.authorized("GET", path)
                self.fence(generation, output_generation)
                if status == 400 and progress:
                    # Current device/session authority changed while paging. Discard
                    # the prefix; continuation never grants access by itself.
                    recovery.progress = None
                    self.clear()
                    output_generation = client.output_generation
                    continue
                if status != 200:
                    if status in {403, 404, 410}:
                        self.clear()
                    raise CloudError(
                        "cloud_unavailable"
                        if status >= 500 or status in {403, 404, 410}
                        else "snapshot_refused",
                        status,
                    )
                try:
                    page = DeviceSnapshot.model_validate(raw)
                except ValidationError:
                    raise CloudError("invalid_cloud_response", 502) from None
                if (page.device_id, page.tenant_id) != (recovery.device_id, recovery.tenant_id):
                    raise CloudError("invalid_cloud_response", 502)
                session = page.active_private_session
                if session and (
                    session.status != "active"
                    or session.device_id != page.device_id
                    or session.tenant_id != page.tenant_id
                    or client.now() >= min(session.expires_at, session.idle_expires_at).timestamp()
                ):
                    self.clear()
                    raise CloudError("cloud_unavailable")
                old = self.session()
                if old and (
                    session is None
                    or (session.id, session.version, session.allowed_scopes)
                    != (old.id, old.version, old.allowed_scopes)
                ):
                    self.clear()
                    output_generation = client.output_generation
                    progress = None
                if progress is None:
                    progress = Progress(snapshot_id=page.snapshot_id, cursor=None)
                elif progress.snapshot_id != page.snapshot_id:
                    raise CloudError("invalid_cloud_response", 502)
                for run in page.assigned_runs:
                    if run.assignment_generation != 1 or (
                        run.private_session_id is not None
                        and (
                            session is None
                            or run.private_session_id != session.id
                            or "conversation" not in session.allowed_scopes
                        )
                    ):
                        raise CloudError("invalid_cloud_response", 502)
                    if any(old.run_id == run.run_id for old in progress.runs):
                        raise CloudError("invalid_cloud_response", 502)
                    progress.runs.append(run)
                progress.commands += page.pending_command_ids
                progress.notifications += page.pending_notification_ids
                if (
                    len(progress.runs) + len(progress.commands) + len(progress.notifications)
                    > MAX_ITEMS
                    or len(set(progress.commands)) != len(progress.commands)
                    or len(set(progress.notifications)) != len(progress.notifications)
                ):
                    self.clear()
                    raise CloudError("cloud_unavailable", 503)
                if page.next_cursor and page.next_cursor == progress.cursor:
                    raise CloudError("invalid_cloud_response", 502)
                progress.cursor = page.next_cursor
                recovery.progress = progress
                self.save()  # Only metadata, committed before requesting the next page.
                if page.next_cursor:
                    continue
                # Recheck every accumulated identifier against current authority.
                # A signed page cursor is a scan position, not a grant for its prefix.
                selection = SnapshotSelection(
                    snapshot_id=progress.snapshot_id,
                    run_ids=[r.run_id for r in progress.runs],
                    command_ids=progress.commands,
                    notification_ids=progress.notifications,
                )
                status, raw = await client.authorized(
                    "POST",
                    f"/v1/devices/{recovery.device_id}/snapshot:validate",
                    body=selection.model_dump(mode="json"),
                )
                self.fence(generation, output_generation)
                if status == 400:
                    self.clear()
                    output_generation = client.output_generation
                    continue
                if status != 200:
                    self.clear()
                    raise CloudError("cloud_unavailable")
                try:
                    state = DeviceSnapshotState.model_validate(raw)
                except ValidationError:
                    raise CloudError("invalid_cloud_response", 502) from None
                if (
                    state.device_id != page.device_id
                    or state.tenant_id != page.tenant_id
                    or state.snapshot_id != page.snapshot_id
                    or state.active_private_session != page.active_private_session
                    or not {r.run_id for r in state.assigned_runs} <= set(selection.run_ids)
                    or not set(state.pending_command_ids) <= set(selection.command_ids)
                    or not set(state.pending_notification_ids) <= set(selection.notification_ids)
                    or any(
                        r.assignment_generation != 1
                        or (
                            r.private_session_id is not None
                            and (
                                session is None
                                or r.private_session_id != session.id
                                or "conversation" not in session.allowed_scopes
                            )
                        )
                        for r in state.assigned_runs
                    )
                ):
                    raise CloudError("invalid_cloud_response", 502)
                if self.state and {
                    (r.run_id, r.assignment_generation) for r in self.state.assigned_runs
                } - {(r.run_id, r.assignment_generation) for r in state.assigned_runs}:
                    self.clear()
                    output_generation = client.output_generation
                self.state = state
                recovery.progress = None
                self.save()
                await self.refresh_notifications(generation, output_generation)
                self.fence(generation, output_generation)
                self.ready = True
                return
            # A large history yields cooperatively. Its durable cursor is resumed on
            # the next poll; no incomplete prefix becomes ready or gets published.
            self.state = None
            self.notifications.clear()
            self.ready = False

    async def refresh_notifications(self, generation: int, output_generation: int) -> None:
        assert self.state is not None
        page = self.state
        self.notifications = {
            key: value
            for key, value in self.notifications.items()
            if key in page.pending_notification_ids
        }
        recovery = self.load()
        # Unconfirmed display receipts remain until the canonical ACK is known to
        # have committed, even when the item temporarily leaves current scope.
        pending = list(
            dict.fromkeys(page.pending_notification_ids + [r.id for r in recovery.displayed])
        )
        for identity in pending:
            session = self.session()
            headers: dict[str, str] = {}
            reading = self.notification_read = NotificationRead(identity)
            try:
                status, body = await self.client.authorized(
                    "GET",
                    f"/v1/tenants/{page.tenant_id}/notifications/{identity}",
                    headers=headers,
                )
                self.fence(generation, output_generation)
                if status == 404 and session:
                    status, body = await self.client.authorized(
                        "GET",
                        f"/v1/tenants/{page.tenant_id}/notifications/{identity}",
                        headers={"X-Private-Session": session.id},
                    )
            finally:
                self.notification_read = None
            self.fence(generation, output_generation)
            if reading.invalidated:
                # A concurrently admitted display supersedes this older response,
                # even if its successful ACK has already retired the receipt.
                self.notifications.pop(identity, None)
                continue
            if status in {403, 404, 410}:
                self.notifications.pop(identity, None)
                continue
            if status != 200:
                raise CloudError()
            try:
                value = NotificationView.model_validate(body)
            except ValidationError:
                raise CloudError("invalid_cloud_response", 502) from None
            if (
                value.id != identity
                or value.tenant_id != page.tenant_id
                or value.target_kind != "device"
                or str(value.target_id) != page.device_id
            ):
                raise CloudError("invalid_cloud_response", 502)
            if value.visibility == "private" and (
                session is None or "reminders" not in session.allowed_scopes
            ):
                raise CloudError("invalid_cloud_response", 502)
            if value.status in {"displayed", "acknowledged"}:
                recovery.displayed = [r for r in recovery.displayed if r.id != identity]
                self.notifications.pop(identity, None)
                self.save()
                continue
            if value.status == "expired" or identity not in page.pending_notification_ids:
                continue
            if any(receipt.id == identity for receipt in recovery.displayed):
                try:
                    await self.displayed(identity, value=value)
                except CloudError as error:
                    # Automatic reconciliation is an item-level request. Its
                    # source/session can disappear after GET and before ACK;
                    # retain the receipt for later current-authority recovery.
                    # Credential refresh/revocation and unexpected failures still
                    # propagate through their existing runtime boundaries.
                    if not (
                        (error.code == "notification_refused" and error.status in {403, 404, 410})
                        or (error.code == "private_session_unavailable" and error.status == 403)
                        or (error.code == "notification_unavailable" and error.status == 404)
                    ):
                        raise
                    self.notifications.pop(identity, None)
            else:
                self.notifications[identity] = value

    async def displayed(
        self, identity: ResourceId, *, value: NotificationView | None = None
    ) -> None:
        value = value or self.notifications.get(identity)
        if value is None:
            raise CloudError("notification_unavailable", 404)
        session = self.session() if value.visibility == "private" else None
        if value.visibility == "private" and session is None:
            raise CloudError("private_session_unavailable", 403)
        recovery = self.load()
        # Persist the ID before acknowledging. Retrying after a response loss never
        # introduces a second inbox item, and never claims a person's confirmation.
        if not any(receipt.id == identity for receipt in recovery.displayed):
            if len(recovery.displayed) >= MAX_ITEMS:
                raise CloudError("cloud_unavailable", 503)
            recovery.displayed.append(Receipt(id=identity))
            self.save()
        self.display_generation += 1
        if self.notification_read and self.notification_read.identity == identity:
            self.notification_read.invalidated = True
        self.notifications.pop(identity, None)
        headers = {"Idempotency-Key": "display-" + str(identity)}
        if session:
            headers["X-Private-Session"] = session.id
        generation, output_generation = self.client.generation, self.client.output_generation
        status, _ = await self.client.authorized(
            "POST",
            f"/v1/tenants/{recovery.tenant_id}/notifications/{identity}:ack",
            headers=headers,
            body={"action": "displayed"},
        )
        self.fence(generation, output_generation)
        if status != 200:
            raise CloudError(
                "cloud_unavailable" if status >= 500 else "notification_refused", status
            )
        recovery.displayed = [r for r in recovery.displayed if r.id != identity]
        self.save()

    def check_publication(self, display: int, client: int, output: int) -> None:
        self.client.check(client)
        self.check(None, output)
        if display != self.display_generation:
            raise CloudError("snapshot_changed", 409)

    async def publication(self) -> tuple[dict[str, Any], Callable[[], None]]:
        # Capture every publication fence with the built value, before the owned
        # task hands it to the route. Response construction alone would be late.
        value = await self.public()
        return value, partial(
            self.check_publication,
            self.display_generation,
            self.client.generation,
            self.client.output_generation,
        )

    async def public(self) -> dict[str, Any]:
        # An inbox is not a reusable grant. Revalidate all current sources at
        # the local publication boundary, even if periodic sync just succeeded.
        await self.sync()
        self.session()
        state = self.state
        if state and state.active_private_session and self.stopped(state.active_private_session.id):
            state = state.model_copy(
                update={
                    "active_private_session": None,
                    "assigned_runs": [
                        run for run in state.assigned_runs if run.private_session_id is None
                    ],
                }
            )
        return {
            "ready": self.ready,
            "snapshot": state.model_dump(mode="json") if state else None,
            "notifications": [
                v.model_dump(mode="json") for v in self.notifications.values() if self.ready
            ],
            "play_speech": False,
        }

    async def run(self) -> None:
        while self.client.active:
            try:
                await self.sync()
            except CloudError as error:
                if error.code != "cloud_unavailable":
                    raise
            self.changed.clear()
            try:
                async with asyncio.timeout(2):
                    await self.changed.wait()
            except TimeoutError:
                pass

    async def expire(self) -> None:
        while self.client.active:
            self.session()
            await asyncio.sleep(0.1)
