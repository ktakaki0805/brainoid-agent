"""Shared OAuth HTTP and concurrency scenarios for strict fake and real Firestore."""

import json
import tempfile
import unittest
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Event
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from brainoid_api.adapters.calendar_secrets import LocalCalendarSecrets
from brainoid_api.adapters.google_oauth import GoogleOAuth
from brainoid_api.app import create_app
from brainoid_api.auth.context import AuthenticatedIdentity
from brainoid_api.auth.runtime import AuthServices, authenticated_identity, get_auth_services
from brainoid_api.common.pagination import Cursors
from brainoid_api.errors import APIError
from brainoid_api.repositories.tenants import Tenants
from brainoid_api.services.calendar_authorization import CalendarAuthorization, path
from brainoid_api.settings import load_settings
from brainoid_contracts import FirebaseUid, ResourceId
from brainoid_contracts.transactions import Document, Transaction
from brainoid_memory.calendar_oauth import READ, WRITE, OAuthStart, SecretUnavailable
from brainoid_policy.control_ledger import ControlLedger
from pydantic import SecretStr
from starlette.testclient import TestClient
from test_api_foundation import LOCAL_ENV

from fakes.authz_ledger import MemoryObjects
from fakes.runtime import FakeClock


class OAuthTransport:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []
        self.scopes = READ
        self.after: Callable[[], None] = lambda: None
        self.fail = False

    def post(self, url: str, body: dict[str, str]) -> dict[str, object]:
        self.calls.append(dict(body))
        self.after()
        if self.fail:
            raise RuntimeError("SYNTHETIC_ACCESS SYNTHETIC_REFRESH SYNTHETIC_CODE")
        return {
            "access_token": "SYNTHETIC_ACCESS",
            "refresh_token": "SYNTHETIC_REFRESH",
            "scope": self.scopes,
            "token_type": "Bearer",
        }


