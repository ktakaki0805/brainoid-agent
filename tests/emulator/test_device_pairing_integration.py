"""T-003: official Firestore transactions for concurrent rotation and response loss."""

import os
import tempfile
import unittest
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from threading import Barrier
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import firebase_admin  # type: ignore[import-untyped]
from brainoid_api.adapters.local_authz_ledger import LocalAuthzObjects
from brainoid_api.app import create_app
from brainoid_api.auth.context import AuthenticatedIdentity
from brainoid_api.auth.device_tokens import DeviceTokens
from brainoid_api.common.firestore import FirestoreTransactions
from brainoid_api.common.pagination import Cursors
from brainoid_api.errors import APIError, ErrorCode
from brainoid_api.repositories.device_credentials import (
    DeviceCredentialRepository,
    DeviceCredentials,
    DeviceRefresh,
    Pairing,
    PairingConfirm,
    PairingCreate,
    PairingExchange,
)
from brainoid_api.repositories.tenants import Tenants
from brainoid_contracts import FirebaseUid, ResourceId
from brainoid_policy.control_ledger import ControlLedger
from firebase_admin import auth
from google.api_core.exceptions import ServiceUnavailable
from google.auth.credentials import AnonymousCredentials
from google.cloud.firestore_v1 import Client
from starlette.testclient import TestClient

from scripts.demo_emulator import DATABASE, PROJECT, DemoEmulator, load_fixture

KEY = "emulator-device-request-key"


class DevicePairingIntegrationFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.emulator = DemoEmulator(os.environ)
        self.emulator.reset()
        self.addCleanup(self.emulator.reset)
        self.emulator.seed()
        self.db = Client(project=PROJECT, database=DATABASE, credentials=AnonymousCredentials())  # type: ignore[no-untyped-call]
        self.addCleanup(self.db.close)
        self.now = datetime.now(UTC)
        self.store = FirestoreTransactions(self.db)
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.ledger = ControlLedger(LocalAuthzObjects(self.directory))
        self.tenants = Tenants(
            self.store,
            Cursors(b"t" * 32, now=lambda: self.now),
            ledger=self.ledger,
            now=lambda: self.now,
        )
        self.tokens = DeviceTokens(
            Path("tests/fixtures/device_tokens/local-keyring.json").read_text(),
            project=PROJECT,
            credential_epoch=ResourceId("00000000-0000-0000-0000-000000000001"),
            test_only=True,
            now=lambda: self.now,
        )
        self.repo = DeviceCredentialRepository(
            self.store, self.tenants, self.ledger, self.tokens, now=lambda: self.now
        )
        self.tenant = ResourceId(
            next(
                d["fields"]["tenant_id"]
                for d in load_fixture()["documents"]
                if d["path"].startswith("tenants/") and d["path"].count("/") == 1
            )
        )

    def identity(self) -> AuthenticatedIdentity:
        return AuthenticatedIdentity(
            user_id=FirebaseUid("demo-a-owner"),
            issued_at=self.now,
            authenticated_at=self.now,
            expires_at=self.now + timedelta(hours=1),
        )

    def pairing(self) -> Pairing:
        return self.repo.create(
            PairingCreate(device_name="Pi", installation_nonce=str(uuid4())), KEY, "127.0.0.1"
        )

    def confirm(self, pairing: Pairing, code: str | None = None) -> Any:
        return self.repo.confirm(
            self.identity(),
            self.tenant,
            PairingConfirm(
                user_code=code or pairing.user_code, device_name="Pi", allowed_entity_ids=[]
            ),
            KEY,
            "127.0.0.1",
        )

    def exchange(self, pairing: Pairing) -> DeviceCredentials:
        return self.repo.exchange(
            pairing.pairing_id, PairingExchange(poll_secret=pairing.poll_secret), KEY, "127.0.0.1"
        )

    def refresh(self, body: DeviceRefresh) -> DeviceCredentials:
        return self.repo.refresh(body, KEY, "127.0.0.1")

    def body(self, credentials: DeviceCredentials) -> DeviceRefresh:
        return DeviceRefresh(
            device_id=credentials.device_id,
            refresh_token=credentials.refresh_token,
            refresh_request_id=ResourceId(str(uuid4())),
        )

    def fails(self, call: Callable[[], object], code: ErrorCode) -> None:
        with self.assertRaises(APIError) as error:
            call()
        self.assertEqual(error.exception.code, code)

    def concurrent(self, *calls: Callable[[], object]) -> list[object]:
        barrier = Barrier(len(calls))

        def run(call: Callable[[], object]) -> object:
            barrier.wait(timeout=10)
            try:
                return call()
            except APIError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=len(calls)) as pool:
            futures = [pool.submit(run, call) for call in calls]
            return [future.result(timeout=45) for future in futures]


