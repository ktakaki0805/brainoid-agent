"""BA-FND-02 / T-027: startup, health boundaries, correlation and log privacy."""

import asyncio
import json
import logging
import os
import subprocess
import sys
import traceback
import unittest
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Annotated
from unittest.mock import patch
from uuid import UUID

from brainoid_api.app import create_app
from brainoid_api.errors import APIError, ErrorCode
from brainoid_api.health import ReadinessProbe, get_settings
from brainoid_api.settings import ConfigurationError, Settings, load_settings
from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel
from starlette.responses import StreamingResponse
from starlette.testclient import TestClient

LOCAL_ENV = {
    "APP_ENV": "local-fake",
    "AUTH_PROVIDER": "fake",
    "MODEL_PROVIDER": "fake",
    "GCP_PROJECT_ID": "demo-brainoid",
    "GCP_REGION": "asia-northeast1",
    "FIRESTORE_DATABASE_ID": "(default)",
    "MODEL_ID": "fake-model",
}
CLOUD_ENV = {
    **LOCAL_ENV,
    "APP_ENV": "production",
    "AUTH_PROVIDER": "firebase",
    "MODEL_PROVIDER": "vertex",
    "GCP_PROJECT_ID": "brainoid-test-project",
    "MODEL_ID": "explicit-model",
    "CURSOR_SIGNING_KEY": "synthetic-cursor-signing-key-for-tests-only",
}
EMULATOR_ENV = {
    **LOCAL_ENV,
    "APP_ENV": "local-emulator",
    "AUTH_PROVIDER": "firebase",
    "FIRESTORE_EMULATOR_HOST": "127.0.0.1:8080",
    "FIREBASE_AUTH_EMULATOR_HOST": "127.0.0.1:9099",
    "CURSOR_SIGNING_KEY": "synthetic-cursor-signing-key-for-tests-only",
}
SECRET = "fixture-secret-not-for-logs"


def local_app() -> FastAPI:
    return create_app(settings_loader=lambda: load_settings(LOCAL_ENV))


