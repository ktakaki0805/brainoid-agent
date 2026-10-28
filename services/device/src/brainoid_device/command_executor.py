"""Grant-gated HA execution; one durable execution, never an automatic POST retry."""

import asyncio
import math
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from brainoid_contracts import ResourceId

from brainoid_device.cloud_models import CloudError
from brainoid_device.cloud_wire import WS_VALIDATOR
from brainoid_device.command_journal import MAX_PENDING, CommandJournal, Execution
from brainoid_device.home_assistant import HomeAssistant, HomeError, Observation
from brainoid_device.ledger import LedgerConflict, StorageUnavailable

if TYPE_CHECKING:
    from brainoid_device.device_transport import DeviceTransport


def timestamp(value: str) -> float:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("timezone required")
    return result.timestamp()


class CommandClock:
    """Bound local drift, persist wall high-water, use server timestamps in reports.

    The deadline bound includes five seconds of clock/network uncertainty. Report
    timestamps use an actually received server time, never a fast Pi wall clock.
    """

    def __init__(self, now: Callable[[], float], monotonic: Callable[[], float]) -> None:
        self.wall, self.monotonic = now, monotonic
        self.last_wall = self.wall()
        self.last_mono = self.monotonic()
        self.server = 0.0
        self.sampled = self.last_mono

    def check(self) -> None:
        wall, mono = self.wall(), self.monotonic()
        if (
            not math.isfinite(wall)
            or wall < self.last_wall
            or mono < self.last_mono
            or abs((wall - self.last_wall) - (mono - self.last_mono)) > 1
        ):
            raise CloudError("command_clock_untrusted", 409)
        self.last_wall, self.last_mono = wall, mono

    def sample(self, value: str) -> None:
        self.check()
        server = timestamp(value)
        if abs(self.last_wall - server) > 5:
            raise CloudError("command_clock_untrusted", 409)
        if server >= self.server:
            self.server, self.sampled = server, self.last_mono

    def upper_ms(self) -> int:
        self.check()
        if not self.server:
            raise CloudError("command_clock_untrusted", 409)
        return math.ceil(
            max(self.last_wall, self.server + self.last_mono - self.sampled + 5) * 1000
        )

    def observed_at(self) -> str:
        return datetime.fromtimestamp(self.server, UTC).isoformat()


def matches(operation: dict[str, Any], observation: Observation) -> bool:
    action, parameters = operation["action"], operation["parameters"]
    if action == "light.turn_on":
        return (
            observation.power == "on"
            and observation.brightness_pct != 0
            and (
                "brightness_pct" not in parameters
                or parameters["brightness_pct"] == observation.brightness_pct
            )
        )
    if action == "light.turn_off":
        return observation.power == "off" and observation.brightness_pct in (None, 0)
    if action == "climate.set_temperature":
        return bool(parameters["temperature_c"] == observation.temperature_c)
    return bool(parameters["hvac_mode"] == observation.hvac_mode)


