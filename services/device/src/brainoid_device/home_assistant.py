"""Protected, fixed-origin HA primitives. Only DEV07 may connect these to commands."""

import asyncio
import ipaddress
import json
import logging
import math
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal, Self
from urllib.parse import urlsplit

import httpx
from brainoid_contracts import ResourceId
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from brainoid_device.cloud_client import CloudClient
from brainoid_device.cloud_models import CloudError
from brainoid_device.ledger import DeviceStorage
from brainoid_device.runtime import Runtime
from brainoid_device.storage import CredentialStore

CONFIG_NAME = "home-assistant-v1"
MAX_BYTES = 1024 * 1024
ExternalId = Annotated[str, Field(pattern=r"^(light|climate)\.[a-z0-9_]+$", max_length=255)]
Action = Literal[
    "light.turn_on", "light.turn_off", "climate.set_temperature", "climate.set_hvac_mode"
]


class Value(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, frozen=True)


class Configuration(Value):
    origin: Annotated[str, Field(max_length=512)] = Field(repr=False)
    token: SecretStr = Field(repr=False)
    integration_id: ResourceId
    allowed_entity_ids: Annotated[tuple[ExternalId, ...], Field(max_length=100)]

    @model_validator(mode="after")
    def valid(self) -> Self:
        parsed = urlsplit(self.origin)
        # HA commonly runs HTTP on a trusted LAN. Cleartext is restricted to
        # literal RFC1918/ULA/loopback addresses, never DNS or public addresses.
        cleartext = False
        if parsed.hostname:
            try:
                address = ipaddress.ip_address(parsed.hostname)
                cleartext = address.is_loopback or any(
                    address in ipaddress.ip_network(network)
                    for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
                    if address.version == ipaddress.ip_network(network).version
                )
            except ValueError:
                pass
        if (
            not parsed.hostname
            or parsed.scheme not in {"https", "http"}
            or (parsed.scheme == "http" and not cleartext)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or any(c.isspace() or c in "\\%" for c in self.origin)
            or (parsed.port is not None and not 1 <= parsed.port <= 65535)
            or not 16 <= len(self.token.get_secret_value()) <= 16384
            or any(not "!" <= c <= "~" for c in self.token.get_secret_value())
            or len(set(self.allowed_entity_ids)) != len(self.allowed_entity_ids)
        ):
            raise ValueError("invalid_home_configuration")
        return self


class SavedConfiguration(Configuration):
    epoch: ResourceId


class InventoryItem(Value):
    external_id: ExternalId
    label: Annotated[str, Field(min_length=1, max_length=200)]
    domain: Literal["light", "climate"]
    supported_actions: tuple[Action, ...]


class Observation(Value):
    power: Literal["on", "off"] | None = None
    brightness_pct: int | None = None
    temperature_c: float | None = None
    hvac_mode: Literal["off", "heat", "cool", "auto"] | None = None


class Result(Value):
    status: Literal["accepted", "observed", "failed", "outcome_unknown"]
    code: str | None = None
    observed_state: Observation | None = None


class HomeError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def export_inventory(
    config: SavedConfiguration, items: tuple[InventoryItem, ...]
) -> dict[str, Any]:
    """Final cloud projection: even reflected provider text cannot export secrets."""
    protected = (config.token.get_secret_value(), config.origin)
    entities = []
    for item in items:
        value = item.model_dump(mode="json")
        if any(secret in value["label"] for secret in protected):
            value["label"] = item.external_id
        entities.append(value)
    body = {"entities": entities}

    def check(value: Any) -> None:
        if isinstance(value, str) and any(secret in value for secret in protected):
            raise HomeError("home_export_refused")
        if isinstance(value, dict):
            for key, child in value.items():
                check(key)
                check(child)
        elif isinstance(value, list):
            for child in value:
                check(child)

    check(body)
    return body