class CalendarScenarios(unittest.TestCase):
    store: Any

    def setup_calendar(self) -> None:
        self.clock = FakeClock()
        fixture = json.loads(Path("tests/fixtures/tenants/demo.json").read_text())
        for document in fixture["documents"]:
            self.put(document["path"], document["fields"])
        self.tenant = ResourceId(fixture["documents"][0]["fields"]["tenant_id"])
        self.uid = FirebaseUid("demo-a-member")
        self.objects = MemoryObjects()
        self.ledger = ControlLedger(self.objects)
        self.tenants = Tenants(
            self.store,
            Cursors(b"k" * 32, now=self.clock.now),
            now=self.clock.now,
            ledger=self.ledger,
        )
        self.transport = OAuthTransport()
        self.provider = GoogleOAuth(
            client_id="synthetic-client",
            client_secret=SecretStr("synthetic-client-secret"),
            redirect_uri="https://api.example.test/v1/oauth/google-calendar/callback",
            post=self.transport.post,
        )
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.secrets = LocalCalendarSecrets(Path(directory), b"s" * 32, now=self.clock.now)
        self.service = CalendarAuthorization(
            self.tenants,
            self.provider,
            self.secrets,
            returns={
                "mobile": "https://app.example.test/oauth-return",
                "web": "https://web.example.test/oauth-return",
            },
        )

    def client(self) -> TestClient:
        app = create_app(settings_loader=lambda: load_settings(LOCAL_ENV))
        app.dependency_overrides[authenticated_identity] = lambda: self.identity()
        app.dependency_overrides[get_auth_services] = lambda: AuthServices(
            tenants=self.tenants, calendar=self.service
        )
        return TestClient(app)

    def identity(self, uid: FirebaseUid | None = None) -> AuthenticatedIdentity:
        now = self.clock.now()
        return AuthenticatedIdentity(
            user_id=uid or self.uid,
            authenticated_at=now,
            issued_at=now,
            expires_at=now + timedelta(hours=1),
        )

    def put(self, location: str, document: Document) -> None:
        self.store.run(lambda tx: tx.set(location, document))

    def get(self, location: str) -> Document:
        result = self.store.run(lambda tx: tx.get(location))
        assert result is not None
        return result  # type: ignore[no-any-return]

    def start(
        self, key: str = "synthetic-oauth-key-001", profile: str = "read_write"
    ) -> tuple[str, ResourceId]:
        response = self.service.start(
            self.identity(),
            self.tenant,
            OAuthStart.model_validate({"scope_profile": profile, "return_target": "mobile"}),
            key,
        )
        state = parse_qs(urlsplit(response.authorization_url).query)["state"][0]
        return state, ResourceId(state.split(".")[1])

    def records(self) -> list[Document]:
        return [
            doc
            for _, doc in self.store.run(
                lambda tx: tx.collection(f"tenants/{self.tenant}/integrations")
            )
        ]

    def revoke_member(self) -> None:
        def change(tx: Transaction) -> None:
            location, saved = tx.query(f"tenants/{self.tenant}/memberships", {"user_id": self.uid})[
                0
            ]
            saved.update(status="revoked", authorization_version=2)
            tx.set(location, saved)

        self.store.run(change)

    def test_success_idempotent_start_actual_scope_pkce_and_no_plaintext_db(self) -> None:
        state, auth = self.start()
        self.assertEqual(self.start(), (state, auth))
        with self.assertRaises(APIError):
            self.start(profile="read")
        saved = self.get(path(self.tenant, auth))
        transient = json.loads(self.secrets.get(saved["transient_ref"]))
        self.assertEqual(
            self.service.callback(state, "SYNTHETIC_CODE", None), self.service.returns["mobile"]
        )
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.transport.calls[0]["code_verifier"], transient["verifier"])
        record = self.records()[0]
        self.assertEqual(record["granted_scopes"], [READ])
        self.assertNotIn(WRITE, record["granted_scopes"])
        self.assertEqual(len(self.service.integrations(self.identity(), self.tenant)), 1)
        self.assertEqual(
            self.service.integrations(self.identity(FirebaseUid("demo-a-owner")), self.tenant), []
        )
        with self.assertRaises(SecretUnavailable):
            self.secrets.get(saved["transient_ref"])
        text = json.dumps(
            self.store.run(
                lambda tx: tx.collection(f"tenants/{self.tenant}/calendar_authorizations")
            )
            + self.store.run(lambda tx: tx.collection(f"tenants/{self.tenant}/integrations")),
            default=str,
        )
        for value in (
            state,
            transient["verifier"],
            transient["nonce"],
            "SYNTHETIC_ACCESS",
            "SYNTHETIC_REFRESH",
            "SYNTHETIC_CODE",
        ):
            self.assertNotIn(value, text)
        with self.assertRaises(APIError):
            self.service.callback(state, "SYNTHETIC_CODE", None)
        self.assertEqual(len(self.transport.calls), 1)

    def test_state_substitution_expiry_and_another_owner_cannot_claim(self) -> None:
        state, auth = self.start()
        with self.assertRaises(APIError):
            self.service.callback(state[:-1] + ("x" if state[-1] != "x" else "y"), "code", None)
        with self.assertRaises(APIError):
            self.store.run(
                lambda tx: self.service.current(
                    tx, self.tenant, auth, origin=self.identity(FirebaseUid("demo-a-owner"))
                )
            )
        self.clock.advance(600)
        with self.assertRaises(APIError):
            self.service.callback(state, "code", None)
        self.service.recover(self.tenant, auth)
        self.assertFalse(self.get(path(self.tenant, auth))["cleanup_pending"])
        self.assertEqual(self.transport.calls, [])

    def test_cancel_bad_callback_and_ungranted_scope_do_not_connect(self) -> None:
        state, _ = self.start()
        for code, error in ((None, None), ("code", "denied")):
            with self.assertRaises(APIError):
                self.service.callback(state, code, error)
        self.service.callback(state, None, "access_denied")
        self.assertEqual(self.records(), [])
        state, _ = self.start("synthetic-oauth-key-002")
        self.transport.scopes = "unrelated.scope"
        with self.assertRaises(APIError):
            self.service.callback(state, "code", None)
        self.assertEqual(self.records(), [])

    def test_concurrent_callbacks_exchange_once(self) -> None:
        state, _ = self.start()
        entered, released = Event(), Event()

        def hold() -> None:
            entered.set()
            if not released.wait(10):
                raise AssertionError("callback test timed out")

        self.transport.after = hold
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.service.callback, state, "code", None)
            self.assertTrue(entered.wait(10))
            try:
                with self.assertRaises(APIError):
                    self.service.callback(state, "code", None)
            finally:
                released.set()
            first.result(10)
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(len(self.records()), 1)

    def test_revocation_during_exchange_blocks_commit_and_cleans_secrets(self) -> None:
        state, auth = self.start()
        self.transport.after = self.revoke_member
        with self.assertRaises(APIError):
            self.service.callback(state, "code", None)
        self.assertEqual(self.records(), [])
        self.assertFalse(self.get(path(self.tenant, auth))["cleanup_pending"])
        with self.assertRaises(APIError):
            self.start()

    def test_token_write_failure_and_exchange_response_loss_never_exchange_again(self) -> None:
        for failure in ("exchange", "secret"):
            state, auth = self.start("synthetic-failure-key-" + failure)
            if failure == "exchange":
                self.transport.fail = True
                with self.assertRaises(APIError):
                    self.service.callback(state, "SYNTHETIC_CODE", None)
                self.transport.fail = False
            else:
                with patch.object(
                    self.secrets, "put", side_effect=RuntimeError("SYNTHETIC_REFRESH")
                ):
                    with self.assertRaises(APIError):
                        self.service.callback(state, "code", None)
            self.service.recover(self.tenant, auth)
            count = len(self.transport.calls)
            with self.assertRaises(APIError):
                self.service.callback(state, "code", None)
            self.assertEqual(len(self.transport.calls), count)
        self.assertEqual(self.records(), [])

    def test_cleanup_failure_retains_durable_work_and_recovers(self) -> None:
        state, auth = self.start()
        self.transport.fail = True
        with patch.object(self.secrets, "revoke", side_effect=RuntimeError("secret failure")):
            with self.assertRaises(APIError):
                self.service.callback(state, "code", None)
        self.assertTrue(self.get(path(self.tenant, auth))["cleanup_pending"])
        self.service.recover(self.tenant, auth)
        self.assertFalse(self.get(path(self.tenant, auth))["cleanup_pending"])

    def test_start_secret_response_loss_reuses_first_value(self) -> None:
        original = self.secrets.put

        def lost(*args: Any, **kwargs: Any) -> str:
            original(*args, **kwargs)
            raise RuntimeError("lost secret response")

        with patch.object(self.secrets, "put", side_effect=lost):
            with self.assertRaises(APIError):
                self.start()
        state, auth = self.start()
        self.assertEqual(self.start(), (state, auth))
        self.service.callback(state, "code", None)
        self.assertEqual(len(self.records()), 1)

    def test_nonce_binding_corruption_prevents_exchange(self) -> None:
        state, auth = self.start()
        saved = self.get(path(self.tenant, auth))
        saved["nonce_hash"] = "0" * 64
        self.put(path(self.tenant, auth), saved)
        with self.assertRaises(APIError):
            self.service.callback(state, "code", None)
        self.assertEqual(self.transport.calls, [])

    def test_account_and_tenant_fences_deny_callback(self) -> None:
        from brainoid_contracts.deletion import lifecycle_account_path, lifecycle_tenant_path

        for fence in (lifecycle_account_path(self.uid), lifecycle_tenant_path(self.tenant)):
            state, _ = self.start("synthetic-fence-" + fence.replace("/", "-"))
            self.put(fence, {"operation_id": "denied"})
            with self.assertRaises(APIError):
                self.service.callback(state, "code", None)
            self.store.run(lambda tx, fence=fence: tx.delete(fence))
        self.assertEqual(self.transport.calls, [])

    def test_revocation_after_token_write_prevents_integration_and_destroys_slot(self) -> None:
        state, auth = self.start()
        saved = self.get(path(self.tenant, auth))
        put = self.secrets.put

        def after_write(*args: Any, **kwargs: Any) -> str:
            reference = put(*args, **kwargs)
            self.revoke_member()
            return reference

        with patch.object(self.secrets, "put", side_effect=after_write):
            with self.assertRaises(APIError):
                self.service.callback(state, "code", None)
        self.assertEqual(self.records(), [])
        with self.assertRaises(SecretUnavailable):
            self.secrets.get(f"local-calendar/{saved['token_slot']}/versions/1")

    def test_expired_recovery_during_exchange_fences_late_completion(self) -> None:
        state, auth = self.start()

        def expire() -> None:
            self.clock.advance(600)
            self.service.recover(self.tenant, auth)

        self.transport.after = expire
        with self.assertRaises(APIError):
            self.service.callback(state, "code", None)
        self.assertEqual(self.records(), [])
        self.assertEqual(self.get(path(self.tenant, auth))["status"], "failed")

    def test_control_ledger_unavailability_blocks_exchange(self) -> None:
        from brainoid_policy.control_ledger import LedgerError

        state, _ = self.start()
        with patch.object(self.objects, "names", side_effect=LedgerError()):
            with self.assertRaises(APIError):
                self.service.callback(state, "code", None)
        self.assertEqual(self.transport.calls, [])

    def test_callback_rejects_changed_request_owner_and_expiry_binding(self) -> None:
        for field, value in (
            ("request", {"scope_profile": "read_write", "return_target": "web"}),
            ("owner_user_id", "demo-a-owner"),
            ("expires_at", self.clock.now() + timedelta(hours=2)),
        ):
            state, auth = self.start("synthetic-bound-fields-" + field, profile="read")
            saved = self.get(path(self.tenant, auth))
            saved[field] = value
            self.put(path(self.tenant, auth), saved)
            with self.assertRaises(APIError):
                self.service.callback(state, "code", None)
        self.assertEqual(self.transport.calls, [])