class CommandExecutor:
    def __init__(
        self,
        transport: "DeviceTransport",
        home: HomeAssistant,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.transport, self.client, self.home = transport, transport.client, home
        self.journal = CommandJournal(self.client.storage)
        self.monotonic = monotonic
        self.queue: asyncio.Queue[str] = asyncio.Queue(MAX_PENDING)
        self.queued: set[str] = set()
        self.clock: CommandClock | None = None
        self.epoch = ""
        self.generation = 0
        self.client_generation = 0
        self.output_generation = 0
        self.lease_until = 0.0
        self.client.runtime.register_private_cleanup(self.clear)

    def clear(self) -> None:
        self.clock = None
        self.queued.clear()
        self.queue = asyncio.Queue(MAX_PENDING)

    def begin(self, hello: dict[str, Any]) -> None:
        self.clear()
        self.client.check()
        registration = self.client.storage.snapshot().registration
        if registration is None:
            raise CloudError("cloud_stopped", 409)
        self.epoch, self.generation = registration.epoch, self.transport.generation
        self.client_generation = self.client.generation
        self.journal.prepare(self.epoch)
        self.clock = CommandClock(self.client.now, self.monotonic)
        self.sample(hello)
        self.fence()
        for identity in self.journal.pending(self.epoch):
            self.enqueue(identity)

    def sample(self, value: dict[str, Any]) -> None:
        if self.clock is None:
            raise CloudError()
        self.clock.sample(value["sent_at"])
        if value["type"] in {"hello_ack", "heartbeat_ack"}:
            self.clock.sample(value["payload"]["server_time"])
            self.lease_until = timestamp(value["payload"]["lease_expires_at"])

    def fence(self, *, execution: Execution | None = None, deadline: float | None = None) -> int:
        self.client.check(self.client_generation)
        registration = self.client.storage.snapshot().registration
        clock = self.clock
        if (
            clock is None
            or self.transport.socket is None
            or registration is None
            or registration.epoch != self.epoch
            or registration.connection_generation != self.generation
            or self.transport.generation != self.generation
        ):
            raise CloudError()
        upper = clock.upper_ms()
        self.journal.clock(self.epoch, int(clock.last_wall * 1000))
        if deadline is not None and upper >= min(deadline, self.lease_until) * 1000:
            raise CloudError("command_expired", 409)
        if execution is not None:
            session = execution.delivery.get("private_session_id")
            if session is not None:
                recovery = self.client.recovery
                if recovery is None:
                    raise CloudError("private_session_unavailable", 403)
                recovery.check(ResourceId(session), self.output_generation)
                current = recovery.session()
                if (
                    current is None
                    or "home.control" not in current.allowed_scopes
                    or upper >= min(current.expires_at, current.idle_expires_at).timestamp() * 1000
                ):
                    raise CloudError("private_session_unavailable", 403)
        return upper

    def enqueue(self, identity: str) -> None:
        if identity in self.queued:
            return
        try:
            self.queue.put_nowait(identity)
        except asyncio.QueueFull:
            raise CloudError() from None
        self.queued.add(identity)

    def receive(self, value: dict[str, Any]) -> None:
        if (
            not WS_VALIDATOR.is_valid(value)
            or value.get("connection_generation") != self.generation
        ):
            raise CloudError("invalid_cloud_response", 502)
        self.sample(value)
        self.fence()
        payload = value["payload"]
        if timestamp(payload["created_at"]) > timestamp(payload["expires_at"]):
            raise CloudError("invalid_cloud_response", 502)
        assert self.clock is not None
        receipt = self.journal.receive(
            self.epoch,
            self.generation,
            payload,
            now_ms=int(self.clock.server * 1000),
            observed_at=self.clock.observed_at(),
        )
        self.enqueue(receipt.command.command_id)

    async def _get(self, path: str, execution: Execution) -> dict[str, Any] | None:
        headers = {}
        if session := execution.delivery.get("private_session_id"):
            headers["X-Private-Session"] = session
        status, body = await self.client.authorized("GET", path, headers=headers)
        self.fence(execution=execution)
        if status in {403, 404, 410}:
            return None
        if status != 200:
            raise CloudError()
        return body

    async def canonical(self, value: Execution) -> dict[str, Any] | None:
        payload = value.delivery
        body = await self._get(
            f"/v1/tenants/{payload['tenant_id']}/commands/{value.command.command_id}", value
        )
        if body is None:
            return None
        if (
            body.get("id") != value.command.command_id
            or any(
                body.get(key) != payload[key]
                for key in ("tenant_id", "device_id", "target_entity_id", "operation")
            )
            or any(
                timestamp(body[key]) != timestamp(payload[key])
                for key in ("created_at", "expires_at")
            )
        ):
            raise CloudError("invalid_cloud_response", 502)
        return body

    async def target(self, value: Execution) -> str | None:
        payload = value.delivery
        entity = await self._get(
            f"/v1/tenants/{payload['tenant_id']}/commands/{value.command.command_id}/target", value
        )
        if entity is None:
            return None
        config = self.home.check()
        reference = entity.get("reference")
        if (
            entity.get("target_entity_id") != payload["target_entity_id"]
            or entity.get("tenant_id") != payload["tenant_id"]
            or entity.get("device_id") != payload["device_id"]
            or entity.get("command_id") != value.command.command_id
            or not isinstance(reference, dict)
            or reference.get("provider") != "home_assistant"
            or reference.get("integration_id") != config.integration_id
            or reference.get("external_id") not in config.allowed_entity_ids
        ):
            raise HomeError("home_entity_forbidden")
        return str(reference["external_id"])

    async def report(self, value: Execution) -> bool:
        report = value.reports[value.command.status]
        result = await self.transport.request("command_status", report)
        self.fence(execution=value)
        if result["payload"]["result"] != "recorded":
            return False
        if value.command.status != "accepted":
            self.journal.acknowledge(self.epoch, value)
        return True

    def finish(
        self,
        value: Execution,
        status: str,
        *,
        observation: Observation | None = None,
        code: str | None = None,
    ) -> Execution:
        now = self.fence()
        assert self.clock is not None
        report: dict[str, Any] = {
            "command_id": value.command.command_id,
            "execution_id": value.command.execution_id,
            "status": status,
            "observed_at": self.clock.observed_at(),
        }
        if observation is not None:
            report["observed_state"] = observation.model_dump(exclude_none=True)
        if code is not None:
            report["error_code"] = code
        self.journal.finish(self.epoch, report, now_ms=now)
        return self.journal.get(self.epoch, value.command.command_id)

    async def reconcile(self, value: Execution, external: str) -> None:
        # Only durable acceptance permits an observation to establish success.
        # A crash/POST response loss leaves acceptance unknown, even if the light
        # happens to match. Reading current state never authorizes another POST.
        result = await self.home.observe(external)
        self.fence(execution=value)
        observed = result.observed_state
        success = (
            value.ha_accepted
            and observed is not None
            and matches(value.delivery["operation"], observed)
        )
        finished = self.finish(
            value,
            "succeeded" if success else "outcome_unknown",
            observation=observed,
            code=None if success else "home_outcome_unconfirmed",
        )
        await self.report(finished)

    async def execute(self, identity: str) -> None:
        self.output_generation = self.client.output_generation
        value = self.journal.get(self.epoch, identity)
        self.fence(execution=value)
        if value.command.result_acked or value.settled:
            return
        # Terminal retries need no provider call or renewed execution grant; the
        # server rechecks authority before it accepts this exact saved payload.
        if value.command.status not in {"accepted", "started"}:
            await self.report(value)
            return
        current = await self.canonical(value)
        if current is None:
            return
        if value.command.status == "started":
            if current.get("execution_id") != value.command.execution_id:
                raise CloudError("invalid_cloud_response", 502)
            external = await self.target(value)
            if external is not None:
                await self.reconcile(value, external)
            return
        if current.get("status") in {"cancelled", "expired"}:
            # Cancellation/expiry is authoritative; no grant or provider access.
            if current["status"] == "expired":
                await self.report(self.finish(value, "expired"))
            else:
                self.journal.cancelled(self.epoch, identity, now_ms=self.fence())
            return
        expires = timestamp(value.delivery["expires_at"])
        if self.fence() >= expires * 1000:
            if current.get("execution_id") == value.command.execution_id:
                await self.report(self.finish(value, "failed", code="execution_not_started"))
            # The conservative clock bound can expire locally before the server.
            # Keep accepted until the server confirms expiry; never send early.
            return
        external = await self.target(value)
        if external is None or not await self.report(value):
            return
        granted = await self.transport.request(
            "command_start",
            {
                "command_id": identity,
                "execution_id": value.command.execution_id,
            },
        )
        grant = granted["payload"]
        if grant["result"] != "granted":
            if (
                grant.get("reason_code") == "expired"
                and current.get("execution_id") == value.command.execution_id
            ):
                await self.report(self.finish(value, "failed", code="execution_not_started"))
            return
        deadline = min(expires, timestamp(grant["expires_at"]))
        now = self.fence(execution=value, deadline=deadline)
        # Record start before even entering the adapter. Ambiguous commit aborts
        # execution; reopened storage decides whether this grant was consumed.
        started = self.client.storage.record_started(
            self.epoch,
            identity,
            value.command.execution_id,
            self.generation,
            grant_expires_at_ms=int(deadline * 1000),
            now_ms=now,
            clock_trusted=True,
        )
        if not started:
            return

        async def authorize() -> None:
            try:
                self.fence(execution=value, deadline=deadline)
                # HA capability reads await external I/O. Recheck the server's
                # current target fingerprint/actor/session after those reads.
                checked = await self.transport.request(
                    "command_start",
                    {
                        "command_id": identity,
                        "execution_id": value.command.execution_id,
                    },
                )
                if checked["payload"]["result"] != "granted" or timestamp(
                    checked["payload"]["expires_at"]
                ) != timestamp(grant["expires_at"]):
                    raise CloudError("command_refused", 409)
                self.fence(execution=value, deadline=deadline)
            except (CloudError, LedgerConflict, StorageUnavailable):
                raise HomeError("home_execution_fenced") from None

        result = await self.home.set_state(
            external, value.delivery["operation"], authorize=authorize
        )
        self.fence(execution=value)
        if result.status == "accepted":
            self.journal.accepted(self.epoch, identity)
            await self.reconcile(self.journal.get(self.epoch, identity), external)
        else:
            finished = self.finish(
                value,
                "failed" if result.status == "failed" else "outcome_unknown",
                code=result.code or "home_outcome_unconfirmed",
            )
            await self.report(finished)

    async def run(self) -> None:
        try:
            while True:
                try:
                    async with asyncio.timeout(2):
                        identity = await self.queue.get()
                except TimeoutError:
                    now = self.fence()
                    self.journal.prune(self.epoch, now_ms=now)
                    self.client.storage.prune_commands(now_ms=now, clock_trusted=True)
                    for pending in self.journal.pending(self.epoch):
                        self.enqueue(pending)
                    continue
                try:
                    await self.execute(identity)
                except HomeError:
                    # Configuration or exact-target refusal grants no execution.
                    pass
                except CloudError as error:
                    if error.code not in {
                        "private_session_unavailable",
                        "private_state_changed",
                        "command_expired",
                    }:
                        raise
                finally:
                    self.queued.discard(identity)
        except (LedgerConflict, StorageUnavailable):
            self.client.stop()
        except CloudError as error:
            if error.code != "cloud_unavailable":
                self.client.stop()
        finally:
            if self.transport.socket is not None:
                await self.transport.close_socket(self.transport.socket)
