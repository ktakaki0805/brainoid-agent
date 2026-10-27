"""Calendar OAuth shared scenarios and API boundary regression tests."""

import json
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from brainoid_api.app import create_app
from brainoid_api.auth.runtime import authenticated_identity
from brainoid_api.errors import APIError
from brainoid_api.services.calendar_authorization import path
from brainoid_api.settings import load_settings
from brainoid_persistence.firestore import PersistenceUnavailable
from fakes import calendar_scenarios
from starlette.testclient import TestClient
from test_api_foundation import LOCAL_ENV
from test_notifications import NotificationStore


class CalendarTests(calendar_scenarios.CalendarScenarios):
    def setUp(self) -> None:
        self.store = NotificationStore()
        self.setup_calendar()

    def test_http_contract_allowlist_and_redaction(self) -> None:
        with self.client() as client:
            url = f"/v1/tenants/{self.tenant}/integrations/google-calendar/authorizations"
            for body in (
                {"scope_profile": "read", "return_target": "https://evil.test"},
                {
                    "scope_profile": "read",
                    "return_target": "web",
                    "redirect_uri": "https://evil.test",
                },
            ):
                self.assertEqual(
                    client.post(
                        url, json=body, headers={"Idempotency-Key": "synthetic-http-key-001"}
                    ).status_code,
                    422,
                )
            response = client.post(
                url,
                json={"scope_profile": "read_write", "return_target": "web"},
                headers={"Idempotency-Key": "synthetic-http-key-001"},
            )
            self.assertEqual(response.status_code, 201)
            state = parse_qs(urlsplit(response.json()["authorization_url"]).query)["state"][0]
            self.assertEqual(response.headers["cache-control"], "no-store")
            with self.assertLogs("brainoid_api", level="INFO") as logs:
                self.transport.fail = True
                result = client.get(
                    "/v1/oauth/google-calendar/callback",
                    params={"state": state, "code": "SYNTHETIC_CODE"},
                    headers={"Authorization": "Bearer SYNTHETIC_AUTHORIZATION"},
                    follow_redirects=False,
                )
                malformed = client.get(
                    "/v1/oauth/google-calendar/callback",
                    params={"state": state, "code": "SYNTHETIC_CODE", "error": "SYNTHETIC_ERROR"},
                )
            combined = json.dumps(
                [
                    logs.output,
                    result.text,
                    dict(result.headers),
                    malformed.text,
                    dict(malformed.headers),
                ]
            )
            for value in (
                state,
                "SYNTHETIC_CODE",
                "SYNTHETIC_ACCESS",
                "SYNTHETIC_REFRESH",
                "SYNTHETIC_ERROR",
                "SYNTHETIC_AUTHORIZATION",
            ):
                self.assertNotIn(value, combined)
            self.transport.fail = False
            state, _ = self.start("synthetic-http-key-002")
            result = client.get(
                "/v1/oauth/google-calendar/callback",
                params={"state": state, "code": "code"},
                follow_redirects=False,
            )
            self.assertEqual(result.status_code, 303)
            self.assertEqual(result.headers["location"], self.service.returns["mobile"])
            self.assertEqual(result.headers["referrer-policy"], "no-referrer")
            for query in (
                f"state={state}&state={state}&code=code",
                f"state={state}&code=code&owner_user_id=other",
            ):
                self.assertEqual(
                    client.get("/v1/oauth/google-calendar/callback?" + query).status_code, 400
                )

    def test_default_runtime_fails_closed(self) -> None:
        app = create_app(settings_loader=lambda: load_settings(LOCAL_ENV))
        app.dependency_overrides[authenticated_identity] = lambda: self.identity()
        with TestClient(app) as client:
            response = client.post(
                f"/v1/tenants/{self.tenant}/integrations/google-calendar/authorizations",
                json={"scope_profile": "read", "return_target": "web"},
                headers={"Idempotency-Key": "synthetic-default-key"},
            )
            self.assertEqual(response.status_code, 503)

    def test_lost_connected_commit_response_preserves_token_and_single_exchange(self) -> None:
        state, auth = self.start()
        lost: list[bool] = []

        def after() -> None:
            saved = self.store.data.get(path(self.tenant, auth), {})
            if saved.get("status") == "connected" and not lost:
                lost.append(True)
                raise PersistenceUnavailable()

        self.store.after_commit = after
        with self.assertRaises(APIError):
            self.service.callback(state, "code", None)
        self.store.after_commit = lambda: None
        self.service.recover(self.tenant, auth)
        saved = self.get(path(self.tenant, auth))
        self.assertEqual(saved["status"], "connected")
        self.assertIn(b"SYNTHETIC_REFRESH", self.secrets.get(saved["secret_ref"]))
        self.assertEqual(len(self.transport.calls), 1)

    def test_precommit_failure_recovers_and_no_orphan(self) -> None:
        state, auth = self.start()
        original = self.secrets.put

        def fail_after(*args: object, **kwargs: object) -> str:
            result = original(*args, **kwargs)  # type: ignore[arg-type]
            self.store.fail_commit = True
            return result

        with patch.object(self.secrets, "put", side_effect=fail_after):
            with self.assertRaises(APIError):
                self.service.callback(state, "code", None)
        self.store.fail_commit = False
        self.clock.advance(600)
        self.service.recover(self.tenant, auth)
        self.assertEqual(self.records(), [])
        self.assertFalse(self.get(path(self.tenant, auth))["cleanup_pending"])

    def test_http_metadata_pagination_is_owner_bound_and_excludes_credentials(self) -> None:
        with self.client() as client:
            for suffix in ("1", "2"):
                state, _ = self.start("synthetic-list-key-" + suffix)
                self.service.callback(state, "code", None)
            url = f"/v1/tenants/{self.tenant}/integrations"
            first = client.get(url, params={"limit": 1}).json()
            self.assertEqual(len(first["items"]), 1)
            self.assertNotIn("secret_ref", json.dumps(first))
            self.assertNotIn("granted_scopes", json.dumps(first))
            self.assertIsNotNone(first["next_cursor"])
            second = client.get(url, params={"limit": 1, "cursor": first["next_cursor"]}).json()
            self.assertNotEqual(first["items"][0]["id"], second["items"][0]["id"])
            self.assertIsNone(second["next_cursor"])
            from brainoid_contracts import FirebaseUid

            self.uid = FirebaseUid("demo-a-owner")
            self.assertEqual(client.get(url).json()["items"], [])
            self.assertNotEqual(
                client.get(url, params={"limit": 1, "cursor": first["next_cursor"]}).status_code,
                200,
            )
