"""Locally confirmed, daemon-owned reset; remote revocation is never inferred."""

import asyncio
import secrets
import time
from dataclasses import dataclass

from brainoid_device.ledger import DeviceStorage
from brainoid_device.runtime import Runtime
from brainoid_device.storage import CredentialStore


@dataclass(frozen=True)
class ResetConfirmation:
    token: str
    expires: float


class ResetCommand:
    def __init__(
        self, runtime: Runtime, storage: DeviceStorage, credentials: CredentialStore
    ) -> None:
        self.runtime = runtime
        self.storage = storage
        self.credentials = credentials
        self.status = "idle"
        self._confirmations: dict[str, ResetConfirmation] = {}
        self._task: asyncio.Task[bool] | None = None

    def prepare(self, session: str) -> ResetConfirmation:
        now = time.monotonic()
        self._confirmations = {k: v for k, v in self._confirmations.items() if v.expires > now}
        confirmation = ResetConfirmation(secrets.token_urlsafe(32), now + 60)
        self._confirmations[session] = confirmation
        return confirmation

    def confirm(self, session: str, token: str) -> bool:
        confirmation = self._confirmations.pop(session, None)
        return bool(
            confirmation
            and confirmation.expires > time.monotonic()
            and secrets.compare_digest(confirmation.token, token)
        )

    async def execute(self) -> bool:
        if self._task is not None and not self._task.done():
            return False
        # Owned outside Runtime, whose resources this command must drain. Shield
        # disconnect/cancellation of an HTTP caller from interrupting the erasure.
        self._task = asyncio.create_task(self._execute())
        return await asyncio.shield(self._task)

    async def _execute(self) -> bool:
        self.status = "pending"
        self._confirmations.clear()
        self.runtime.fence_operations()
        intent = False
        try:
            self.credentials.begin_reset()
            intent = True
        except Exception:
            pass
        # Clear display/audio/token memory immediately; repeat after cancellation
        # finally blocks have completed, before allowing durable intent removal.
        cleared = True
        try:
            self.runtime.clear_private()
        except Exception:
            cleared = False
        try:
            await self.runtime.close()
        except Exception:
            self.status = "failed"
            return False
        if not intent or not cleared:
            self.status = "failed"
            return False
        complete = self.storage.local_reset()
        self.status = "complete" if complete else "failed"
        return complete

    async def finish(self) -> None:
        if self._task is not None:
            await asyncio.shield(self._task)