class DevicePairingIntegrationTests(DevicePairingIntegrationFixture):
    def test_double_confirmation_exchange_and_identical_refresh_issue_once(self) -> None:
        pairing = self.pairing()
        confirmations = self.concurrent(
            partial(self.confirm, pairing), partial(self.confirm, pairing)
        )
        self.assertEqual(confirmations[0], confirmations[1])
        results = self.concurrent(partial(self.exchange, pairing), partial(self.exchange, pairing))
        self.assertEqual(results[0], results[1])
        self.assertIsInstance(results[0], DeviceCredentials)
        credentials = self.exchange(pairing)
        body = self.body(credentials)
        refreshed = self.concurrent(partial(self.refresh, body), partial(self.refresh, body))
        self.assertEqual(refreshed[0], refreshed[1])
        self.assertIsInstance(refreshed[0], DeviceCredentials)
        self.assertEqual(len(list(self.db.collection("device_credential_families").stream())), 1)
        self.assertEqual(len(list(self.db.collection("device_refresh_tokens").stream())), 2)

    def test_different_concurrent_rotation_requests_revoke_family(self) -> None:
        pairing = self.pairing()
        self.confirm(pairing)
        credentials = self.exchange(pairing)
        results = self.concurrent(
            partial(self.refresh, self.body(credentials)),
            partial(self.refresh, self.body(credentials)),
        )
        self.assertEqual(sum(isinstance(result, DeviceCredentials) for result in results), 1)
        self.assertIn(ErrorCode.AUTHENTICATION_REQUIRED, results)
        self.fails(
            partial(self.repo.authenticate, credentials.access_token),
            ErrorCode.AUTHENTICATION_REQUIRED,
        )
        family = next(self.db.collection("device_credential_families").stream()).to_dict()
        assert family is not None
        self.assertEqual(family["status"], "revoked")

    def test_exchange_and_rotation_commit_response_loss_recovers_exact_receipt(self) -> None:
        pairing = self.pairing()
        self.confirm(pairing)
        commit = self.db._firestore_api.commit

        def lose_second(call: Callable[[], object]) -> None:
            count = 0

            def commit_then_lose(*args: Any, **kwargs: Any) -> Any:
                nonlocal count
                result = commit(*args, **kwargs)
                count += 1
                if count == 2:  # rate admission commits first; lose the business commit response.
                    raise ServiceUnavailable("synthetic response loss")  # type: ignore[no-untyped-call]
                return result

            with patch.object(self.db._firestore_api, "commit", side_effect=commit_then_lose):
                self.fails(call, ErrorCode.TEMPORARILY_UNAVAILABLE)

        lose_second(partial(self.exchange, pairing))
        credentials = self.exchange(pairing)
        self.assertEqual(credentials, self.exchange(pairing))
        body = self.body(credentials)
        lose_second(partial(self.refresh, body))
        refreshed = self.refresh(body)
        self.assertEqual(refreshed, self.refresh(body))
        self.assertEqual(len(list(self.db.collection("device_refresh_tokens").stream())), 2)

    def test_five_concurrent_guesses_and_expiry_commit_without_exposing_secrets(self) -> None:
        pairing = self.pairing()
        wrong = pairing.user_code[:4] + ("AAAA" if pairing.user_code[4:] != "AAAA" else "BBBB")
        results = self.concurrent(*(partial(self.confirm, pairing, wrong) for _ in range(5)))
        # Concurrent rate admission can exhaust the SDK's bounded transaction retries.
        # Retry only unfinished 503 requests, with the same body/key, at most three
        # additional times each. A 503 never counts as a completed bad-code refusal.
        for index, result in enumerate(results):
            for _ in range(3):
                if result != ErrorCode.TEMPORARILY_UNAVAILABLE:
                    break
                try:
                    result = self.confirm(pairing, wrong)
                except APIError as error:
                    result = error.code
            results[index] = result
        self.assertTrue(all(result == ErrorCode.RESOURCE_NOT_FOUND for result in results), results)
        self.fails(partial(self.confirm, pairing), ErrorCode.RESOURCE_EXPIRED)
        saved = self.db.document("device_pairings/" + pairing.pairing_id).get().to_dict()
        assert saved is not None
        self.assertEqual(saved["failures"], 5)
        self.assertEqual(saved["status"], "expired")
        other = self.pairing()
        self.now += timedelta(seconds=300)
        self.fails(partial(self.confirm, other), ErrorCode.RESOURCE_EXPIRED)

    def test_runtime_firebase_and_device_tokens_cannot_substitute_for_each_other(self) -> None:
        firebase = firebase_admin.initialize_app(
            options={"projectId": PROJECT}, name="pairing-" + str(uuid4())
        )
        self.addCleanup(firebase_admin.delete_app, firebase)
        custom = auth.create_custom_token("demo-a-owner", app=firebase).decode("ascii")
        result = self.emulator.request(
            "auth",
            "POST",
            "/identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken?key=fixture-key",
            {"token": custom, "returnSecureToken": True},
        )
        bearer = result["idToken"]
        headers = {"Authorization": "Bearer " + bearer, "Idempotency-Key": KEY}
        with (
            patch.dict(os.environ, {"AUTHZ_LEDGER_DIRECTORY": self.directory}),
            TestClient(create_app()) as http,
        ):
            response = http.post(
                "/v1/device-pairings",
                json={"device_name": "Pi", "installation_nonce": str(uuid4())},
                headers=headers,
            )
            self.assertEqual(response.status_code, 201, response.text)
            pairing = response.json()
            confirm_body = {
                "user_code": pairing["user_code"],
                "device_name": "Pi",
                "allowed_entity_ids": [],
            }
            response = http.post(
                f"/v1/tenants/{self.tenant}/device-pairings:confirm",
                json=confirm_body,
                headers=headers,
            )
            self.assertEqual(response.status_code, 201, response.text)
            response = http.post(
                f"/v1/device-pairings/{pairing['pairing_id']}:exchange",
                json={"poll_secret": pairing["poll_secret"]},
                headers=headers,
            )
            self.assertEqual(response.status_code, 200, response.text)
            token = response.json()["access_token"]
            self.assertEqual(
                http.get(
                    f"/v1/tenants/{self.tenant}", headers={"Authorization": "Bearer " + token}
                ).status_code,
                401,
            )
            self.assertEqual(
                http.post(
                    f"/v1/tenants/{self.tenant}/device-pairings:confirm",
                    json=confirm_body,
                    headers={**headers, "Authorization": "Bearer " + token},
                ).status_code,
                401,
            )
            self.fails(partial(self.repo.authenticate, bearer), ErrorCode.AUTHENTICATION_REQUIRED)
