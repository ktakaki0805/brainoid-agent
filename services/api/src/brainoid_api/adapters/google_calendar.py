"""Fixed Google Calendar events transport with an explicitly supplied access token."""

from urllib.parse import quote

from brainoid_contracts.errors import APIError, ErrorCode
from brainoid_contracts.transactions import Document
from brainoid_memory.calendar_credentials import CalendarCredentialsRejected, credential_failure
from brainoid_memory.calendar_reader import CalendarQuery
from pydantic import SecretStr
from requests import Session
from requests.adapters import HTTPAdapter


class GoogleCalendar:
    def __init__(self) -> None:
        self.session = Session()
        self.session.mount("https://", HTTPAdapter(max_retries=0))
        self.session.trust_env = False

    def close(self) -> None:
        self.session.close()

    def events(self, token: SecretStr, query: CalendarQuery, page_token: str | None) -> Document:
        params: dict[str, str | int] = {
            "timeMin": query.time_min.isoformat(),
            "timeMax": query.time_max.isoformat(),
            "maxResults": query.limit,
            "singleEvents": "true",
            "orderBy": "startTime",
            "showDeleted": "false",
        }
        if page_token is not None:
            params["pageToken"] = page_token
        try:
            response = self.session.get(
                "https://www.googleapis.com/calendar/v3/calendars/"
                + quote(query.calendar_id, safe="")
                + "/events",
                params=params,
                headers={"Authorization": "Bearer " + token.get_secret_value()},
                timeout=10,
                allow_redirects=False,
            )
            reason = credential_failure(
                response.status_code, response.json() if response.status_code == 403 else None
            )
            if reason is not None:
                raise CalendarCredentialsRejected(reason)
            if response.status_code in (403, 404):
                raise APIError(ErrorCode.OPERATION_FORBIDDEN)
            if response.status_code != 200:
                raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError
            return result
        except APIError:
            raise
        except Exception:
            raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE) from None

    def event(self, token: SecretStr, calendar: str, event: str) -> Document:
        try:
            response = self.session.get(
                "https://www.googleapis.com/calendar/v3/calendars/"
                + quote(calendar, safe="")
                + "/events/"
                + quote(event, safe=""),
                params={"timeZone": "UTC"},
                headers={"Authorization": "Bearer " + token.get_secret_value()},
                timeout=10,
                allow_redirects=False,
            )
            reason = credential_failure(
                response.status_code, response.json() if response.status_code == 403 else None
            )
            if reason is not None:
                raise CalendarCredentialsRejected(reason)
            if response.status_code in (403, 404):
                raise APIError(ErrorCode.OPERATION_FORBIDDEN)
            if response.status_code != 200:
                raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError
            return result
        except APIError:
            raise
        except Exception:
            raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE) from None