def configure(storage: DeviceStorage, store: CredentialStore, value: object) -> None:
    """Operator-only provisioning while daemon stopped; never callable from HTTP/chat."""
    try:
        config = Configuration.model_validate(value)
        registration = storage.snapshot().registration
        if registration is None:
            raise ValueError
        if storage.credential_reference(registration.epoch, "home_assistant") is not None:
            previous = SavedConfiguration.model_validate_json(store.read(CONFIG_NAME))
            if previous.epoch != registration.epoch or (
                previous.integration_id == config.integration_id
                and previous.origin != config.origin
            ):
                # A new HA server requires a new cloud identity namespace.
                raise ValueError
        saved = SavedConfiguration(**config.model_dump(), epoch=ResourceId(registration.epoch))
        data = saved.model_dump(mode="json")
        data["token"] = config.token.get_secret_value()
        store.write(CONFIG_NAME, json.dumps(data).encode())
        storage.save_credential_reference(registration.epoch, "home_assistant", CONFIG_NAME)
    except (ValueError, OSError, RuntimeError):
        raise HomeError("home_configuration_refused") from None


def number(value: object) -> float | None:
    if type(value) not in {int, float}:
        return None
    try:
        result = float(value)  # type: ignore[arg-type]
        return result if math.isfinite(result) else None
    except (ValueError, OverflowError):
        return None


