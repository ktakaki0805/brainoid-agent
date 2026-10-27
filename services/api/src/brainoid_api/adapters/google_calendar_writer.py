"""Explicit Google write transport: fixed host, one request, no redirects or retries."""

from urllib.parse import quote

from brainoid_contracts.transactions import Document
from brainoid_memory.calendar_credentials import credential_failure
from brainoid_memory.calendar_writer import ProviderReply
from pydantic import SecretStr
from requests import Session
from requests.adapters import HTTPAdapter


class GoogleCalendarWriter:
    def __init__(self) -> None:
        self.session = Session()
        self.session.trust_env = False
        self.session.mount("https://", HTTPAdapter(max_retries=0))

    def close(self) -> None:
        self.session.close()

    def request(
        self,
        token: SecretStr,
        method: str,
        calendar: str,
        event: str | None,
        *,
        etag: str | None = None,
        body: Document | None = None,
    ) -> ProviderReply:
        url = (
            "https://www.googleapis.com/calendar/v3/calendars/"
            + quote(calendar, safe="")
            + "/events"
        )
        if event is not None:
            url += "/" + quote(event, safe="")
        headers = {"Authorization": "Bearer " + token.get_secret_value()}
        if etag is not None:
            headers["If-Match"] = etag
        try:
            response = self.session.request(
                method,
                url,
                headers=headers,
                params={"timeZone": "UTC"} if method == "GET" else {"sendUpdates": "none"},
                json=body,
                timeout=10,
                allow_redirects=False,
            )
            if response.status_code == 204:
                return ProviderReply(204)
            raw = None
            if response.status_code == 200:
                raw = response.json()
            elif response.status_code == 403:
                try:
                    raw = response.json()
                except ValueError:
                    pass  # A malformed denial is still a known rejection, not a timeout.
            return ProviderReply(
                response.status_code,
                raw if response.status_code == 200 and isinstance(raw, dict) else None,
                credential_failure(response.status_code, raw),
            )
        except Exception:
            # No token refresh or retry: the mutation may already have completed.
            return ProviderReply(0)

    def inspect(self, token: SecretStr, calendar: str, event: str) -> ProviderReply:
        return self.request(token, "GET", calendar, event)

    def write(
        self,
        token: SecretStr,
        action: str,
        calendar: str,
        event: str,
        etag: str | None,
        body: Document | None,
    ) -> ProviderReply:
        return self.request(
            token,
            {"create": "POST", "update": "PATCH", "delete": "DELETE"}[action],
            calendar,
            None if action == "create" else event,
            etag=etag,
            body=body,
        )
