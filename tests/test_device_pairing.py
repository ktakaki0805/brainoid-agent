"""T-003: pairing guessing/expiry, encrypted replay, rotation and authentication fences."""

import base64
import copy
import json
import secrets
import unittest
from datetime import timedelta
from functools import partial
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import jwt
import yaml  # type: ignore[import-untyped]
from brainoid_api.app import create_app
from brainoid_api.auth.context import AuthenticatedIdentity
from brainoid_api.auth.device_tokens import DeviceTokens
from brainoid_api.auth.runtime import AuthServices, authenticated_identity, get_auth_services
from brainoid_api.common.pagination import Cursors
from brainoid_api.errors import APIError, ErrorCode
from brainoid_api.repositories.device_credentials import (
    DeviceCredentialRepository,
    DeviceRefresh,
    Pairing,
    PairingConfirm,
    PairingCreate,
    PairingExchange,
)
from brainoid_api.repositories.tenants import Tenants
from brainoid_api.settings import ConfigurationError, load_settings
from brainoid_contracts import FirebaseUid, ResourceId
from brainoid_policy.control_ledger import Change, ControlLedger, DeviceTarget
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fakes.authz_ledger import MemoryObjects
from fakes.runtime import FakeClock
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from starlette.testclient import TestClient
from test_api_foundation import CLOUD_ENV, EMULATOR_ENV, LOCAL_ENV
from test_common_mutations import MemoryStore

KEY = "pairing-test-request-key"
EPOCH = ResourceId("00000000-0000-0000-0000-000000000001")
KEYRING = Path("tests/fixtures/device_tokens/local-keyring.json").read_text()


class DevicePairingFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.store = MemoryStore()
        fixture = json.loads(Path("tests/fixtures/tenants/demo.json").read_text())
        self.store.data = {doc["path"]: doc["fields"] for doc in fixture["documents"]}
        self.tenant, self.other = [
            ResourceId(p.split("/")[1])
            for p in self.store.data
            if p.startswith("tenants/") and p.count("/") == 1
        ]
        self.ledger = ControlLedger(MemoryObjects())
        self.tenants = Tenants(
            self.store,
            Cursors(b"t" * 32, now=self.clock.now),
            ledger=self.ledger,
            now=self.clock.now,
        )
        self.tokens = self.codec()
        self.repo = self.repository()
        self.uid = "demo-a-owner"
        app = create_app(settings_loader=lambda: load_settings(LOCAL_ENV))
        app.dependency_overrides[authenticated_identity] = lambda: self.identity()
        app.dependency_overrides[get_auth_services] = lambda: AuthServices(
            tenants=self.tenants, devices=self.repo
        )
        self.client = self.enterContext(TestClient(app))

    def codec(self, keyring: str = KEYRING, epoch: ResourceId = EPOCH) -> DeviceTokens:
        return DeviceTokens(
            keyring,
            project="demo-brainoid",
            credential_epoch=epoch,
            test_only=True,
            now=self.clock.now,
        )

    def repository(self) -> DeviceCredentialRepository:
        return DeviceCredentialRepository(
            self.store, self.tenants, self.ledger, self.tokens, now=self.clock.now
        )

    def identity(self) -> AuthenticatedIdentity:
        now = self.clock.now()
        return AuthenticatedIdentity(
            user_id=FirebaseUid(self.uid),
            issued_at=now,
            authenticated_at=now,
            expires_at=now + timedelta(hours=1),
        )

    def create(self, nonce: str | None = None, key: str = KEY, ip: str = "127.0.0.1") -> Pairing:
        return self.repo.create(
            PairingCreate(device_name="Test Pi", installation_nonce=nonce or secrets.token_hex(32)),
            key,
            ip,
        )

    def confirm(self, pairing: Pairing, code: str | None = None, key: str = KEY) -> Any:
        return self.repo.confirm(
            self.identity(),
            self.tenant,
            PairingConfirm(
                user_code=code or pairing.user_code, device_name="Pi", allowed_entity_ids=[]
            ),
            key,
            "127.0.0.1",
        )

    def exchange(self, pairing: Pairing, key: str = KEY) -> Any:
        return self.repo.exchange(
            pairing.pairing_id, PairingExchange(poll_secret=pairing.poll_secret), key, "127.0.0.1"
        )

    def refresh_body(self, credentials: Any, request_id: ResourceId | None = None) -> DeviceRefresh:
        return DeviceRefresh(
            device_id=credentials.device_id,
            refresh_token=credentials.refresh_token,
            refresh_request_id=request_id or ResourceId(str(uuid4())),
        )

    def refresh(self, body: DeviceRefresh, key: str = KEY) -> Any:
        return self.repo.refresh(body, key, "127.0.0.1")

    def credentials(self) -> Any:
        pairing = self.create()
        self.confirm(pairing)
        return self.exchange(pairing)

    def fails(self, fn: Any, code: ErrorCode) -> None:
        with self.assertRaises(APIError) as error:
            fn()
        self.assertEqual(error.exception.code, code)


