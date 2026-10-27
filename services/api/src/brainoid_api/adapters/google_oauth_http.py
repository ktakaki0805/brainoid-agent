"""Explicit Google HTTP transports: no retries, redirects or credential discovery."""

import base64
import re
from collections.abc import Mapping
from dataclasses import dataclass
from threading import Lock
from typing import Any

from brainoid_memory.calendar_oauth import SecretUnavailable
from google.auth.credentials import Credentials, with_scopes_if_required
from google.auth.exceptions import RefreshError
from requests import Session
from requests.adapters import HTTPAdapter


class OAuthHTTP:
    def __init__(self) -> None:
        self.session = Session()
        self.session.mount("https://", HTTPAdapter(max_retries=0))
        self.session.trust_env = False

    def close(self) -> None:
        self.session.close()

    def __call__(self, url: str, form: dict[str, str]) -> dict[str, object]:
        if url != "https://oauth2.googleapis.com/token":
            raise SecretUnavailable()
        try:
            response = self.session.post(url, data=form, timeout=10, allow_redirects=False)
            if response.status_code != 200:
                raise SecretUnavailable()
            payload = response.json()
            if not isinstance(payload, dict):
                raise SecretUnavailable()
            return payload
        except Exception:
            raise SecretUnavailable() from None


@dataclass(frozen=True)
class _AuthResponse:
    status: int
    data: bytes
    headers: Mapping[str, str]


class _SingleAuthRequest:
    """One synchronous refresh budget, private to one Secret Manager request."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.lock = Lock()
        self.used = False

    def __call__(
        self,
        url: str,
        method: str = "GET",
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        **kwargs: Any,
    ) -> _AuthResponse:
        with self.lock:
            if self.used:
                raise RefreshError("Calendar credential refresh unavailable")  # type: ignore[no-untyped-call]
            self.used = True
        try:
            # Do not delegate to google.auth's Request: it can log credential
            # bodies, and its defaults permit redirects. Ignore SDK timeout and
            # redirect overrides; this boundary owns both policies.
            response = self.session.request(
                method, url, data=body, headers=headers, timeout=10, allow_redirects=False
            )
            # Raising before returning an error response prevents jwt_grant's
            # internal retry/backoff. The consumed budget also rejects a second
            # call if another credential implementation catches this exception.
            if response.status_code != 200:
                raise RefreshError("Calendar credential refresh unavailable")  # type: ignore[no-untyped-call]
            return _AuthResponse(response.status_code, response.content, response.headers)
        except Exception:
            raise RefreshError("Calendar credential refresh unavailable") from None  # type: ignore[no-untyped-call]


class SecretManagerHTTP:
    def __init__(self, *, credentials: Credentials, replication: dict[str, Any]) -> None:
        if set(replication) not in ({"automatic"}, {"userManaged"}):
            raise ValueError("Explicit Secret Manager replication policy required")
        self.replication = replication
        self.credentials = with_scopes_if_required(
            credentials, ["https://www.googleapis.com/auth/cloud-platform"]
        )  # type: ignore[no-untyped-call]
        self.auth_session = Session()
        self.auth_session.mount("https://", HTTPAdapter(max_retries=0))
        self.auth_session.trust_env = False
        self.session = Session()
        self.session.mount("https://", HTTPAdapter(max_retries=0))
        self.session.trust_env = False

    def close(self) -> None:
        self.session.close()
        self.auth_session.close()

    def request(
        self,
        method: str,
        name: str,
        *,
        body: dict[str, Any] | None = None,
        conflict_ok: bool = False,
    ) -> dict[str, Any]:
        try:
            # Synchronous public refresh/apply avoids background refresh and
            # implicit 401 refresh/replay. Never share/reset an attempt counter
            # across concurrent operations. Multi-request credential refreshes
            # fail closed at their second transport call.
            if not self.credentials.valid:
                self.credentials.refresh(_SingleAuthRequest(self.auth_session))
            headers: dict[str, str] = {}
            self.credentials.apply(headers)
            response = self.session.request(
                method,
                "https://secretmanager.googleapis.com/v1/" + name,
                headers=headers,
                json=body,
                timeout=10,
                allow_redirects=False,
            )
            if conflict_ok and response.status_code == 409:
                return {}
            if response.status_code != 200:
                raise SecretUnavailable()
            payload = response.json()
            if not isinstance(payload, dict):
                raise SecretUnavailable()
            return payload
        except Exception:
            raise SecretUnavailable() from None

    @staticmethod
    def valid(name: str, *, version: bool = False) -> None:
        suffix = "/versions/1" if version else ""
        if not re.fullmatch(
            r"projects/(?:[0-9]{6,30}|[a-z][a-z0-9-]{4,61}[a-z0-9])/secrets/calendar-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
            + suffix,
            name,
        ):
            raise SecretUnavailable()

    def create(self, name: str) -> None:
        self.valid(name)
        parent, identity = name.rsplit("/", 1)
        self.request(
            "POST",
            parent + "?secretId=" + identity,
            body={"replication": self.replication},
            conflict_ok=True,
        )

    def add(self, name: str, payload: bytes) -> str:
        self.valid(name)
        result = self.request(
            "POST",
            name + ":addVersion",
            body={"payload": {"data": base64.b64encode(payload).decode()}},
        )
        reference = result.get("name")
        if reference != name + "/versions/1":
            raise SecretUnavailable()
        return str(reference)

    def access(self, reference: str) -> bytes:
        self.valid(reference, version=True)
        result = self.request("GET", reference + ":access")
        try:
            if result.get("name") != reference:
                raise SecretUnavailable()
            return base64.b64decode(result["payload"]["data"], validate=True)
        except Exception:
            raise SecretUnavailable() from None

    def destroy(self, reference: str) -> None:
        self.valid(reference, version=True)
        # GET makes retrying an already destroyed version explicitly idempotent.
        metadata = self.request("GET", reference)
        if metadata.get("name") != reference:
            raise SecretUnavailable()
        if metadata.get("state") == "DESTROYED":
            return
        result = self.request("POST", reference + ":destroy", body={})
        if result.get("name") != reference or result.get("state") != "DESTROYED":
            raise SecretUnavailable()
