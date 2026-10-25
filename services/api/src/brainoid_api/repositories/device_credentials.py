"""Atomic pairing claims, bounded encrypted replay, and rotating credential families."""

import hashlib
import hmac
import secrets
import string
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from uuid import uuid4

from brainoid_contracts import ResourceId, UtcTimestamp, Version
from brainoid_policy.control_ledger import ControlLedger
from brainoid_policy.models import ManagementAction, UserSubject
from pydantic import BaseModel, ConfigDict, Field

from brainoid_api.auth.authorization import TransactionPolicyState, policy_call, transaction_policy
from brainoid_api.auth.context import AuthenticatedIdentity
from brainoid_api.auth.device_tokens import DeviceIdentity, DeviceTokens
from brainoid_api.common.access import fingerprint
from brainoid_api.common.transactions import Document, Transaction, TransactionStore
from brainoid_api.errors import APIError, ErrorCode
from brainoid_api.repositories.tenants import Name, Tenants
from brainoid_api.services.device_authorization import (
    DeviceAuthorization,
    DeviceBinding,
    validate_entities,
)

PAIRING_LIFETIME = timedelta(minutes=5)
REPLAY_GRACE = timedelta(seconds=60)
FAMILY_LIFETIME = timedelta(days=30)
Code = Annotated[str, Field(strict=True, pattern=r"^[A-Z0-9]{8}$")]
Secret = Annotated[str, Field(strict=True, min_length=32, max_length=256)]