class SettingsTests(unittest.TestCase):
    def test_explicit_supported_environments_preserve_project_model_and_region(self) -> None:
        for env in (LOCAL_ENV, EMULATOR_ENV, CLOUD_ENV, {**CLOUD_ENV, "APP_ENV": "staging"}):
            with self.subTest(environment=env["APP_ENV"]):
                settings = load_settings(env)
                self.assertEqual(settings.gcp_project_id, env["GCP_PROJECT_ID"])
                self.assertEqual(settings.model_provider, env["MODEL_PROVIDER"])
                self.assertEqual(settings.model_id, env["MODEL_ID"])
                self.assertEqual(settings.gcp_region, env["GCP_REGION"])

    def test_required_settings_are_never_inferred(self) -> None:
        for name in LOCAL_ENV:
            with self.subTest(name=name), self.assertRaises(ConfigurationError):
                load_settings({key: value for key, value in LOCAL_ENV.items() if key != name})

    def test_t027_cloud_rejects_fake_auth_and_all_emulator_values(self) -> None:
        for environment in ("production", "staging"):
            for overrides in (
                {"AUTH_PROVIDER": "fake"},
                {"MODEL_PROVIDER": "fake"},
                *(
                    {name: value}
                    for name in ("FIRESTORE_EMULATOR_HOST", "FIREBASE_AUTH_EMULATOR_HOST")
                    for value in ("127.0.0.1:8080", "")
                ),
            ):
                with (
                    self.subTest(environment=environment, overrides=overrides),
                    self.assertRaises(ConfigurationError),
                ):
                    load_settings({**CLOUD_ENV, "APP_ENV": environment, **overrides})

    def test_invalid_and_contradictory_configuration_is_rejected(self) -> None:
        cases = [
            {"APP_ENV": "prod"},
            {"MODEL_PROVIDER": "unknown"},
            {"AUTH_PROVIDER": "firebase"},
            {"MODEL_ID": ""},
            {"GCP_REGION": ""},
            {"GCP_PROJECT_ID": "real-project"},
            {"READINESS_TIMEOUT_SECONDS": "0"},
            {"READINESS_TIMEOUT_SECONDS": "nan"},
            {"READINESS_TIMEOUT_SECONDS": "31"},
            {"LOG_LEVEL": "TRACE"},
            {"GOOGLE_CLOUD_PROJECT": "another-project"},
            {"GCLOUD_PROJECT": "another-project"},
            {"FIRESTORE_EMULATOR_HOST": "localhost:8080"},
        ]
        for overrides in cases:
            with self.subTest(overrides=overrides), self.assertRaises(ConfigurationError):
                load_settings({**LOCAL_ENV, **overrides})
        for name in ("FIRESTORE_EMULATOR_HOST", "FIREBASE_AUTH_EMULATOR_HOST"):
            with self.subTest(missing=name), self.assertRaises(ConfigurationError):
                load_settings({key: value for key, value in EMULATOR_ENV.items() if key != name})
            for host in (
                "localhost",
                "http://localhost:80",
                "user:pass@localhost:80",
                "localhost:80/path",
                "localhost:0",
                "localhost:65536",
                "localhost:bad",
            ):
                with self.subTest(host=host), self.assertRaises(ConfigurationError):
                    load_settings({**EMULATOR_ENV, name: host})

    def test_configuration_errors_hide_values_even_in_formatted_tracebacks(self) -> None:
        for overrides in (
            {"AUTH_PROVIDER": SECRET},
            {"GOOGLE_CLOUD_PROJECT": SECRET},
            {"FIRESTORE_EMULATOR_HOST": SECRET},
        ):
            try:
                load_settings({**CLOUD_ENV, **overrides})
            except ConfigurationError as exception:
                self.assertNotIn(SECRET, "".join(traceback.format_exception(exception)))
            else:
                self.fail("Invalid configuration was accepted")

    def test_missing_settings_fail_at_lifespan_before_dependencies_start(self) -> None:
        entered = False

        @asynccontextmanager
        async def dependencies(settings: Settings) -> AsyncIterator[Sequence[ReadinessProbe]]:
            nonlocal entered
            entered = True
            yield ()

        with patch.dict(os.environ, {}, clear=True):
            app = create_app(dependency_lifespan=dependencies)
            with (
                self.assertLogs("brainoid_api"),
                self.assertRaisesRegex(RuntimeError, "startup failed"),
            ):
                with TestClient(app):
                    self.fail("Invalid configuration served requests")
        self.assertFalse(entered)
        self.assertFalse(app.state.runtime.started)

    def test_t027_real_entrypoint_exits_nonzero_without_leaking_invalid_value(self) -> None:
        for overrides in (
            {"AUTH_PROVIDER": "fake"},
            {"AUTH_PROVIDER": SECRET},
            {"FIRESTORE_EMULATOR_HOST": "127.0.0.1:8080"},
        ):
            with self.subTest(overrides=overrides):
                result = subprocess.run(
                    [sys.executable, "-m", "brainoid_api"],
                    env={**CLOUD_ENV, "MODEL_ID": SECRET, **overrides},
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("startup failed", result.stderr)
                self.assertNotIn(SECRET, result.stdout + result.stderr)


class MutableProbe:
    ready = True
    fail = False
    hang = False
    cancelled = False

    async def is_ready(self) -> bool:
        if self.fail:
            raise RuntimeError(SECRET)
        if self.hang:
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled = True
        return self.ready


class HealthTests(unittest.TestCase):
    def test_readiness_tracks_dependencies_and_liveness_stays_alive(self) -> None:
        probe = MutableProbe()
        phases: list[str] = []

        @asynccontextmanager
        async def dependencies(settings: Settings) -> AsyncIterator[Sequence[ReadinessProbe]]:
            self.assertEqual(settings.gcp_project_id, "demo-brainoid")
            phases.append("start")
            try:
                yield (probe,)
            finally:
                phases.append("stop")

        app = create_app(
            settings_loader=lambda: load_settings(LOCAL_ENV), dependency_lifespan=dependencies
        )
        with TestClient(app) as client:
            self.assertEqual(phases, ["start"])
            self.assertEqual(client.get("/health/ready").json(), {"status": "ready"})
            probe.ready = False
            response = client.get("/health/ready")
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()["error"]["code"], "temporarily_unavailable")
            self.assertTrue(response.json()["error"]["retryable"])
            self.assertEqual(
                response.headers["x-request-id"], response.json()["error"]["request_id"]
            )
            self.assertEqual(client.get("/health/live").json(), {"status": "ok"})
            probe.ready = True
            self.assertEqual(client.get("/health/ready").status_code, 200)
            with self.assertLogs("brainoid_api") as logs:
                probe.fail = True
                response = client.get("/health/ready")
            self.assertEqual(response.status_code, 503)
            self.assertNotIn(SECRET, response.text + repr(logs.records))
        self.assertEqual(phases, ["start", "stop"])
        self.assertFalse(app.state.runtime.started)
        self.assertIsNone(app.state.runtime.settings)

    def test_slow_probe_is_cancelled_within_readiness_deadline(self) -> None:
        probe = MutableProbe()
        probe.hang = True

        @asynccontextmanager
        async def dependencies(settings: Settings) -> AsyncIterator[Sequence[ReadinessProbe]]:
            yield (probe,)

        app = create_app(
            settings_loader=lambda: load_settings(
                {**LOCAL_ENV, "READINESS_TIMEOUT_SECONDS": "0.01"}
            ),
            dependency_lifespan=dependencies,
        )
        with TestClient(app) as client:
            self.assertEqual(client.get("/health/ready").status_code, 503)
            self.assertTrue(probe.cancelled)
            self.assertEqual(client.get("/health/live").status_code, 200)
            probe.hang = False
            self.assertEqual(client.get("/health/ready").status_code, 200)

    def test_no_lifespan_cannot_report_ready(self) -> None:
        client = TestClient(local_app())
        self.assertEqual(client.get("/health/ready").status_code, 503)
        self.assertEqual(client.get("/health/live").status_code, 200)
        client.close()

    def test_dependency_startup_and_shutdown_failures_are_sanitized(self) -> None:
        for phase in ("startup", "shutdown"):

            @asynccontextmanager
            async def dependencies(
                settings: Settings, failure_phase: str = phase
            ) -> AsyncIterator[Sequence[ReadinessProbe]]:
                if failure_phase == "startup":
                    raise RuntimeError(SECRET)
                yield ()
                raise RuntimeError(SECRET)

            app = create_app(
                settings_loader=lambda: load_settings(LOCAL_ENV), dependency_lifespan=dependencies
            )
            with self.subTest(phase=phase), self.assertLogs("brainoid_api") as logs:
                try:
                    with TestClient(app):
                        pass
                except RuntimeError as exception:
                    self.assertIn(phase + " failed", str(exception))
                    self.assertNotIn(SECRET, "".join(traceback.format_exception(exception)))
                else:
                    self.fail("Expected a lifecycle failure")
            self.assertNotIn(SECRET, str([record.__dict__ for record in logs.records]))
            self.assertFalse(app.state.runtime.started)

    def test_settings_are_available_through_fastapi_dependency_injection(self) -> None:
        app = local_app()

        @app.get("/test-settings")
        async def settings_route(
            settings: Annotated[Settings, Depends(get_settings)],
        ) -> dict[str, str]:
            return {"environment": settings.app_env}

        with TestClient(app) as client:
            self.assertEqual(client.get("/test-settings").json(), {"environment": "local-fake"})


class ErrorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = local_app()

        class Input(BaseModel):
            count: int

        @self.app.post("/input")
        async def input_route(payload: Input) -> dict[str, int]:
            return {"count": payload.count}

        @self.app.post("/fail")
        async def fail(request: Request) -> None:
            raise ValueError((await request.body()).decode() + request.headers["Authorization"])

        @self.app.get("/unavailable")
        async def unavailable() -> None:
            raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)

        @self.app.get("/http-error")
        async def http_error() -> None:
            raise HTTPException(status_code=403, detail=SECRET)

    def test_common_errors_have_server_generated_correlated_ids_and_no_content(self) -> None:
        with TestClient(self.app) as client, self.assertLogs("brainoid_api", logging.DEBUG) as logs:
            responses = [
                client.post("/input", json={"count": SECRET}),
                client.post(
                    "/input", content='{"' + SECRET, headers={"Content-Type": "application/json"}
                ),
                client.post(
                    "/fail?token=" + SECRET,
                    json={"content": SECRET},
                    headers={"Authorization": "Bearer " + SECRET, "X-Request-ID": SECRET},
                ),
                client.get("/unavailable"),
                client.get("/http-error"),
                client.get("/missing/" + SECRET),
                client.post("/health/live"),
            ]
        expected = [
            (422, "validation_failed", False),
            (422, "validation_failed", False),
            (500, "internal_error", False),
            (503, "temporarily_unavailable", True),
            (403, "operation_forbidden", False),
            (404, "resource_not_found", False),
            (405, "method_not_allowed", False),
        ]
        records = [json.loads(record.getMessage()) for record in logs.records]
        ids = set()
        for response, (status, code, retryable) in zip(responses, expected, strict=True):
            self.assertEqual(response.status_code, status)
            error = response.json()["error"]
            self.assertEqual(set(error), {"code", "message", "request_id", "retryable"})
            self.assertEqual(error["code"], code)
            self.assertEqual(error["retryable"], retryable)
            self.assertEqual(UUID(error["request_id"]).version, 4)
            self.assertEqual(error["request_id"], response.headers["x-request-id"])
            self.assertEqual(response.headers["cache-control"], "no-store")
            ids.add(error["request_id"])
            record = next(
                record for record in records if record["request_id"] == error["request_id"]
            )
            self.assertEqual(
                record,
                {
                    "event": "request_completed",
                    "request_id": error["request_id"],
                    "status": status,
                    "error_code": code,
                },
            )
            self.assertNotIn(SECRET, response.text)
        self.assertEqual(len(ids), len(responses))
        self.assertEqual(responses[-1].headers["allow"], "GET")
        self.assertNotIn(SECRET, str([record.__dict__ for record in logs.records]))
        self.assertTrue(all(record.exc_info is None for record in logs.records))

    def test_success_is_logged_without_header_body_or_url_and_ignores_caller_id(self) -> None:
        with TestClient(self.app) as client, self.assertLogs("brainoid_api") as logs:
            response = client.post(
                "/input?secret=" + SECRET,
                json={"count": 42, "content": SECRET},
                headers={"Authorization": SECRET, "Cookie": SECRET, "X-Request-ID": SECRET},
            )
        self.assertEqual(response.json(), {"count": 42})
        record = json.loads(logs.records[0].getMessage())
        self.assertEqual(record["status"], 200)
        self.assertEqual(record["request_id"], response.headers["x-request-id"])
        self.assertIsNone(record["error_code"])
        self.assertNotIn(SECRET, str([item.__dict__ for item in logs.records]))

    def test_partial_stream_failure_does_not_expose_exception_to_server(self) -> None:
        @self.app.get("/stream")
        async def stream() -> StreamingResponse:
            async def chunks() -> AsyncIterator[bytes]:
                yield b"first"
                raise RuntimeError(SECRET)

            return StreamingResponse(chunks())

        with TestClient(self.app) as client, self.assertLogs("brainoid_api") as logs:
            try:
                client.get("/stream")
            except RuntimeError as exception:
                self.assertNotIn(SECRET, "".join(traceback.format_exception(exception)))
            else:
                self.fail("A partially sent response must abort")
        self.assertNotIn(SECRET, str([record.__dict__ for record in logs.records]))
        self.assertEqual(json.loads(logs.records[0].getMessage())["error_code"], "internal_error")


if __name__ == "__main__":
    unittest.main()
