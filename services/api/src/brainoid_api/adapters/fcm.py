"""FCM HTTP v1, one explicit POST; importing never discovers credentials or connects."""

import re
from typing import Any, Literal

from google.auth.credentials import Credentials, with_scopes_if_required
from google.auth.transport.requests import AuthorizedSession
from requests.adapters import HTTPAdapter

GENERIC_BODY = "新しい通知があります。アプリで確認してください。"


class FCM:
    def __init__(self, *, project: str, credentials: Credentials) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9-]{4,61}[a-z0-9]", project):
            raise ValueError("Invalid explicit FCM project")
        self.url = f"https://fcm.googleapis.com/v1/projects/{project}/messages:send"
        scoped = with_scopes_if_required(  # type: ignore[no-untyped-call]
            credentials, ["https://www.googleapis.com/auth/firebase.messaging"]
        )
        self.session = AuthorizedSession(scoped, max_refresh_attempts=0)  # type: ignore[no-untyped-call]
        # Firebase Admin's default POST retries cannot be used with an unknown outcome.
        self.session.mount("https://", HTTPAdapter(max_retries=0))
        self.session.trust_env = False

    def close(self) -> None:
        self.session.close()  # type: ignore[no-untyped-call]

    def send(
        self, token: str, data: dict[str, str]
    ) -> Literal["accepted", "invalid", "rejected", "unknown"]:
        from brainoid_contracts import ResourceId
        from pydantic import TypeAdapter

        if set(data) != {"notification_id", "tenant_id", "body"} or data["body"] != GENERIC_BODY:
            raise ValueError("Invalid push payload")
        for field in ("notification_id", "tenant_id"):
            TypeAdapter(ResourceId).validate_python(data[field])
        message: dict[str, Any] = {
            "token": token,
            "data": data,
            "notification": {"body": GENERIC_BODY},
        }
        try:
            response = self.session.post(
                self.url, json={"message": message}, timeout=10, allow_redirects=False
            )
            payload = response.json()
            if (
                response.status_code == 200
                and isinstance(payload, dict)
                and isinstance(payload.get("name"), str)
            ):
                return "accepted"
            if response.status_code == 404 and isinstance(payload, dict):
                error = payload.get("error")
                details = error.get("details", []) if isinstance(error, dict) else []
                if isinstance(details, list) and any(
                    isinstance(item, dict)
                    and item.get("@type") == "type.googleapis.com/google.firebase.fcm.v1.FcmError"
                    and item.get("errorCode") == "UNREGISTERED"
                    for item in details
                ):
                    return "invalid"
            # Explicit client rejection is not a permanent token verdict. Transient
            # failures/redirects/malformed response may conceal acceptance: hold unknown.
            return "rejected" if response.status_code in {400, 401, 403, 404, 429} else "unknown"
        except Exception:
            return "unknown"
