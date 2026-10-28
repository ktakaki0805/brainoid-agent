"""Daemon-only credentials, bounded durable reconciliation and authenticated HTTP."""

import asyncio
import logging
import secrets
import time
from collections.abc import Awaitable, Callable, Coroutine
from typing import TYPE_CHECKING, Any, TypeVar
from uuid import uuid4

import httpx
from brainoid_contracts import ResourceId
from pydantic import ValidationError

from brainoid_device.cloud_models import CloudError, Credentials, Journal, Pairing, Pending
from brainoid_device.cloud_wire import MAX_BYTES, decode, encode
from brainoid_device.config import CloudSettings
from brainoid_device.ledger import DeviceStorage
from brainoid_device.runtime import Runtime
from brainoid_device.storage import CredentialStore

if TYPE_CHECKING:
    from brainoid_device.snapshot_sync import SnapshotSync

T = TypeVar("T")
JOURNAL_NAME = "cloud-connection-v1"
RECONCILE_SECONDS = 55.0  # strictly inside the server's 60 second receipt grace


class CloudClient:
    def __init__(
        self,
        settings: CloudSettings,
        runtime: Runtime,
        storage: DeviceStorage,
        credentials: CredentialStore,
        *,
        now: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings, self.runtime, self.storage, self.store = (
            settings,
            runtime,
            storage,
            credentials,
        )
        self.now, self.sleep = now, sleep
        self._runtime_generation = runtime.generation
        self.generation = 0
        self.output_generation = 0
        self.recovery: SnapshotSync | None = None
        self.status = "unregistered"
        self.active = True
        self._tasks: set[asyncio.Task[None]] = set()
        self.auth_lock = asyncio.Lock()
        self.mutation_lock = asyncio.Lock()
        # HTTPX/httpcore debug logs can include raw headers. This daemon is the sole
        # owner of these outbound libraries; suppress their payload-bearing logging.
        for name in (
            "httpx",
            "httpcore",
            "httpcore.connection",
            "httpcore.http11",
            "httpcore.http2",
            "httpcore.proxy",
        ):
            logging.getLogger(name).disabled = True
        self.http = httpx.AsyncClient(
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(10.0, read=45.0),
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
            transport=transport,
        )
        self.journal = Journal(api=settings.api_url, installation_nonce=secrets.token_urlsafe(32))
        try:
            self.journal = Journal.model_validate(decode(credentials.read(JOURNAL_NAME)))
            if self.journal.api != settings.api_url or now() < self.journal.observed_at:
                self.journal.stopped = True
            self.status = (
                "stopped"
                if self.journal.stopped
                else "connecting"
                if self.journal.credentials
                else "unregistered"
            )
            self.active = not self.journal.stopped
        except FileNotFoundError:
            pass
        except (OSError, RuntimeError, ValueError):
            self.active, self.status = False, "stopped"
        runtime.register_private_cleanup(self.clear_private)

    def check(self, generation: int | None = None) -> None:
        if (
            not self.active
            or not self.runtime.permits_work(self._runtime_generation)
            or (generation is not None and generation != self.generation)
            or self.storage.snapshot().status not in {"ready", "unregistered"}
            or self.now() < self.journal.observed_at
        ):
            raise CloudError("cloud_stopped", 409)

    def check_output(
        self, session: ResourceId | None = None, generation: int | None = None
    ) -> None:
        self.check()
        if self.recovery is not None:
            self.recovery.check(session, generation)

    def save(self) -> None:
        self.check()
        self.journal.observed_at = self.now()
        try:
            self.store.write(JOURNAL_NAME, encode(self.journal.model_dump(mode="json")))
        except (OSError, RuntimeError, ValueError):
            self.runtime.clear_private()
            raise CloudError("cloud_storage_unavailable", 503) from None

    def clear_private(self) -> None:
        self.active = False
        self.generation += 1
        self.status = "stopped"
        self.journal.credentials = None
        self.journal.pairing = None
        self.journal.pending_auth = None
        self.journal.pending_mutation = None
        self.journal.cursors.clear()
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        for task in tuple(self._tasks):
            if task is not current:
                task.cancel()

    def stop(self) -> None:
        if self.active:
            self.journal.stopped = True
            self.journal.credentials = None
            self.journal.pairing = None
            self.journal.pending_auth = None
            self.journal.pending_mutation = None
            self.journal.cursors.clear()
            try:
                self.save()
            finally:
                self.runtime.clear_private()

    def start(self, work: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        try:
            self.check()
            if len(self._tasks) >= 16:
                raise CloudError("cloud_busy", 429)
        except Exception:
            work.close()
            raise
        task = self.runtime.start_task(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def owned(self, work: Coroutine[Any, Any, T]) -> T:
        if len(self._tasks) >= 16:
            work.close()
            raise CloudError("cloud_busy", 429)
        result: list[T] = []
        failures: list[CloudError] = []

        async def run() -> None:
            try:
                result.append(await work)
            except CloudError as error:
                # A request refusal is not a failed daemon resource. Unexpected
                # exceptions still reach Runtime's failure callback.
                failures.append(error)

        try:
            task = self.start(run())
        except Exception:
            work.close()
            raise
        await task
        if failures:
            raise failures[0]
        self.check()
        return result[0]

    async def close(self) -> None:
        self.clear_private()
        current = asyncio.current_task()
        tasks = [task for task in self._tasks if task is not current]
        if tasks:
            async with asyncio.timeout(2):
                await asyncio.gather(*tasks, return_exceptions=True)
        async with asyncio.timeout(2):
            await self.http.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        self.check()
        generation = self.generation
        # Internal callers construct every path from a typed operation and UUIDs.
        if not path.startswith("/v1/") or "//" in path or "#" in path:
            raise CloudError("invalid_operation", 400)
        request = httpx.Request(
            method,
            self.settings.api_url + path,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                **(headers or {}),
                **({"Content-Type": "application/json"} if body is not None else {}),
            },
            content=encode(body) if body is not None else None,
        )
        try:
            async with asyncio.timeout(12):
                response = await self.http.send(request, stream=True)
                try:
                    if response.headers.get("content-encoding", "identity") != "identity":
                        raise CloudError("invalid_cloud_response", 502)
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(data) + len(chunk) > MAX_BYTES:
                            raise CloudError("invalid_cloud_response", 502)
                        data.extend(chunk)
                    self.check(generation)
                    if response.status_code >= 300:
                        # Never copy remote error text, URLs, headers or credentials.
                        return response.status_code, {}
                    if response.headers.get("content-type", "").split(";")[0] != "application/json":
                        raise CloudError("invalid_cloud_response", 502)
                    return response.status_code, decode(bytes(data))
                finally:
                    await response.aclose()
        except (httpx.HTTPError, TimeoutError, OSError):
            self.check(generation)
            raise CloudError() from None

    def _binding(self, credentials: Credentials) -> None:
        self.check()
        registration = self.storage.register(credentials.tenant_id, credentials.device_id)
        self.storage.save_credential_reference(registration.epoch, "cloud_refresh", JOURNAL_NAME)

    def _install(self, value: dict[str, Any], pending: Pending) -> None:
        try:
            credentials = Credentials.model_validate(value)
            current = self.journal.credentials
            if current and (current.device_id, current.tenant_id) != (
                credentials.device_id,
                credentials.tenant_id,
            ):
                raise ValueError
        except (ValidationError, ValueError):
            self.stop()
            raise CloudError("invalid_cloud_response", 502) from None
        self.check()
        self.journal.credentials = credentials
        self.journal.credential_version += 1
        self.journal.access_until = pending.started + 900
        if pending.kind == "exchange":
            self.journal.family_until = pending.started + 30 * 86400
        self.journal.pending_auth = None
        self.journal.pairing = None
        self.journal.exchange_key = None
        self.save()  # atomic secret commit first; register() is idempotent after reopen
        self._binding(credentials)
        self.status = "connecting"

    async def _reconcile(
        self,
        pending: Pending,
        *,
        authenticated: bool = False,
        body_override: dict[str, Any] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        deadline = pending.started + (295 if pending.kind == "create" else RECONCILE_SECONDS)
        while pending.attempts < 3 and self.now() < deadline:
            self.check()
            previously_uncertain = pending.uncertain
            pending.uncertain = True
            pending.attempts += 1
            self.save()  # crash/response loss retains body, key and cumulative budget
            try:
                headers: dict[str, str] = {"Idempotency-Key": pending.key}
                if pending.session:
                    headers["X-Private-Session"] = pending.session
                async with asyncio.timeout(max(0.001, deadline - self.now())):
                    if authenticated:
                        result = await self.authorized(
                            "POST",
                            pending.path,
                            body=body_override if body_override is not None else pending.body,
                            headers=headers,
                        )
                    else:
                        result = await self.request(
                            "POST", pending.path, body=pending.body, headers=headers
                        )
                if result[0] < 500 and result[0] != 429:
                    if authenticated and result[0] >= 400 and not previously_uncertain:
                        pending.uncertain = False
                        self.save()
                    return result
            except TimeoutError:
                pass
            except CloudError as error:
                if error.code != "cloud_unavailable":
                    if pending.kind in {"create", "exchange", "refresh"}:
                        self.stop()
                    raise
            if pending.attempts < 3 and self.now() + 2 ** (pending.attempts - 1) < deadline:
                await self.sleep(2 ** (pending.attempts - 1))
        if pending.kind in {"create", "exchange", "refresh"}:
            self.stop()
        raise CloudError("reconciliation_required", 409)

    async def begin_pairing(self, name: str) -> dict[str, str]:
        async with self.auth_lock:
            self.check()
            if self.journal.credentials:
                raise CloudError("already_registered", 409)
            if self.journal.pairing:
                return self.public_pairing()
            pending = self.journal.pending_auth
            if pending is None:
                pending = Pending(
                    kind="create",
                    key=ResourceId(str(uuid4())),
                    path="/v1/device-pairings",
                    body={
                        "device_name": name,
                        "installation_nonce": self.journal.installation_nonce,
                    },
                    started=self.now(),
                )
                self.journal.pending_auth = pending
            if pending.kind != "create":
                raise CloudError("pairing_pending", 409)
            status, value = await self._reconcile(pending)
            if status != 201:
                self.stop()
                raise CloudError("pairing_refused", 409)
            try:
                pairing = Pairing.model_validate(value)
                if not self.now() < pairing.expires_at.timestamp() <= pending.started + 301:
                    raise ValueError
            except (ValueError, ValidationError):
                self.stop()
                raise CloudError("invalid_cloud_response", 502) from None
            self.journal.pairing, self.journal.pending_auth = pairing, None
            self.journal.exchange_key = ResourceId(str(uuid4()))
            self.status = "pairing"
            self.save()
            return self.public_pairing()

    def public_pairing(self) -> dict[str, str]:
        self.check()
        pairing = self.journal.pairing
        if not pairing or self.now() >= pairing.expires_at.timestamp():
            raise CloudError("pairing_expired", 410)
        return {"user_code": pairing.user_code, "expires_at": pairing.expires_at.isoformat()}

    async def exchange(self) -> bool:
        async with self.auth_lock:
            self.check()
            pairing = self.journal.pairing
            if pairing is None:
                return False
            if self.now() >= pairing.expires_at.timestamp():
                self.stop()
                raise CloudError("pairing_expired", 410)
            pending = self.journal.pending_auth
            if pending is None:
                assert self.journal.exchange_key is not None
                pending = Pending(
                    kind="exchange",
                    key=self.journal.exchange_key,
                    path=f"/v1/device-pairings/{pairing.pairing_id}:exchange",
                    body={"poll_secret": pairing.poll_secret},
                    started=self.now(),
                )
                self.journal.pending_auth = pending
            status, value = await self._reconcile(pending)
            if status == 409:
                # Definite pending response; same exchange key for the next poll.
                self.journal.pending_auth = None
                self.save()
                return False
            if status != 200:
                self.stop()
                raise CloudError("pairing_refused", 409)
            self._install(value, pending)
            return True

    async def token(
        self, *, rejected: str | None = None, rejected_version: int | None = None
    ) -> str:
        async with self.auth_lock:
            self.check()
            credentials = self.journal.credentials
            if credentials is None:
                raise CloudError("pairing_required", 409)
            if self.now() >= self.journal.family_until:
                self.stop()
                raise CloudError("credential_expired", 401)
            if (
                self.journal.pending_auth is None
                and self.now() < self.journal.access_until - 60
                and (
                    rejected is None
                    or rejected != credentials.access_token
                    or (
                        rejected_version is not None
                        and rejected_version != self.journal.credential_version
                    )
                )
            ):
                return credentials.access_token
            pending = self.journal.pending_auth
            if pending is None:
                pending = Pending(
                    kind="refresh",
                    key=ResourceId(str(uuid4())),
                    path="/v1/device-tokens:refresh",
                    body={
                        "device_id": credentials.device_id,
                        "refresh_token": credentials.refresh_token,
                        "refresh_request_id": str(uuid4()),
                    },
                    started=self.now(),
                )
                self.journal.pending_auth = pending
            if pending.kind != "refresh":
                raise CloudError("cloud_stopped", 409)
            status, value = await self._reconcile(pending)
            if status != 200:
                self.stop()
                raise CloudError("credential_refused", 401)
            self._install(value, pending)
            assert self.journal.credentials is not None
            return self.journal.credentials.access_token

    async def authorized(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        token = await self.token()
        version = self.journal.credential_version
        for attempt in range(2):
            status, value = await self.request(
                method,
                path,
                body=body,
                headers={**(headers or {}), "Authorization": "Bearer " + token},
            )
            if status not in {401, 403, 404}:
                return status, value
            if attempt == 0:
                # The server deliberately masks revoked devices as 404 in some
                # resource policies. A single serialized refresh distinguishes a
                # dead family from an ordinary private/resource denial.
                token = await self.token(rejected=token, rejected_version=version)
            elif status in {403, 404}:
                return status, value
        self.stop()
        raise CloudError("credential_refused", 401)

    async def maintain(self) -> None:
        try:
            if self.journal.credentials:
                self._binding(self.journal.credentials)
            while self.active:
                if self.journal.pending_auth and self.journal.pending_auth.kind == "create":
                    await self.begin_pairing(str(self.journal.pending_auth.body["device_name"]))
                elif self.journal.pairing:
                    await self.exchange()
                elif self.journal.credentials:
                    await self.token()
                await self.sleep(5)
        except CloudError:
            if self.active:
                self.stop()
        except Exception:
            # Disk/validation errors must leave no live transport using a cached
            # credential after the journal can no longer be committed safely.
            if self.active:
                self.stop()
        finally:
            await self.close()
