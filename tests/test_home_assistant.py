"""DEV06 / T009, T018, T019: synthetic HA, exact identity and fixed HTTP boundary."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import httpx
from brainoid_device.app import DeviceApplication
from brainoid_device.config import Settings
from brainoid_device.home_assistant import Configuration, HomeAssistant, HomeError, configure
from brainoid_device.ledger import DeviceStorage
from brainoid_device.runtime import Runtime
from brainoid_device.storage import CredentialStore

SECRET = "synthetic-ha-token-do-not-disclose"


def states() -> dict[str, dict[str, Any]]:
    return {
        "light.first": {
            "entity_id": "light.first",
            "state": "off",
            "attributes": {
                "friendly_name": "照明",
                "supported_color_modes": ["brightness"],
                "brightness": 0,
            },
        },
        "light.second": {
            "entity_id": "light.second",
            "state": "on",
            "attributes": {"friendly_name": "照明", "supported_color_modes": ["onoff"]},
        },
        "climate.room": {
            "entity_id": "climate.room",
            "state": "heat",
            "attributes": {
                "friendly_name": "空調",
                "supported_features": 1,
                "temperature": 21.0,
                "min_temp": 18,
                "max_temp": 28,
                "hvac_modes": ["off", "heat", "cool"],
            },
        },
    }


class FakeHA:
    def __init__(self) -> None:
        self.states = states()
        self.calls: list[tuple[str, str, Any]] = []
        self.unit = "°C"
        self.fail = ""
        self.entered = asyncio.Event()
        self.release: asyncio.Event | None = None

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer " + SECRET
        self.calls.append(
            (
                request.method,
                request.url.path,
                json.loads(request.content) if request.content else None,
            )
        )
        if self.release:
            self.entered.set()
            await self.release.wait()
        if self.fail == "redirect":
            return httpx.Response(302, headers={"location": "https://untrusted.invalid"})
        if self.fail == "malformed":
            return httpx.Response(200, content=b"not-json")
        if self.fail == "oversize":
            return httpx.Response(200, content=b" " * (1024 * 1024 + 1))
        if self.fail == "timeout":
            raise httpx.ReadTimeout(SECRET)
        if request.url.path == "/api/config":
            return httpx.Response(200, json={"unit_system": {"temperature": self.unit}})
        if request.url.path == "/api/states":
            return httpx.Response(200, json=list(self.states.values()))
        if request.method == "GET":
            value = self.states.get(request.url.path.removeprefix("/api/states/"))
            return httpx.Response(200 if value else 404, json=value)
        if self.fail == "response_loss":
            raise httpx.ReadError(SECRET)
        return httpx.Response(200, json=[])


class HomeAssistantTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="ba-dev-06-ha-")))
        for name in ("state", "credentials", "static"):
            (self.root / name).mkdir(mode=0o700)
        (self.root / "static/index.html").write_text("synthetic fixture")
        self.runtime = Runtime()
        self.runtime.running = True
        self.store = CredentialStore(self.root / "credentials")
        self.storage = DeviceStorage(
            self.root / "state", self.store, cleanup_private=self.runtime.clear_private
        )
        self.registration = self.storage.register(str(uuid4()), str(uuid4()))
        self.config = {
            "origin": "http://127.0.0.1:8123",
            "token": SECRET,
            "integration_id": str(uuid4()),
            "allowed_entity_ids": list(states()),
        }
        configure(self.storage, self.store, self.config)
        self.fake = FakeHA()
        self.adapter = HomeAssistant(
            self.runtime, self.storage, self.store, transport=httpx.MockTransport(self.fake)
        )
        self.addAsyncCleanup(self.close)

    async def close(self) -> None:
        await self.adapter.close()
        await self.runtime.close()
        self.storage.close()
        self.store.close()

    async def test_exact_ids_units_capabilities_and_no_secret_inventory(self) -> None:
        items = await self.adapter.inventory()
        self.assertEqual(len(items), 3)
        self.assertEqual(
            {i.external_id for i in items if i.label == "照明"}, {"light.first", "light.second"}
        )
        self.assertNotIn(SECRET, repr(items))
        climate = next(i for i in items if i.domain == "climate")
        self.assertIn("climate.set_temperature", climate.supported_actions)
        self.fake.unit = "°F"
        climate = next(i for i in await self.adapter.inventory() if i.domain == "climate")
        self.assertEqual(climate.supported_actions, ("climate.set_hvac_mode",))
        result = await self.adapter.set_state(
            "climate.room",
            {"action": "climate.set_temperature", "parameters": {"temperature_c": 21}},
            authorize=lambda: None,
        )
        self.assertEqual(result.code, "home_operation_unsupported")
        self.assertFalse(any(method == "POST" for method, _, _ in self.fake.calls))

    async def test_accepted_is_not_observed_or_physical_success_and_post_never_retries(
        self,
    ) -> None:
        result = await self.adapter.set_state(
            "light.first",
            {"action": "light.turn_on", "parameters": {"brightness_pct": 40}},
            authorize=lambda: None,
        )
        self.assertEqual(result.status, "accepted")
        self.assertIsNone(result.observed_state)
        observed = await self.adapter.observe("light.first")
        assert observed.observed_state is not None
        self.assertEqual(observed.observed_state.power, "off")
        self.fake.fail = "response_loss"
        result = await self.adapter.set_state(
            "light.first", {"action": "light.turn_off", "parameters": {}}, authorize=lambda: None
        )
        self.assertEqual(result.status, "outcome_unknown")
        self.assertEqual(sum(m == "POST" for m, _, _ in self.fake.calls), 2)
        self.assertNotIn(SECRET, repr(result))

    async def test_t018_invalid_input_has_no_network_side_effect(self) -> None:
        for external, op in [
            ("light.unknown", {"action": "light.turn_on", "parameters": {}}),
            ("light.first", {"action": "lock.unlock", "parameters": {}}),
            (
                "light.first",
                {"action": "light.turn_on", "parameters": {"entity_id": "light.second"}},
            ),
            ("light.first", {"action": "light.turn_on", "parameters": {"brightness_pct": True}}),
            ("light.first", {"action": "light.turn_off", "parameters": {"brightness_pct": 50}}),
            (
                "climate.room",
                {"action": "climate.set_temperature", "parameters": {"temperature_c": 31}},
            ),
            (
                "climate.room",
                {"action": "climate.set_hvac_mode", "parameters": {"hvac_mode": "dry"}},
            ),
            (
                "light.first",
                {"action": "light.turn_off", "parameters": {}, "url": "https://bad.invalid"},
            ),
        ]:
            with self.subTest(op=op):
                self.assertEqual(
                    (await self.adapter.set_state(external, op, authorize=lambda: None)).status,
                    "failed",
                )
        self.assertEqual(self.fake.calls, [])

    async def test_state_capability_shrink_and_hardware_ranges(self) -> None:
        for external, operation in [
            ("light.second", {"action": "light.turn_on", "parameters": {"brightness_pct": 40}}),
            (
                "climate.room",
                {"action": "climate.set_temperature", "parameters": {"temperature_c": 16}},
            ),
            (
                "climate.room",
                {"action": "climate.set_hvac_mode", "parameters": {"hvac_mode": "auto"}},
            ),
        ]:
            self.assertEqual(
                (await self.adapter.set_state(external, operation, authorize=lambda: None)).status,
                "failed",
            )
        self.fake.states["light.first"]["state"] = "unavailable"
        self.assertEqual((await self.adapter.observe("light.first")).code, "home_state_unavailable")
        del self.fake.states["light.second"]
        self.assertEqual((await self.adapter.observe("light.second")).code, "home_not_found")
        self.assertFalse(any(method == "POST" for method, _, _ in self.fake.calls))

    async def test_http_redirect_malformed_bounded_timeout_and_fixed_origin(self) -> None:
        for failure in ("redirect", "malformed", "oversize", "timeout"):
            self.fake.fail = failure
            result = await self.adapter.observe("light.first")
            self.assertEqual(result.status, "failed")
            self.assertNotIn(SECRET, repr(result))
        self.assertEqual(len(self.fake.calls), 4)
        for origin in (
            "https://u:p@example.invalid",
            "http://public.invalid",
            "http://169.254.169.254",
            "https://example.invalid/path",
            "https://example.invalid?token=x",
            "https://example.invalid/#x",
        ):
            with self.assertRaises(ValueError) as error:
                Configuration.model_validate({**self.config, "origin": origin})
            self.assertNotIn(SECRET, str(error.exception))
        self.assertNotIn(SECRET, repr(Configuration.model_validate(self.config)))

    async def test_callback_and_inflight_reset_fence(self) -> None:
        def revoke() -> None:
            self.runtime.fence_operations()

        result = await self.adapter.set_state(
            "light.first", {"action": "light.turn_off", "parameters": {}}, authorize=revoke
        )
        self.assertEqual(result.status, "failed")
        self.assertFalse(any(method == "POST" for method, _, _ in self.fake.calls))

    async def test_response_after_reset_cannot_publish_or_rebind_new_registration(self) -> None:
        self.fake.release = asyncio.Event()
        task = asyncio.create_task(self.adapter.observe("light.first"))
        await self.fake.entered.wait()
        self.storage.local_reset()
        self.storage.register(str(uuid4()), str(uuid4()))
        self.fake.release.set()
        self.assertEqual((await task).status, "failed")
        self.assertIsNone(self.adapter.config)
        self.assertEqual(list((self.root / "credentials").iterdir()), [])

    async def test_application_composition_loads_protected_config_and_no_local_execution(
        self,
    ) -> None:
        await self.close()
        settings = Settings(
            "127.0.0.1", 8765, self.root / "static", self.root / "credentials", self.root / "state"
        )
        app = DeviceApplication(settings)
        try:
            assert app.home_assistant is not None
            self.assertEqual(app.home_assistant.status, "configured")
            from starlette.testclient import TestClient

            with (
                patch.object(HomeAssistant, "synchronize", return_value=None),
                TestClient(
                    app.app, base_url=settings.origin, client=("127.0.0.1", 12345)
                ) as client,
            ):
                result = client.post("/local/home-assistant/execute", json={"token": SECRET})
                self.assertNotEqual(result.status_code, 200)
                self.assertNotIn(SECRET, result.text)
        finally:
            app.close()

    async def test_real_loopback_http_state_request_and_owned_shutdown(self) -> None:
        requests = []

        async def respond(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                request = await reader.readuntil(b"\r\n\r\n")
                requests.append(request)
                data = json.dumps(states()["light.first"]).encode()
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "
                    + str(len(data)).encode()
                    + b"\r\n\r\n"
                    + data
                )
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(respond, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            await self.adapter.close()
            configure(
                self.storage,
                self.store,
                {
                    **self.config,
                    "origin": f"http://127.0.0.1:{port}",
                    "integration_id": str(uuid4()),
                },
            )
            self.adapter = HomeAssistant(self.runtime, self.storage, self.store)
            result = await self.adapter.observe("light.first")
            self.assertEqual(result.status, "observed")
            self.assertTrue(requests[0].startswith(b"GET /api/states/light.first HTTP/1.1"))
            self.assertIn(b"Bearer " + SECRET.encode(), requests[0])
            await self.adapter.close()
            assert self.adapter.http is not None
            self.assertTrue(self.adapter.http.is_closed)
        finally:
            server.close()
            await server.wait_closed()

    async def test_malformed_capability_and_huge_temperature_fail_without_post(self) -> None:
        for modes in (None, 1, {"brightness": True}, [123]):
            self.fake.states["light.first"]["attributes"]["supported_color_modes"] = modes
            result = await self.adapter.set_state(
                "light.first",
                {"action": "light.turn_on", "parameters": {"brightness_pct": 50}},
                authorize=lambda: None,
            )
            self.assertEqual(result.code, "home_invalid_response")
        result = await self.adapter.set_state(
            "climate.room",
            {"action": "climate.set_temperature", "parameters": {"temperature_c": 10**400}},
            authorize=lambda: None,
        )
        self.assertEqual(result.code, "home_value_invalid")
        self.assertFalse(any(method == "POST" for method, _, _ in self.fake.calls))
        self.assertFalse(self.runtime.resource_failed)

    async def test_reconfiguration_cannot_rebind_same_cloud_identity_to_new_origin(self) -> None:
        with self.assertRaises(HomeError):
            configure(self.storage, self.store, {**self.config, "origin": "https://other.invalid"})
        assert self.adapter.config is not None
        self.assertEqual(self.adapter.config.origin, self.config["origin"])
        configure(
            self.storage, self.store, {**self.config, "token": "synthetic-rotated-local-token"}
        )

    async def test_domain_state_validation_precedes_every_service_post(self) -> None:
        original = self.fake.states["light.first"]
        malformed = [
            {**original, "state": "garbage"},
            {k: v for k, v in original.items() if k != "state"},
            {k: v for k, v in original.items() if k != "attributes"},
            {**original, "attributes": None},
            {**original, "state": 1},
        ]
        for value in malformed:
            self.fake.states["light.first"] = value
            result = await self.adapter.set_state(
                "light.first", {"action": "light.turn_on", "parameters": {}}, authorize=lambda: None
            )
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.code, "home_invalid_response")
        self.assertFalse(any(method == "POST" for method, _, _ in self.fake.calls))
        self.fake.states["light.first"] = original
        result = await self.adapter.set_state(
            "light.first", {"action": "light.turn_on", "parameters": {}}, authorize=lambda: None
        )
        self.assertEqual(result.status, "accepted")
