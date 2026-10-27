"""Durable transport generations, independent of the credential authorization fence."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

from brainoid_contracts import ResourceId
from brainoid_contracts.guards import GuardedTransactions, protect
from brainoid_contracts.transactions import Document, Transaction, TransactionStore

from brainoid_api.auth.device_tokens import DeviceIdentity
from brainoid_api.errors import APIError, ErrorCode
from brainoid_api.repositories.device_credentials import DeviceCredentialRepository

HEARTBEAT_SECONDS = 15
LEASE_SECONDS = 3 * HEARTBEAT_SECONDS


@dataclass(frozen=True)
class ConnectionBinding:
    """Server-created authority: signed credential fence and separate wire generation."""

    identity: DeviceIdentity
    connection_id: str
    generation: int

    @property
    def tenant_id(self) -> ResourceId:
        return self.identity.tenant_id

    @property
    def device_id(self) -> ResourceId:
        return self.identity.sub

    @property
    def device_path(self) -> str:
        return f"tenants/{self.tenant_id}/devices/{self.device_id}"


class DeviceLeases:
    def __init__(
        self,
        store: TransactionStore,
        credentials: DeviceCredentialRepository,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = GuardedTransactions(store)
        self.credentials, self.now = credentials, now

    @staticmethod
    def path(identity: DeviceIdentity) -> str:
        return f"tenants/{identity.tenant_id}/device_connections/{identity.sub}"

    def _credential(self, tx: Transaction, identity: DeviceIdentity) -> None:
        self.credentials.authorize(tx, identity)

        def live() -> None:
            if not identity.iat <= self.now().timestamp() < identity.exp:
                raise APIError(ErrorCode.AUTHENTICATION_REQUIRED)

        protect(tx, live)

    def acquire(
        self,
        identity: DeviceIdentity,
        connection_id: str,
        *,
        admit: Callable[[], None] | None = None,
    ) -> ConnectionBinding:
        if admit is not None:
            admit()

        def acquire(tx: Transaction) -> ConnectionBinding:
            if admit is not None:
                protect(tx, admit)
            self._credential(tx, identity)
            path = self.path(identity)
            previous = tx.get(path)
            generation = (previous["generation"] if previous else 0) + 1
            binding = ConnectionBinding(identity, connection_id, generation)
            tx.set(
                path,
                {
                    "generation": generation,
                    "connection_id": connection_id,
                    "authorization_version": identity.authorization_version,
                    "credential_generation": identity.connection_generation,
                    "family_id": identity.family_id,
                    "credential_epoch": identity.credential_epoch,
                    "expires_at": self.now() + timedelta(seconds=LEASE_SECONDS),
                    "online": True,
                },
            )
            return binding

        return self._store.run(acquire)

    def current(self, tx: Transaction, binding: ConnectionBinding) -> Document:
        self._credential(tx, binding.identity)
        lease = tx.get(self.path(binding.identity))
        if (
            lease is None
            or lease.get("generation") != binding.generation
            or lease.get("connection_id") != binding.connection_id
            or lease.get("online") is not True
            or lease.get("authorization_version") != binding.identity.authorization_version
            or lease.get("credential_generation") != binding.identity.connection_generation
            or lease.get("family_id") != binding.identity.family_id
            or lease.get("credential_epoch") != binding.identity.credential_epoch
        ):
            raise APIError(ErrorCode.AUTHENTICATION_REQUIRED)
        expiry = lease["expires_at"]

        def live() -> None:
            if self.now() >= expiry:
                raise APIError(ErrorCode.AUTHENTICATION_REQUIRED)

        protect(tx, live)
        return lease

    def run[T](
        self,
        binding: ConnectionBinding,
        work: Callable[[Transaction], T],
        *,
        admit: Callable[[], None] | None = None,
    ) -> T:
        """Admit work and its writes in the SAME transaction as the generation read.

        Callback retries must have no external effects. DEV-05 must additionally
        check command/session DeviceTarget using identity.connection_generation;
        the wire generation is never substituted for that credential fence.
        """

        if admit is not None:
            admit()

        def checked(tx: Transaction) -> T:
            if admit is not None:
                protect(tx, admit)
            self.current(tx, binding)
            # Policy/lease reads may block; submission is not callback admission.
            if admit is not None:
                admit()
            return work(tx)

        return self._store.run(checked)

    def check(self, binding: ConnectionBinding) -> datetime:
        return cast(datetime, self._store.run(lambda tx: self.current(tx, binding)["expires_at"]))

    def heartbeat(
        self, binding: ConnectionBinding, *, admit: Callable[[], None] | None = None
    ) -> datetime:
        if admit is not None:
            admit()

        def renew(tx: Transaction) -> datetime:
            if admit is not None:
                protect(tx, admit)
            lease = self.current(tx, binding)
            expires = self.now() + timedelta(seconds=LEASE_SECONDS)
            tx.set(self.path(binding.identity), {**lease, "expires_at": expires})
            return expires

        return self._store.run(renew)

    def release(self, identity: DeviceIdentity, connection_id: str) -> None:
        """CAS by unique owner, including acquire response loss; retain generation.

        No live authority is required to relinquish one's own expired/revoked lease.
        A failed cleanup leaves only a lease which expires within 45 seconds.
        """

        def release(tx: Transaction) -> None:
            path = self.path(identity)
            lease = tx.get(path)
            if lease is not None and lease.get("connection_id") == connection_id:
                tx.set(path, {**lease, "online": False, "expires_at": self.now()})

        self._store.run(release)
