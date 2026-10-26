"""Local-only emulator lifecycle and deterministic synthetic fixture seeding."""

import argparse
import http.client
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from brainoid_api.settings import ConfigurationError, Settings, load_settings

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = ROOT / "tests/fixtures/tenants/demo.json"
PROJECT = "demo-brainoid"
DATABASE = "(default)"
HOSTS = {"firestore": "127.0.0.1:8080", "auth": "127.0.0.1:9099"}


class DemoError(RuntimeError):
    """Safe diagnostics: no supplied settings, response bodies or credentials."""


def validate_environment(environ: Mapping[str, str]) -> Settings:
    settings = load_settings(environ)
    if (
        settings.app_env != "local-emulator"
        or settings.gcp_project_id != PROJECT
        or settings.firestore_database_id != DATABASE
        or settings.model_id != "fake-model"
        or settings.firestore_emulator_host != HOSTS["firestore"]
        or settings.firebase_auth_emulator_host != HOSTS["auth"]
    ):
        raise DemoError("Demo tasks require the dedicated .env.emulator.example configuration")
    # Future external adapters must stay fake, even before their settings exist in the API.
    for name in (
        "SPEECH_PROVIDER",
        "STT_PROVIDER",
        "TTS_PROVIDER",
        "TOOL_PROVIDER",
        "CALENDAR_PROVIDER",
        "HOME_ASSISTANT_PROVIDER",
    ):
        if name in environ and environ[name] != "fake":
            raise DemoError("Demo tasks require fake external providers")
    return settings


def load_fixture() -> dict[str, Any]:
    with FIXTURE_PATH.open(encoding="utf-8") as stream:
        fixture: dict[str, Any] = json.load(stream)
    return fixture


def firestore_value(value: Any, field: str = "") -> dict[str, Any]:
    if value is None:
        return {"nullValue": None}
    if isinstance(value, bool):
        return {"booleanValue": value}
    if isinstance(value, int):
        return {"integerValue": str(value)}
    if isinstance(value, str):
        return {"timestampValue" if field.endswith("_at") else "stringValue": value}
    if isinstance(value, list):
        return {"arrayValue": {"values": [firestore_value(item) for item in value]}}
    if isinstance(value, dict):
        return {"mapValue": {"fields": {k: firestore_value(v, k) for k, v in value.items()}}}
    raise DemoError("Unsupported fixture value")


class DemoEmulator:
    def __init__(self, environ: Mapping[str, str]) -> None:
        # This precedes even the first socket/read and is shared by seed and reset.
        validate_environment(environ)
        self.document_prefix = f"projects/{PROJECT}/databases/{DATABASE}/documents"
        self.auth_prefix = f"/identitytoolkit.googleapis.com/v1/projects/{PROJECT}"

    def request(
        self, service: str, method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        # Direct numeric loopback sockets: no SDK/ADC, DNS, proxy, redirects or cloud fallback.
        connection = http.client.HTTPConnection(HOSTS[service], timeout=10)
        try:
            connection.request(
                method,
                path,
                body=json.dumps(body) if body is not None else None,
                headers={"Content-Type": "application/json", "Authorization": "Bearer owner"},
            )
            response = connection.getresponse()
            if not 200 <= response.status < 300:
                raise DemoError(f"{service} emulator request failed (HTTP {response.status})")
            result: dict[str, Any] = json.loads(response.read())
            return result
        except (OSError, http.client.HTTPException, ValueError):
            raise DemoError(
                f"{service} emulator unavailable or returned an invalid response"
            ) from None
        finally:
            connection.close()

    def users(self) -> list[dict[str, Any]]:
        result = self.request(
            "auth", "GET", self.auth_prefix + "/accounts:batchGet?maxResults=1000"
        )
        if result.get("nextPageToken"):
            raise DemoError("Unexpected number of local Auth accounts; reset the demo project")
        users: list[dict[str, Any]] = result.get("users", [])
        return users

    def seed(self) -> None:
        fixture = load_fixture()
        existing = {user["localId"]: user for user in self.users()}
        users = [
            user
            for user in fixture["auth_users"]
            if any(existing.get(user["localId"], {}).get(k) != v for k, v in user.items())
        ]
        if users:
            result = self.request(
                "auth",
                "POST",
                self.auth_prefix + "/accounts:batchCreate",
                {"users": users, "allowOverwrite": True},
            )
            if result.get("error"):
                raise DemoError("Auth fixture import failed; reset and retry")
        writes = [
            {
                "update": {
                    "name": self.document_prefix + "/" + document["path"],
                    **firestore_value(document["fields"])["mapValue"],
                }
            }
            for document in fixture["documents"]
        ]
        self.request(
            "firestore", "POST", "/v1/" + self.document_prefix + ":commit", {"writes": writes}
        )

    def reset(self) -> None:
        # Clears ALL local demo data, including subcollections and non-fixture test records.
        self.request("firestore", "DELETE", "/emulator/v1/" + self.document_prefix)
        self.request("auth", "DELETE", f"/emulator/v1/projects/{PROJECT}/accounts")


def firebase_command(mode: str) -> list[str]:
    command = [
        "pnpm",
        "exec",
        "firebase",
        "emulators:" + mode,
        "--only",
        "auth,firestore",
        "--project",
        PROJECT,
        "--config",
        str(ROOT / "firebase.json"),
        "--non-interactive",
    ]
    if mode == "exec":
        command.append(
            "PYTHONPATH=tests uv run --all-packages --locked --offline python -B "
            "-m unittest discover -s tests/emulator -p 'test_*.py'"
        )
    return command


def main(argv: list[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("start", "test", "seed", "reset"))
    args = parser.parse_args(argv)
    environment = dict(os.environ if environ is None else environ)
    try:
        emulator = DemoEmulator(environment)
        if args.command in ("start", "test"):
            os.chdir(ROOT)
            environment["FIREBASE_CLI_DISABLE_MOTD"] = "true"
            environment["CI"] = "true"
            command = firebase_command("start" if args.command == "start" else "exec")
            # Replace the wrapper so terminal signals reach the CLI lifecycle manager.
            os.execvpe(command[0], command, environment)
        if args.command == "seed":
            emulator.seed()
        elif args.command == "reset":
            emulator.reset()
        print(f"demo_{args.command}_complete")
        return 0
    except (ConfigurationError, DemoError) as error:
        print(str(error), file=sys.stderr)
        return 1
    except OSError:
        print("Demo tool could not start; run mise run setup first", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
