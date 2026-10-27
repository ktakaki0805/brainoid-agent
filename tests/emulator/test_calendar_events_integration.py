"""Identical Calendar reader/selection cases on real Firestore transactions."""

import os

from brainoid_api.app import create_app
from brainoid_api.auth.runtime import authenticated_identity
from brainoid_persistence.firestore import FirestoreTransactions
from fakes import calendar_events
from google.auth.credentials import AnonymousCredentials
from google.cloud.firestore_v1 import Client
from starlette.testclient import TestClient

from scripts.demo_emulator import DATABASE, PROJECT, DemoEmulator


class CalendarEventsIntegrationTests(calendar_events.CalendarEventsScenarios):
    def setUp(self) -> None:
        emulator = DemoEmulator(os.environ)
        emulator.reset()
        self.addCleanup(emulator.reset)
        client = Client(project=PROJECT, database=DATABASE, credentials=AnonymousCredentials())  # type: ignore[no-untyped-call]
        self.addCleanup(client.close)
        self.store = FirestoreTransactions(client)
        self.setup_events()

    def client(self) -> TestClient:
        # Shared scenarios inject an identity only after real Emulator startup.
        # The dedicated Firebase bearer test below uses a separate app unchanged.
        app = create_app()
        app.dependency_overrides[authenticated_identity] = lambda: self.identity()
        return TestClient(app)

    def test_real_firebase_bearer_selection_and_read(self) -> None:
        from dataclasses import replace
        from datetime import UTC, datetime
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
            options={"projectId": PROJECT}, name="calendar-reader-" + str(uuid4())
        )
        self.addCleanup(firebase_admin.delete_app, firebase)
        token = auth.create_custom_token(self.uid, app=firebase).decode("ascii")
        signed = emulator.request(
            "auth",
            "POST",
            "/identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken?key=fixture-key",
            {"token": token, "returnSecureToken": True},
        )
        self.clock.advance((datetime.now(UTC) - self.clock.now()).total_seconds())
        app = create_app()

        def services(request: Request) -> AuthServices:
            original: AuthServices = request.app.state.auth_services
            return replace(original, calendar=self.service, calendar_events=self.events)

        app.dependency_overrides[get_auth_services] = services
        with TestClient(app) as client:
            url = f"/v1/tenants/{self.tenant}/integrations/{self.integration}"
            params = self.query.model_dump(mode="json", exclude_none=True)
            self.assertEqual(client.get(url + "/calendar-events", params=params).status_code, 401)
            headers = {
                "Authorization": "Bearer " + signed["idToken"],
                "Idempotency-Key": "calendar-real-auth-selection",
                "If-Match": '"2"',
            }
            selected = client.patch(
                url, json={"allowed_calendar_ids": [self.query.calendar_id]}, headers=headers
            )
            self.assertEqual(selected.status_code, 200, selected.text)
            result = client.get(url + "/calendar-events", params=params, headers=headers)
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(len(result.json()["items"]), 1)
            self.assertNotIn("secret_ref", result.text)