class HomeAssistant:
    def __init__(
        self,
        runtime: Runtime,
        storage: DeviceStorage,
        store: CredentialStore,
        *,
        cloud: CloudClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.runtime, self.storage, self.store, self.cloud = runtime, storage, store, cloud
        self.generation = runtime.generation
        self.config: SavedConfiguration | None = None
        self.status = "disabled"
        self.http: httpx.AsyncClient | None = None
        self.transport = transport
        self.sleep = sleep
        self._lock = asyncio.Lock()
        try:
            registration = storage.snapshot().registration
            if registration and storage.credential_reference(registration.epoch, "home_assistant"):
                config = SavedConfiguration.model_validate_json(store.read(CONFIG_NAME))
                if config.epoch != registration.epoch:
                    raise ValueError
                self.config, self.status = config, "configured"
        except (OSError, ValueError, RuntimeError):
            self.status = "configuration_unavailable"
        runtime.register_private_cleanup(self.clear)

    def clear(self) -> None:
        self.config = None
        self.status = "stopped"

    def check(self) -> SavedConfiguration:
        config = self.config
        registration = self.storage.snapshot().registration
        if (
            config is None
            or not self.runtime.permits_work(self.generation)
            or registration is None
            or registration.epoch != config.epoch
            or self.storage.credential_reference(registration.epoch, "home_assistant")
            != CONFIG_NAME
        ):
            raise HomeError("home_stopped")
        if self.cloud:
            try:
                self.cloud.check()
            except CloudError:
                self.clear()
                raise HomeError("home_stopped") from None
        return config

    async def close(self) -> None:
        self.clear()
        if self.http:
            await self.http.aclose()

    async def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        config = self.check()
        if self.http is None:
            for name in (
                "httpx",
                "httpcore",
                "httpcore.connection",
                "httpcore.http11",
                "httpcore.http2",
                "httpcore.proxy",
            ):
                logging.getLogger(name).disabled = True
            self.http = httpx.AsyncClient(
                trust_env=False,
                follow_redirects=False,
                timeout=5,
                limits=httpx.Limits(max_connections=1),
                transport=self.transport,
            )
        try:
            async with asyncio.timeout(8):
                async with self.http.stream(
                    method,
                    config.origin + path,
                    headers={
                        "Authorization": "Bearer " + config.token.get_secret_value(),
                        "Accept-Encoding": "identity",
                    },
                    json=body,
                ) as response:
                    self.check()
                    if response.status_code != 200:
                        raise HomeError(
                            {
                                401: "home_unauthorized",
                                403: "home_unauthorized",
                                404: "home_not_found",
                                400: "home_rejected",
                            }.get(response.status_code, "home_unavailable")
                        )
                    if response.headers.get("content-encoding", "identity") != "identity":
                        raise HomeError("home_invalid_response")
                    if response.headers.get("content-type", "").split(";")[0] != "application/json":
                        raise HomeError("home_invalid_response")
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(data) + len(chunk) > MAX_BYTES:
                            raise HomeError("home_invalid_response")
                        data.extend(chunk)
                    self.check()
                    return json.loads(data)
        except (httpx.HTTPError, TimeoutError, OSError):
            raise HomeError("home_unavailable") from None
        except (ValueError, RecursionError):
            raise HomeError("home_invalid_response") from None

    def _entity(self, external_id: str) -> None:
        if external_id not in self.check().allowed_entity_ids:
            raise HomeError("home_entity_forbidden")

    @staticmethod
    def _state(value: Any, external_id: str) -> dict[str, Any]:
        if (
            not isinstance(value, dict)
            or value.get("entity_id") != external_id
            or not isinstance(value.get("attributes"), dict)
            or not isinstance(value.get("state"), str)
        ):
            raise HomeError("home_invalid_response")
        for name in ("hvac_modes", "supported_color_modes"):
            modes = value["attributes"].get(name, [])
            if (
                not isinstance(modes, list)
                or len(modes) > 32
                or any(not isinstance(mode, str) for mode in modes)
            ):
                raise HomeError("home_invalid_response")
        return value

    @staticmethod
    def _actions(state: dict[str, Any], celsius: bool = False) -> tuple[Action, ...]:
        if state["entity_id"].startswith("light."):
            return ("light.turn_on", "light.turn_off")
        attrs = state["attributes"]
        actions: list[Action] = []
        features = attrs.get("supported_features")
        if type(features) is int and features >= 0 and features & 1 and celsius:
            actions.append("climate.set_temperature")
        modes = attrs.get("hvac_modes")
        if isinstance(modes, list) and any(
            mode in ("off", "heat", "cool", "auto") for mode in modes
        ):
            actions.append("climate.set_hvac_mode")
        return tuple(actions)

    async def _celsius(self) -> bool:
        value = await self._request("GET", "/api/config")
        if not isinstance(value, dict) or not isinstance(value.get("unit_system"), dict):
            raise HomeError("home_invalid_response")
        unit = value["unit_system"].get("temperature")
        if unit not in ("°C", "°F"):
            raise HomeError("home_invalid_response")
        return bool(unit == "°C")

    async def inventory(self) -> tuple[InventoryItem, ...]:
        async with self._lock:
            config = self.check()
            value = await self._request("GET", "/api/states")
            if not isinstance(value, list) or len(value) > 2000:
                raise HomeError("home_invalid_response")
            celsius = (
                await self._celsius()
                if any(e.startswith("climate.") for e in config.allowed_entity_ids)
                else False
            )
            found: dict[str, InventoryItem] = {}
            for item in value:
                if not isinstance(item, dict):
                    raise HomeError("home_invalid_response")
                external_id = item.get("entity_id")
                if external_id not in config.allowed_entity_ids:
                    continue
                state = self._state(item, external_id)
                if external_id in found:
                    raise HomeError("home_invalid_response")
                actions = self._actions(state, celsius)
                if not actions:
                    continue
                try:
                    found[external_id] = InventoryItem(
                        external_id=external_id,
                        label=state["attributes"].get("friendly_name", external_id),
                        domain=external_id.split(".")[0],
                        supported_actions=actions,
                    )
                except ValueError:
                    raise HomeError("home_invalid_response") from None
            self.check()
            return tuple(found[key] for key in sorted(found))

    async def _observe(self, external_id: str) -> Observation:
        self._entity(external_id)
        state = self._state(await self._request("GET", "/api/states/" + external_id), external_id)
        if state["state"] in {"unknown", "unavailable"}:
            raise HomeError("home_state_unavailable")
        attrs, current = state["attributes"], state["state"]
        if external_id.startswith("light."):
            if current not in {"on", "off"}:
                raise HomeError("home_invalid_response")
            brightness = attrs.get("brightness")
            if brightness is not None and (
                type(brightness) is not int or not 0 <= brightness <= 255
            ):
                raise HomeError("home_invalid_response")
            return Observation(
                power=current,
                brightness_pct=round(brightness * 100 / 255) if brightness is not None else None,
            )
        if current not in {"off", "heat", "cool", "auto"}:
            raise HomeError("home_state_unsupported")
        celsius = await self._celsius()
        temperature = number(attrs.get("temperature")) if celsius else None
        if celsius and attrs.get("temperature") is not None and temperature is None:
            raise HomeError("home_invalid_response")
        return Observation(hvac_mode=current, temperature_c=temperature)

    async def observe(self, external_id: str) -> Result:
        try:
            async with self._lock:
                observed = await self._observe(external_id)
                self.check()
                return Result(status="observed", observed_state=observed)
        except HomeError as error:
            return Result(status="failed", code=error.code)

    async def set_state(
        self, external_id: str, operation: object, *, authorize: Callable[[], None]
    ) -> Result:
        """Internal primitive; DEV07 must supply its live grant fence before send.

        Never retries a POST. Accepted is not observed success; observe separately.
        """
        sent = False
        try:
            async with self._lock:
                self._entity(external_id)
                if not isinstance(operation, dict) or set(operation) != {"action", "parameters"}:
                    raise HomeError("home_operation_forbidden")
                action, params = operation["action"], operation["parameters"]
                if not isinstance(action, str) or not isinstance(params, dict):
                    raise HomeError("home_operation_forbidden")
                allowed = {
                    "light.turn_on": {"brightness_pct"},
                    "light.turn_off": set(),
                    "climate.set_temperature": {"temperature_c"},
                    "climate.set_hvac_mode": {"hvac_mode"},
                }
                if (
                    action not in allowed
                    or not action.startswith(external_id.split(".")[0] + ".")
                    or set(params) - allowed[action]
                ):
                    raise HomeError("home_operation_forbidden")
                if (
                    action == "light.turn_on"
                    and params
                    and (
                        type(params["brightness_pct"]) is not int
                        or not 1 <= params["brightness_pct"] <= 100
                    )
                ):
                    raise HomeError("home_value_invalid")
                if action == "climate.set_temperature" and (
                    set(params) != {"temperature_c"}
                    or number(params["temperature_c"]) is None
                    or not 16 <= params["temperature_c"] <= 30
                ):
                    raise HomeError("home_value_invalid")
                if action == "climate.set_hvac_mode" and (
                    set(params) != {"hvac_mode"}
                    or params["hvac_mode"] not in ("off", "heat", "cool", "auto")
                ):
                    raise HomeError("home_value_invalid")
                state = self._state(
                    await self._request("GET", "/api/states/" + external_id), external_id
                )
                if state["state"] in {"unknown", "unavailable"}:
                    raise HomeError("home_state_unavailable")
                valid_states = (
                    ("on", "off")
                    if external_id.startswith("light.")
                    else ("off", "heat", "cool", "auto", "heat_cool", "dry", "fan_only")
                )
                if state["state"] not in valid_states:
                    raise HomeError("home_invalid_response")
                attrs = state["attributes"]
                celsius = await self._celsius() if external_id.startswith("climate.") else False
                if action not in self._actions(state, celsius):
                    raise HomeError("home_operation_unsupported")
                if "brightness_pct" in params and not any(
                    mode
                    in ("brightness", "color_temp", "hs", "xy", "rgb", "rgbw", "rgbww", "white")
                    for mode in attrs.get("supported_color_modes", [])
                    if isinstance(mode, str)
                ):
                    raise HomeError("home_operation_unsupported")
                if "temperature_c" in params:
                    low, high = number(attrs.get("min_temp")), number(attrs.get("max_temp"))
                    if low is None or high is None or not low <= params["temperature_c"] <= high:
                        raise HomeError("home_value_invalid")
                if "hvac_mode" in params and params["hvac_mode"] not in attrs.get("hvac_modes", []):
                    raise HomeError("home_operation_unsupported")
                self.check()
                authorize()
                self.check()
                body = {"entity_id": external_id, **params}
                if "temperature_c" in body:
                    body["temperature"] = body.pop("temperature_c")
                sent = True
                response = await self._request(
                    "POST", "/api/services/" + action.replace(".", "/"), body
                )
                if not isinstance(response, list):
                    raise HomeError("home_invalid_response")
                self.check()
                return Result(status="accepted")
        except HomeError as error:
            return Result(status="outcome_unknown" if sent else "failed", code=error.code)

    async def synchronize(self) -> None:
        try:
            await self._synchronize()
        finally:
            await self.close()

    async def _synchronize(self) -> None:
        """Periodic metadata discovery. No command execution or permission expansion."""
        while self.config is not None:
            try:
                config = self.check()
                inventory = await self.inventory()
                if self.cloud:
                    credentials = self.cloud.journal.credentials
                    if credentials is not None:
                        from uuid import uuid4

                        self.check()
                        body = export_inventory(config, inventory)
                        status, _ = await self.cloud.authorized(
                            "POST",
                            f"/v1/tenants/{credentials.tenant_id}/integrations/{config.integration_id}/home-assistant/entities:sync",
                            body=body,
                            headers={"Idempotency-Key": str(uuid4())},
                        )
                        self.check()
                        self.status = "synchronized" if status == 200 else "sync_refused"
                        if status in {401, 410}:
                            self.clear()
                            return
            except (HomeError, CloudError):
                if self.config is None:
                    return
                self.status = "unavailable"
            await self.sleep(60)
