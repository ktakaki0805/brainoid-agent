"""The identical Calendar protocol scenarios on real Firestore transactions."""

import os

from brainoid_persistence.firestore import FirestoreTransactions
from fakes import calendar_scenarios
from google.auth.credentials import AnonymousCredentials
from google.cloud.firestore_v1 import Client

from scripts.demo_emulator import DATABASE, PROJECT, DemoEmulator


class CalendarIntegrationTests(calendar_scenarios.CalendarScenarios):
    def setUp(self) -> None:
        emulator = DemoEmulator(os.environ)
        emulator.reset()
        self.addCleanup(emulator.reset)
        client = Client(project=PROJECT, database=DATABASE, credentials=AnonymousCredentials())  # type: ignore[no-untyped-call]
        self.addCleanup(client.close)
        self.store = FirestoreTransactions(client)
        self.setup_calendar()

    def test_real_firebase_bearer_http_and_anonymous_callback(self) -> None:
        from dataclasses import replace
        from datetime import UTC, datetime
        from urllib.parse import parse_qs, urlsplit
        from uuid import uuid4

        import firebase_admin  # type: ignore[import-untyped]
        from brainoid_api.app import create_app
        from brainoid_api.auth.runtime import AuthServices, get_auth_services
        from fastapi import Request
        from firebase_admin import auth
        from starlette.testclient import TestClient

        emulator = DemoEmulator(os.environ)
        emulator.seed()
        firebase = firebase_admin.initialize_app(
            options={"projectId": PROJECT, "httpTimeout": 10}, name="calendar-auth-" + str(uuid4())
        )
        self.addCleanup(firebase_admin.delete_app, firebase)
        custom = auth.create_custom_token(self.uid, app=firebase).decode("ascii")
        response = emulator.request(
            "auth",
            "POST",
            "/identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken?key=fixture-key",
            {"token": custom, "returnSecureToken": True},
        )
        self.clock.advance((datetime.now(UTC) - self.clock.now()).total_seconds())
        app = create_app()

        def services(request: Request) -> AuthServices:
            original: AuthServices = request.app.state.auth_services
            return replace(original, calendar=self.service)

        app.dependency_overrides[get_auth_services] = services
        with TestClient(app) as client:
            url = f"/v1/tenants/{self.tenant}/integrations/google-calendar/authorizations"
            body = {"scope_profile": "read", "return_target": "web"}
            self.assertEqual(
                client.post(
                    url, json=body, headers={"Idempotency-Key": "emulator-calendar-http"}
                ).status_code,
                401,
            )
            headers = {
                "Authorization": "Bearer " + response["idToken"],
                "Idempotency-Key": "emulator-calendar-http",
            }
            started = client.post(url, json=body, headers=headers)
            self.assertEqual(started.status_code, 201, started.text)
            state = parse_qs(urlsplit(started.json()["authorization_url"]).query)["state"][0]
            callback = client.get(
                "/v1/oauth/google-calendar/callback",
                params={"state": state, "code": "synthetic-code"},
                follow_redirects=False,
            )
            self.assertEqual(callback.status_code, 303, callback.text)
            self.assertEqual(callback.headers["location"], self.service.returns["web"])
            public = client.get(f"/v1/tenants/{self.tenant}/integrations", headers=headers)
            self.assertEqual(public.status_code, 200, public.text)
            self.assertEqual(len(public.json()["items"]), 1)
            self.assertNotIn("secret_ref", public.text)