class Input(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


class PairingCreate(Input):
    device_name: Name
    installation_nonce: Annotated[
        str, Field(strict=True, min_length=32, max_length=128, repr=False)
    ]


class Pairing(Input):
    pairing_id: ResourceId
    user_code: Code = Field(repr=False)
    poll_secret: Secret = Field(repr=False)
    expires_at: UtcTimestamp


class PairingConfirm(Input):
    user_code: Code = Field(repr=False)
    device_name: Name
    allowed_entity_ids: Annotated[list[ResourceId], Field(max_length=100)]
    replaces_device_id: ResourceId | None = None


class PairingExchange(Input):
    poll_secret: Secret = Field(repr=False)


class DeviceRefresh(Input):
    device_id: ResourceId
    refresh_token: Annotated[str, Field(strict=True, min_length=32, max_length=512, repr=False)]
    refresh_request_id: ResourceId


class DeviceCredentials(Input):
    device_id: ResourceId
    tenant_id: ResourceId
    access_token: str = Field(repr=False)
    refresh_token: str = Field(repr=False)
    expires_in: Literal[900] = 900
    token_type: Literal["Bearer"] = "Bearer"


class Device(Input):
    id: ResourceId
    tenant_id: ResourceId
    version: Version
    created_at: UtcTimestamp
    updated_at: UtcTimestamp
    name: Name
    status: Literal["active", "revoked", "offline", "suspended"]
    capabilities: list[str]
    allowed_entity_ids: list[ResourceId]
    connection_generation: Version
    authorization_version: Version
    allowed_scopes: list[str]
    allowed_room_ids: list[ResourceId]


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _key(key: str | None) -> str:
    if key is None or not 16 <= len(key) <= 128:
        raise APIError(ErrorCode.INVALID_REQUEST)
    return key


def _result[T](result: T | ErrorCode) -> T:
    # Error outcomes are raised AFTER commit so brute-force counters/reuse fences survive.
    if isinstance(result, ErrorCode):
        raise APIError(result)
    return result


class DeviceCredentialRepository:
    def __init__(
        self,
        store: TransactionStore,
        tenants: Tenants,
        ledger: ControlLedger,
        tokens: DeviceTokens,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store, self._tenants, self._ledger, self._tokens = store, tenants, ledger, tokens
        self._now = now

    def _rate(
        self, operation: str, ip: str, subject: str, ip_limit: int, subject_limit: int
    ) -> None:
        now = self._now()

        def admit(tx: Transaction) -> bool:
            entries = []
            for kind, identity, limit in (
                ("ip", ip, ip_limit),
                ("subject", subject, subject_limit),
            ):
                path = "device_rate_limits/" + self._tokens.digest(operation + kind, identity)
                current = tx.get(path)
                count = current["count"] if current and now < current["expires_at"] else 0
                expires = current["expires_at"] if current and count else now + PAIRING_LIFETIME
                if count >= limit:
                    return False
                entries.append((path, {"count": count + 1, "expires_at": expires}))
            for path, document in entries:
                tx.set(path, document)
            return True

        if not self._store.run(admit):
            raise APIError(ErrorCode.RATE_LIMITED)

    def _binding(self, operation: str, identity: str, request: str, generation: int = 0) -> str:
        return fingerprint(
            ["device-receipt-v1", str(self._tokens.epoch), operation, identity, request, generation]
        )

    def create(self, body: PairingCreate, key: str | None, ip: str) -> Pairing:
        key = _key(key)
        self._rate("create", ip, body.installation_nonce, 20, 5)
        request = fingerprint(body.model_dump(mode="json"))
        receipt_path = "device_pairing_requests/" + self._tokens.digest(
            "create", body.installation_nonce + "\0" + key
        )
        binding = self._binding("create", receipt_path, request)
        for _ in range(16):
            code = "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))
            selector_path = "device_pairing_selectors/" + self._tokens.digest("selector", code[:4])
            pairing = Pairing(
                pairing_id=ResourceId(str(uuid4())),
                user_code=code,
                poll_secret=secrets.token_urlsafe(32),
                expires_at=self._now() + PAIRING_LIFETIME,
            )

            def create(
                tx: Transaction,
                pairing: Pairing = pairing,
                code: str = code,
                selector_path: str = selector_path,
            ) -> Pairing | ErrorCode:
                saved = tx.get(receipt_path)
                if saved:
                    if saved["request"] != request:
                        return ErrorCode.IDEMPOTENCY_CONFLICT
                    if saved["epoch"] != self._tokens.epoch or self._now() >= saved["expires_at"]:
                        return ErrorCode.RESOURCE_EXPIRED
                    current = tx.get("device_pairings/" + saved["pairing_id"])
                    if current is None or current["status"] not in ("pending", "confirmed"):
                        return ErrorCode.RESOURCE_EXPIRED
                    return Pairing.model_validate_json(self._tokens.open(saved["receipt"], binding))
                selector = tx.get(selector_path)
                if selector and self._now() < selector["expires_at"]:
                    return ErrorCode.CONFLICT
                tx.create(
                    "device_pairings/" + pairing.pairing_id,
                    {
                        "id": pairing.pairing_id,
                        "epoch": self._tokens.epoch,
                        "code_hash": self._tokens.digest("code", code),
                        "poll_hash": _hash(pairing.poll_secret),
                        "status": "pending",
                        "failures": 0,
                        "expires_at": pairing.expires_at,
                        "installation": self._tokens.digest(
                            "installation", body.installation_nonce
                        ),
                    },
                )
                tx.set(
                    selector_path,
                    {"pairing_id": pairing.pairing_id, "expires_at": pairing.expires_at},
                )
                tx.create(
                    receipt_path,
                    {
                        "request": request,
                        "epoch": self._tokens.epoch,
                        "pairing_id": pairing.pairing_id,
                        "expires_at": pairing.expires_at,
                        "receipt": self._tokens.seal(pairing.model_dump_json(), binding),
                    },
                )
                return pairing

            result = self._store.run(create)
            if result != ErrorCode.CONFLICT:
                return _result(result)
        raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)

    def confirm(
        self,
        identity: AuthenticatedIdentity,
        tenant_id: ResourceId,
        body: PairingConfirm,
        key: str | None,
        ip: str,
        request_id: str | None = None,
    ) -> Device:
        key = _key(key)
        self._rate("confirm", ip, identity.user_id, 60, 30)
        request = fingerprint(
            [identity.user_id, str(tenant_id), key, body.model_dump(mode="json", exclude_none=True)]
        )
        selector_path = "device_pairing_selectors/" + self._tokens.digest(
            "selector", body.user_code[:4]
        )

        def confirm(tx: Transaction) -> Device | ErrorCode:
            self._tenants.authorize_user(tx, identity)
            policy = transaction_policy(
                tx, now=self._now, ledger=self._ledger, allow_untracked_bootstrap=True
            )
            policy_call(
                lambda: policy.management(
                    tenant_id,
                    UserSubject(user_id=identity.user_id),
                    ManagementAction.MANAGE_DEVICES,
                )
            )
            selector = tx.get(selector_path)
            if not selector:
                return ErrorCode.RESOURCE_NOT_FOUND
            path = "device_pairings/" + selector["pairing_id"]
            pairing = tx.get(path)
            if not pairing:
                return ErrorCode.RESOURCE_NOT_FOUND
            if (
                pairing["epoch"] != self._tokens.epoch
                or self._now() >= pairing["expires_at"]
                or pairing["status"] == "expired"
            ):
                return ErrorCode.RESOURCE_EXPIRED
            if not hmac.compare_digest(
                pairing["code_hash"], self._tokens.digest("code", body.user_code)
            ):
                if pairing["status"] == "pending":
                    pairing["failures"] += 1
                    if pairing["failures"] >= 5:
                        pairing["status"] = "expired"
                    tx.set(path, pairing)
                return ErrorCode.RESOURCE_NOT_FOUND
            if pairing["status"] != "pending":
                if pairing.get("confirm_request") != request:
                    return ErrorCode.IDEMPOTENCY_CONFLICT
                document = self._device(tx, pairing["tenant_id"], pairing["device_id"])
                if any(
                    pairing.get(field, 1) != document[field]
                    for field in ("authorization_version", "connection_generation")
                ):
                    return ErrorCode.AUTHENTICATION_REQUIRED
                return self._view(document)
            validate_entities(tx, tenant_id, body.allowed_entity_ids)
            installation_path = "device_installations/" + pairing["installation"]
            installation = tx.get(installation_path)
            previous = None
            if installation is not None:
                if (
                    installation["tenant_id"] != tenant_id
                    or installation["device_id"] != body.replaces_device_id
                ):
                    return ErrorCode.CONFLICT
                state = TransactionPolicyState(
                    tx, ledger=self._ledger, allow_untracked_bootstrap=False
                )
                previous = policy_call(lambda: state.device(tenant_id, installation["device_id"]))
                if previous is None or previous.status != "revoked":
                    return ErrorCode.CONFLICT
                prior = tx.get(f"tenants/{tenant_id}/devices/{installation['device_id']}")
                if prior is None or prior.get("device_mutation") is not None:
                    return ErrorCode.CONFLICT
                validate_entities(tx, tenant_id, (), previous.allowed_room_ids)
            elif body.replaces_device_id is not None:
                return ErrorCode.CONFLICT
            device_id = ResourceId(str(uuid4()))
            now = self._now()
            device = {
                "id": device_id,
                "tenant_id": tenant_id,
                "name": body.device_name,
                "version": 1,
                "authorization_version": 1,
                "connection_generation": 1,
                "created_at": now,
                "updated_at": now,
                "status": "active",
                "capabilities": [] if previous is None else sorted(previous.capabilities),
                "allowed_scopes": sorted(previous.allowed_scopes)
                if previous is not None
                else [
                    "shared",
                    "conversation",
                    "memory.read",
                    "memory.write",
                    "notes",
                    "tasks",
                    "reminders",
                ],
                "allowed_entity_ids": list(dict.fromkeys(body.allowed_entity_ids)),
                "allowed_room_ids": [] if previous is None else sorted(previous.allowed_room_ids),
            }
            tx.set(
                installation_path,
                {
                    "tenant_id": tenant_id,
                    "device_id": device_id,
                    "original_device_id": (installation or {}).get(
                        "original_device_id", (installation or {}).get("device_id", device_id)
                    ),
                },
            )
            tx.create(f"tenants/{tenant_id}/devices/{device_id}", device)
            tx.set(
                path,
                {
                    **pairing,
                    "status": "confirmed",
                    "tenant_id": tenant_id,
                    "device_id": device_id,
                    "confirm_request": request,
                    "authorization_version": 1,
                    "connection_generation": 1,
                },
            )
            tx.create(
                f"tenants/{tenant_id}/audit_events/{pairing['id']}",
                {
                    "actor_user_id": identity.user_id,
                    "target_device_id": device_id,
                    "result": "succeeded",
                    "request_id": request_id or str(pairing["id"]),
                    "at": now,
                },
            )
            return self._view(device)

        return _result(self._store.run(confirm))

    @staticmethod
    def _view(document: Document) -> Device:
        return Device.model_validate({field: document[field] for field in Device.model_fields})

    def _device(self, tx: Transaction, tenant_id: ResourceId, device_id: ResourceId) -> Document:
        path = f"tenants/{tenant_id}/devices/{device_id}"
        document = tx.get(path)
        if not document:
            raise APIError(ErrorCode.AUTHENTICATION_REQUIRED)
        DeviceAuthorization(self._ledger, now=self._now).current(
            tx,
            DeviceBinding(
                tenant_id=tenant_id,
                device_id=device_id,
                authorization_version=document["authorization_version"],
                connection_generation=document["connection_generation"],
            ),
        )
        return document

    def _family(self, tx: Transaction, family_id: ResourceId) -> tuple[Document, Document]:
        family = tx.get("device_credential_families/" + family_id)
        if (
            not family
            or family["status"] != "active"
            or family["epoch"] != self._tokens.epoch
            or self._now() >= family["expires_at"]
        ):
            raise APIError(ErrorCode.AUTHENTICATION_REQUIRED)
        device = self._device(tx, family["tenant_id"], family["device_id"])
        if any(
            family[field] != device[field]
            for field in ("authorization_version", "connection_generation")
        ):
            raise APIError(ErrorCode.AUTHENTICATION_REQUIRED)
        return family, device

    def _credentials(self, device: Document, family_id: ResourceId) -> DeviceCredentials:
        return DeviceCredentials(
            device_id=device["id"],
            tenant_id=device["tenant_id"],
            refresh_token=str(family_id) + "." + secrets.token_urlsafe(32),
            access_token=self._tokens.issue(
                tenant_id=device["tenant_id"],
                device_id=device["id"],
                family_id=family_id,
                authorization_version=device["authorization_version"],
                connection_generation=device["connection_generation"],
            ),
        )

    def exchange(
        self, pairing_id: ResourceId, body: PairingExchange, key: str | None, ip: str
    ) -> DeviceCredentials:
        key = _key(key)
        self._rate("exchange", ip, pairing_id, 120, 60)
        request = fingerprint([key, _hash(body.poll_secret)])

        def exchange(tx: Transaction) -> DeviceCredentials | ErrorCode:
            path = "device_pairings/" + pairing_id
            pairing = tx.get(path)
            if not pairing or not hmac.compare_digest(
                pairing["poll_hash"], _hash(body.poll_secret)
            ):
                return ErrorCode.RESOURCE_NOT_FOUND
            if (
                pairing["epoch"] != self._tokens.epoch
                or self._now() >= pairing["expires_at"]
                or pairing["status"] == "expired"
            ):
                return ErrorCode.RESOURCE_EXPIRED
            if pairing["status"] == "pending":
                return ErrorCode.PAIRING_PENDING
            device = self._device(tx, pairing["tenant_id"], pairing["device_id"])
            if any(
                pairing.get(field, 1) != device[field]
                for field in ("authorization_version", "connection_generation")
            ):
                return ErrorCode.AUTHENTICATION_REQUIRED
            binding = self._binding(
                "exchange",
                f"{pairing['tenant_id']}/{pairing['device_id']}/{pairing_id}",
                request,
                1,
            )
            if pairing["status"] == "exchanged":
                if pairing["exchange_request"] != request:
                    return ErrorCode.IDEMPOTENCY_CONFLICT
                if self._now() >= pairing["replay_until"]:
                    return ErrorCode.RESOURCE_EXPIRED
                family, _ = self._family(tx, pairing["family_id"])
                if family["generation"] != 1:
                    return ErrorCode.RESOURCE_EXPIRED
                return DeviceCredentials.model_validate_json(
                    self._tokens.open(pairing["receipt"], binding)
                )
            family_id = ResourceId(str(uuid4()))
            credentials = self._credentials(device, family_id)
            digest = _hash(credentials.refresh_token)
            expires = self._now() + FAMILY_LIFETIME
            tx.create(
                "device_credential_families/" + family_id,
                {
                    "tenant_id": device["tenant_id"],
                    "device_id": device["id"],
                    "epoch": self._tokens.epoch,
                    "status": "active",
                    "generation": 1,
                    "current_hash": digest,
                    "authorization_version": device["authorization_version"],
                    "connection_generation": device["connection_generation"],
                    "expires_at": expires,
                },
            )
            tx.create(
                "device_refresh_tokens/" + digest,
                {
                    "family_id": family_id,
                    "generation": 1,
                    "status": "current",
                    "expires_at": expires,
                },
            )
            tx.set(
                path,
                {
                    **pairing,
                    "status": "exchanged",
                    "exchange_request": request,
                    "family_id": family_id,
                    "replay_until": min(pairing["expires_at"], self._now() + REPLAY_GRACE),
                    "receipt": self._tokens.seal(credentials.model_dump_json(), binding),
                },
            )
            return credentials

        return _result(self._store.run(exchange))

    def refresh(self, body: DeviceRefresh, key: str | None, ip: str) -> DeviceCredentials:
        key = _key(key)
        self._rate("refresh", ip, body.device_id, 240, 120)
        digest = _hash(body.refresh_token)
        request = fingerprint([key, body.model_dump(mode="json")])

        def refresh(tx: Transaction) -> DeviceCredentials | ErrorCode:
            path = "device_refresh_tokens/" + digest
            token = tx.get(path)
            if not token:
                return ErrorCode.AUTHENTICATION_REQUIRED
            family_id = token["family_id"]
            family, device = self._family(tx, family_id)
            if body.device_id != family["device_id"]:
                return ErrorCode.AUTHENTICATION_REQUIRED
            request_path = (
                f"device_credential_families/{family_id}/requests/{body.refresh_request_id}"
            )
            previous = tx.get(request_path)
            key_path = f"device_credential_families/{family_id}/idempotency/" + self._tokens.digest(
                "refresh-key", key
            )
            previous_key = tx.get(key_path)
            binding = self._binding(
                "refresh",
                f"{family['tenant_id']}/{family['device_id']}/{family_id}",
                request,
                token["generation"] + 1,
            )
            if token["status"] == "used":
                if token["request_id"] == body.refresh_request_id and token["request"] != request:
                    return ErrorCode.IDEMPOTENCY_CONFLICT
                if (
                    token["request"] == request
                    and self._now() < token["replay_until"]
                    and family["generation"] == token["generation"] + 1
                ):
                    return DeviceCredentials.model_validate_json(
                        self._tokens.open(token["receipt"], binding)
                    )
                tx.set("device_credential_families/" + family_id, {**family, "status": "revoked"})
                return ErrorCode.AUTHENTICATION_REQUIRED
            if previous or previous_key:
                return ErrorCode.IDEMPOTENCY_CONFLICT
            if family["current_hash"] != digest or family["generation"] != token["generation"]:
                return ErrorCode.AUTHENTICATION_REQUIRED
            credentials = self._credentials(device, family_id)
            next_hash = _hash(credentials.refresh_token)
            tx.set(
                path,
                {
                    **token,
                    "status": "used",
                    "request": request,
                    "request_id": body.refresh_request_id,
                    "replay_until": self._now() + REPLAY_GRACE,
                    "receipt": self._tokens.seal(credentials.model_dump_json(), binding),
                },
            )
            tx.create(
                "device_refresh_tokens/" + next_hash,
                {
                    "family_id": family_id,
                    "generation": token["generation"] + 1,
                    "status": "current",
                    "expires_at": family["expires_at"],
                },
            )
            tx.set(
                "device_credential_families/" + family_id,
                {**family, "generation": family["generation"] + 1, "current_hash": next_hash},
            )
            tx.create(request_path, {"request": request, "expires_at": family["expires_at"]})
            tx.create(key_path, {"request": request, "expires_at": family["expires_at"]})
            return credentials

        return _result(self._store.run(refresh))

    def authenticate(self, token: str) -> DeviceIdentity:
        identity = self._tokens.verify(token)

        return self._store.run(lambda tx: self.authorize(tx, identity))

    def authorize(self, tx: Transaction, identity: DeviceIdentity) -> DeviceIdentity:
        """Recheck a verified credential inside the protected operation's transaction."""
        if (
            identity.credential_epoch != self._tokens.epoch
            or not identity.iat <= self._now().timestamp() < identity.exp
        ):
            raise APIError(ErrorCode.AUTHENTICATION_REQUIRED)
        family, _ = self._family(tx, identity.family_id)
        if (
            any(
                family[field] != getattr(identity, field)
                for field in ("tenant_id", "authorization_version", "connection_generation")
            )
            or family["device_id"] != identity.sub
        ):
            raise APIError(ErrorCode.AUTHENTICATION_REQUIRED)
        return identity