class DevicePairingTests(DevicePairingFixture):
    def test_http_contract_and_no_plaintext_secrets_or_logs(self) -> None:
        headers = {"Idempotency-Key": KEY}
        with self.assertLogs("brainoid_api", level="INFO") as logs:
            start = self.client.post(
                "/v1/device-pairings",
                json={"device_name": "Pi", "installation_nonce": "n" * 32},
                headers=headers,
            )
            self.assertEqual(start.status_code, 201, start.text)
            pairing = start.json()
            confirmation = self.client.post(
                f"/v1/tenants/{self.tenant}/device-pairings:confirm",
                json={
                    "user_code": pairing["user_code"],
                    "device_name": "Pi",
                    "allowed_entity_ids": [],
                },
                headers=headers,
            )
            self.assertEqual(confirmation.status_code, 201, confirmation.text)
            exchange = self.client.post(
                f"/v1/device-pairings/{pairing['pairing_id']}:exchange",
                json={"poll_secret": pairing["poll_secret"]},
                headers=headers,
            )
            self.assertEqual(exchange.status_code, 200, exchange.text)
            credentials = exchange.json()
            refreshed = self.client.post(
                "/v1/device-tokens:refresh",
                json={
                    "device_id": credentials["device_id"],
                    "refresh_token": credentials["refresh_token"],
                    "refresh_request_id": str(uuid4()),
                },
                headers=headers,
            )
            self.assertEqual(refreshed.status_code, 200, refreshed.text)
        spec = yaml.safe_load(Path("specs/openapi.yaml").read_text())
        for schema, response in (
            ("Pairing", start),
            ("Device", confirmation),
            ("DeviceCredentials", exchange),
            ("DeviceCredentials", refreshed),
        ):
            Draft202012Validator(
                {"$ref": "#/components/schemas/" + schema, "components": spec["components"]}
            ).validate(response.json())
            self.assertEqual(response.headers["cache-control"], "no-store")
        persisted = json.dumps(self.store.data, default=str) + str(logs.output) + confirmation.text
        for secret in (
            pairing["user_code"],
            pairing["poll_secret"],
            credentials["access_token"],
            credentials["refresh_token"],
            refreshed.json()["refresh_token"],
        ):
            self.assertNotIn(secret, persisted)
        self.assertFalse(any("idempotency_records" in path for path in self.store.data))

    def test_pending_permissions_other_poll_secret_and_five_bad_guesses(self) -> None:
        first, other = self.create(), self.create()
        self.fails(partial(self.exchange, first), ErrorCode.PAIRING_PENDING)
        self.fails(
            lambda: self.repo.exchange(
                first.pairing_id, PairingExchange(poll_secret=other.poll_secret), KEY, "127.0.0.1"
            ),
            ErrorCode.RESOURCE_NOT_FOUND,
        )
        for uid, code in (
            ("demo-a-member", ErrorCode.OPERATION_FORBIDDEN),
            ("demo-b-owner", ErrorCode.RESOURCE_NOT_FOUND),
        ):
            self.uid = uid
            self.fails(partial(self.confirm, first), code)
        self.uid = "demo-a-admin"
        suffix = "AAAA" if first.user_code[4:] != "AAAA" else "BBBB"
        for _ in range(5):
            self.fails(
                partial(self.confirm, first, first.user_code[:4] + suffix),
                ErrorCode.RESOURCE_NOT_FOUND,
            )
        self.fails(partial(self.confirm, first), ErrorCode.RESOURCE_EXPIRED)
        self.confirm(other)
        self.assertEqual(self.exchange(other).tenant_id, self.tenant)

    def test_expiry_boundary_and_one_exchange(self) -> None:
        pairing = self.create()
        self.confirm(pairing)
        first = self.exchange(pairing)
        self.assertEqual(first, self.exchange(pairing))
        self.fails(partial(self.exchange, pairing, KEY + "other"), ErrorCode.IDEMPOTENCY_CONFLICT)
        self.clock.advance(60)
        self.fails(partial(self.exchange, pairing), ErrorCode.RESOURCE_EXPIRED)
        self.clock.advance(240)
        self.fails(partial(self.confirm, pairing), ErrorCode.RESOURCE_EXPIRED)
        self.fails(partial(self.exchange, pairing), ErrorCode.RESOURCE_EXPIRED)

    def test_creation_replay_collision_and_installation_binding(self) -> None:
        nonce = "n" * 32
        with patch(
            "brainoid_api.repositories.device_credentials.secrets.choice",
            side_effect=list("ABCD1234ABCD9999ABCD5678EFGH1234"),
        ):
            first = self.create(nonce)
            self.assertEqual(first, self.create(nonce))
            other = self.create()
        self.assertNotEqual(first.user_code[:4], other.user_code[:4])
        self.confirm(first)
        next_pair = self.create(nonce, KEY + "again")
        self.fails(partial(self.confirm, next_pair), ErrorCode.CONFLICT)
        self.tenant, self.uid = self.other, "demo-b-owner"
        self.fails(partial(self.confirm, next_pair), ErrorCode.CONFLICT)

    def test_installation_ip_and_unknown_selector_limits(self) -> None:
        for index in range(5):
            self.create("n" * 32, KEY + str(index), str(index))
        self.fails(
            lambda: self.create("n" * 32, KEY + "blocked", "different"), ErrorCode.RATE_LIMITED
        )
        for index in range(20):
            self.create(key=KEY + str(index), ip="same-ip")
        self.fails(lambda: self.create(ip="same-ip"), ErrorCode.RATE_LIMITED)
        unknown = Pairing(
            pairing_id=ResourceId(str(uuid4())),
            user_code="ZZZZAAAA",
            poll_secret="s" * 32,
            expires_at=self.clock.now() + timedelta(minutes=5),
        )
        for _ in range(30):
            self.fails(partial(self.confirm, unknown), ErrorCode.RESOURCE_NOT_FOUND)
        self.fails(partial(self.confirm, unknown), ErrorCode.RATE_LIMITED)
        self.clock.advance(300)
        self.fails(partial(self.confirm, unknown), ErrorCode.RESOURCE_NOT_FOUND)

    def test_rotation_exact_retry_lost_response_and_reuse_revokes_family(self) -> None:
        credentials = self.credentials()
        body = self.refresh_body(credentials)
        result = self.refresh(body)
        # New repository/key objects represent process restart after losing the response.
        self.tokens = self.codec()
        self.repo = self.repository()
        self.assertEqual(result, self.refresh(body))
        self.fails(partial(self.refresh, body, KEY + "different"), ErrorCode.IDEMPOTENCY_CONFLICT)
        self.repo.authenticate(result.access_token)
        self.fails(
            partial(self.refresh, self.refresh_body(credentials)), ErrorCode.AUTHENTICATION_REQUIRED
        )
        self.fails(
            partial(self.refresh, self.refresh_body(result)), ErrorCode.AUTHENTICATION_REQUIRED
        )
        self.fails(
            partial(self.repo.authenticate, result.access_token), ErrorCode.AUTHENTICATION_REQUIRED
        )

    def test_refresh_grace_boundary_request_id_rebinding_and_wrong_device(self) -> None:
        old = self.credentials()
        body = self.refresh_body(old)
        new = self.refresh(body)
        self.fails(
            partial(self.refresh, self.refresh_body(new, body.refresh_request_id)),
            ErrorCode.IDEMPOTENCY_CONFLICT,
        )
        wrong = body.model_copy(update={"device_id": ResourceId(str(uuid4()))})
        self.fails(partial(self.refresh, wrong), ErrorCode.AUTHENTICATION_REQUIRED)
        self.clock.advance(60)
        self.fails(partial(self.refresh, body), ErrorCode.AUTHENTICATION_REQUIRED)
        self.fails(partial(self.refresh, self.refresh_body(new)), ErrorCode.AUTHENTICATION_REQUIRED)

    def test_token_signature_algorithm_audience_type_expiry_and_device_binding(self) -> None:
        credentials = self.credentials()
        claims = jwt.decode(credentials.access_token, options={"verify_signature": False})
        identity = self.repo.authenticate(credentials.access_token)
        self.assertEqual(identity.sub, credentials.device_id)
        self.assertEqual(identity.tenant_id, credentials.tenant_id)
        self.assertEqual(identity.exp - identity.iat, 900)
        for patch_claims in (
            {"aud": "firebase"},
            {"iss": "firebase"},
            {"token_use": "id"},
            {"sub": str(uuid4())},
            {"tenant_id": str(self.other)},
            {"authorization_version": 2},
            {"connection_generation": 2},
            {"iat": True},
        ):
            token = jwt.encode(
                {**claims, **patch_claims},
                self.tokens._private,
                algorithm="RS256",
                headers={"kid": self.tokens._kid, "typ": "device+jwt"},
            )
            with self.subTest(claims=patch_claims):
                self.fails(
                    partial(self.repo.authenticate, token), ErrorCode.AUTHENTICATION_REQUIRED
                )
        for token in (
            credentials.access_token[:-12] + "A" * 12,
            jwt.encode(
                claims,
                "x" * 32,
                algorithm="HS256",
                headers={"kid": self.tokens._kid, "typ": "device+jwt"},
            ),
            jwt.encode(
                claims,
                self.tokens._private,
                algorithm="RS256",
                headers={"kid": "unknown", "typ": "device+jwt"},
            ),
            jwt.encode(
                claims,
                self.tokens._private,
                algorithm="RS256",
                headers={"kid": self.tokens._kid, "typ": "JWT"},
            ),
        ):
            self.fails(partial(self.repo.authenticate, token), ErrorCode.AUTHENTICATION_REQUIRED)
        self.clock.advance(900)
        self.fails(
            partial(self.repo.authenticate, credentials.access_token),
            ErrorCode.AUTHENTICATION_REQUIRED,
        )

    def test_latest_ledger_and_credential_epoch_fence_all_paths(self) -> None:
        pairing = self.create()
        self.confirm(pairing)
        credentials = self.exchange(pairing)
        body = self.refresh_body(credentials)
        refreshed = self.refresh(body)
        self.tokens = self.codec(epoch=ResourceId(str(uuid4())))
        self.repo = self.repository()
        for call in (
            partial(self.exchange, pairing),
            partial(self.refresh, body),
            partial(self.refresh, self.refresh_body(refreshed)),
            partial(self.repo.authenticate, credentials.access_token),
        ):
            with self.assertRaises(APIError):
                call()
        self.tokens = self.codec()
        self.repo = self.repository()
        device = self.store.data[f"tenants/{self.tenant}/devices/{credentials.device_id}"]
        change = Change(
            operation_id=ResourceId(str(uuid4())),
            tenant_id=self.tenant,
            expected_version=1,
            authorization_version=2,
            effect="deny",
            target=DeviceTarget(
                device_id=credentials.device_id,
                status="revoked",
                allowed_scopes=tuple(device["allowed_scopes"]),
                allowed_entity_ids=(),
                allowed_room_ids=(),
            ),
        )
        self.ledger.prepare(change)
        for call in (
            partial(self.refresh, self.refresh_body(refreshed)),
            partial(self.repo.authenticate, refreshed.access_token),
        ):
            with self.assertRaises(APIError):
                call()

    def test_encrypted_receipt_tampering_and_key_rotation(self) -> None:
        pairing = self.create()
        self.confirm(pairing)
        original = self.exchange(pairing)
        saved = self.store.data["device_pairings/" + pairing.pairing_id]
        receipt = copy.deepcopy(saved["receipt"])
        saved["receipt"]["data"] = receipt["data"][:-8] + "AAAAAAAA"
        self.fails(partial(self.exchange, pairing), ErrorCode.TEMPORARILY_UNAVAILABLE)
        self.store.data["device_pairings/" + pairing.pairing_id]["receipt"] = receipt
        ring = json.loads(KEYRING)
        ring["receipt_keys"]["new-receipt"] = base64.b64encode(secrets.token_bytes(32)).decode()
        ring["active_receipt_kid"] = "new-receipt"
        new_private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        ring["verification_keys"]["new-signing"] = (
            new_private.public_key()
            .public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
            .decode()
        )
        ring["signing_private_key"] = new_private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
        old_kid = ring["active_signing_kid"]
        ring["active_signing_kid"] = "new-signing"
        self.tokens = self.codec(json.dumps(ring))
        self.repo = self.repository()
        self.assertEqual(original, self.exchange(pairing))
        self.repo.authenticate(original.access_token)
        rotated = self.refresh(self.refresh_body(original))
        self.assertEqual(jwt.get_unverified_header(rotated.access_token)["kid"], "new-signing")
        del ring["verification_keys"][old_kid]
        self.tokens = self.codec(json.dumps(ring))
        self.repo = self.repository()
        self.fails(
            partial(self.repo.authenticate, original.access_token),
            ErrorCode.AUTHENTICATION_REQUIRED,
        )
        self.repo.authenticate(rotated.access_token)
        # AAD binds the operation/identity/request/generation; ciphertext cannot move.
        self.fails(
            lambda: self.tokens.open(receipt, "wrong-binding"), ErrorCode.TEMPORARILY_UNAVAILABLE
        )

    def test_explicit_provider_keys_and_epoch_configuration(self) -> None:
        for name in (
            "DEVICE_TOKEN_PROVIDER",
            "DEVICE_TOKEN_KEYRING_FILE",
            "DEVICE_CREDENTIAL_EPOCH",
        ):
            with self.assertRaises(ConfigurationError):
                load_settings({k: v for k, v in EMULATOR_ENV.items() if k != name})
        with self.assertRaises(ConfigurationError):
            load_settings({**CLOUD_ENV, "DEVICE_TOKEN_PROVIDER": "local-test"})
        with self.assertRaises(ValueError):
            DeviceTokens(KEYRING, project="cloud", credential_epoch=EPOCH, test_only=False)

    def test_absolute_family_expiry_and_refresh_key_is_not_reusable(self) -> None:
        original = self.credentials()
        first = self.refresh_body(original)
        rotated = self.refresh(first)
        second = self.refresh_body(rotated)
        self.fails(partial(self.refresh, second), ErrorCode.IDEMPOTENCY_CONFLICT)
        latest = self.refresh(second, KEY + "next")
        self.clock.advance(30 * 24 * 60 * 60 - 1)
        near_expiry = self.refresh(self.refresh_body(latest), KEY + "near-expiry")
        self.repo.authenticate(near_expiry.access_token)
        self.clock.advance(1)
        self.fails(
            partial(self.refresh, self.refresh_body(near_expiry), KEY + "expired"),
            ErrorCode.AUTHENTICATION_REQUIRED,
        )
        self.fails(
            partial(self.repo.authenticate, near_expiry.access_token),
            ErrorCode.AUTHENTICATION_REQUIRED,
        )

    def test_forwarded_header_does_not_bypass_ip_limit(self) -> None:
        for index in range(21):
            response = self.client.post(
                "/v1/device-pairings",
                json={"device_name": "Pi", "installation_nonce": "i" * 32 + str(index)},
                headers={
                    "Idempotency-Key": KEY + str(index),
                    "X-Forwarded-For": str(index),
                    "Forwarded": "for=" + str(index),
                },
            )
            self.assertEqual(response.status_code, 201 if index < 20 else 429)

    def test_unknown_selector_limit_across_authenticated_actors(self) -> None:
        body = PairingConfirm(user_code="ZZZZAAAA", device_name="Pi", allowed_entity_ids=[])
        for index in range(61):
            self.uid = "unregistered-test-" + str(index)
            self.fails(
                lambda: self.repo.confirm(
                    self.identity(), self.tenant, body, KEY, "same-confirm-ip"
                ),
                ErrorCode.RESOURCE_NOT_FOUND if index < 60 else ErrorCode.RATE_LIMITED,
            )
