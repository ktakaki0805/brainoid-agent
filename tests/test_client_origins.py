"""Registered WebView/browser origins retain bearer authorization and no-store responses."""

import unittest
from collections.abc import Callable

from brainoid_api.app import create_app
from brainoid_api.settings import ConfigurationError, Settings, load_settings
from starlette.testclient import TestClient
from test_api_foundation import CLOUD_ENV, LOCAL_ENV


def settings_loader(settings: Settings) -> Callable[[], Settings]:
    return lambda: settings


class ClientOriginsTests(unittest.TestCase):
    def test_registered_origin_preflight_and_error_response(self) -> None:
        settings = load_settings(
            {**LOCAL_ENV, "CLIENT_ORIGINS": "https://app.example.test,capacitor://localhost"}
        )
        with TestClient(create_app(settings_loader=settings_loader(settings))) as client:
            for origin in settings.client_origins:
                preflight = client.options(
                    "/v1/tenants",
                    headers={
                        "Origin": origin,
                        "Access-Control-Request-Method": "POST",
                        "Access-Control-Request-Headers": "authorization,idempotency-key,content-type",
                    },
                )
                self.assertEqual(preflight.status_code, 200)
                self.assertEqual(preflight.headers["cache-control"], "no-store")
                self.assertIn("x-request-id", preflight.headers)
                self.assertEqual(preflight.headers["access-control-allow-origin"], origin)
                self.assertNotIn("access-control-allow-credentials", preflight.headers)
                denied = client.get("/v1/tenants", headers={"Origin": origin})
                self.assertEqual(denied.status_code, 401)
                self.assertEqual(denied.headers["access-control-allow-origin"], origin)
                self.assertEqual(denied.headers["cache-control"], "no-store")

    def test_unknown_origins_and_unconfigured_origins_are_not_reflected(self) -> None:
        for configured in ("", "https://app.example.test"):
            settings = load_settings({**LOCAL_ENV, "CLIENT_ORIGINS": configured})
            with TestClient(create_app(settings_loader=settings_loader(settings))) as client:
                result = client.options(
                    "/v1/tenants",
                    headers={
                        "Origin": "https://unregistered.example.test",
                        "Access-Control-Request-Method": "GET",
                    },
                )
                self.assertEqual(result.status_code, 400)
                self.assertNotIn("access-control-allow-origin", result.headers)

    def test_wildcards_credentials_paths_and_cloud_insecure_origins_are_rejected(self) -> None:
        for origin in (
            "*",
            "null",
            "https://user:secret@example.test",
            "https://example.test/",
            "https://example.test?secret=x",
            "https://*.example.test",
            "http://example.test",
            "http://127.0.0.1:5173",
            "capacitor://untrusted",
        ):
            with self.subTest(origin=origin):
                with self.assertRaises(ConfigurationError) as error:
                    load_settings({**CLOUD_ENV, "CLIENT_ORIGINS": origin})
                self.assertNotIn(origin, str(error.exception))
        self.assertEqual(
            load_settings({**LOCAL_ENV, "CLIENT_ORIGINS": "http://127.0.0.1:5173"}).client_origins,
            ("http://127.0.0.1:5173",),
        )
