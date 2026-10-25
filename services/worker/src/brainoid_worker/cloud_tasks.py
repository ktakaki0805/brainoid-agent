"""Cloud Tasks REST adapter and Google-signed OIDC caller verification.

Credentials and endpoints must be supplied explicitly at composition time. Imports
never discover credentials, open sockets, or choose a cloud project.
"""

import base64
import json
import re
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from brainoid_jobs import Delivery
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.id_token import verify_oauth2_token

from brainoid_worker.dispatcher import QueueUnavailable
from brainoid_worker.entry import CallerDenied


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class CloudTasksQueue:
    def __init__(
        self,
        *,
        queue: str,
        target_url: str,
        service_account_email: str,
        audience: str,
        access_token: Callable[[], str],
    ) -> None:
        if not re.fullmatch(
            r"projects/[a-z0-9-]+/locations/[a-z0-9-]+/queues/[A-Za-z0-9_-]+", queue
        ):
            raise ValueError("Invalid task queue")
        target = urlsplit(target_url)
        if (
            target.scheme != "https"
            or not target.hostname
            or target.username
            or target.password
            or target.fragment
            or not audience
            or not service_account_email.endswith(".gserviceaccount.com")
        ):
            raise ValueError("Invalid task target")
        self.queue = queue
        self.target_url = target_url
        self.service_account_email = service_account_email
        self.audience = audience
        self.access_token = access_token
        self._opener = build_opener(_NoRedirect())

    def enqueue(self, delivery: Delivery) -> None:
        body = {
            "task": {
                "name": f"{self.queue}/tasks/{delivery.task_id}",
                "httpRequest": {
                    "httpMethod": "POST",
                    "url": self.target_url,
                    "headers": {"Content-Type": "application/json"},
                    "body": base64.b64encode(delivery.model_dump_json().encode()).decode(),
                    "oidcToken": {
                        "serviceAccountEmail": self.service_account_email,
                        "audience": self.audience,
                    },
                },
            },
        }
        try:
            request = Request(
                f"https://cloudtasks.googleapis.com/v2/{self.queue}/tasks",
                data=json.dumps(body).encode(),
                method="POST",
                headers={
                    "Authorization": f"Bearer {self.access_token()}",
                    "Content-Type": "application/json",
                },
            )
            with self._opener.open(request, timeout=10) as response:
                if response.status != 200:
                    raise QueueUnavailable()
        except HTTPError as error:
            try:
                if (
                    error.code == 409
                    and json.loads(error.read(8192)).get("error", {}).get("status")
                    == "ALREADY_EXISTS"
                ):
                    return
            except (ValueError, AttributeError):
                pass
            finally:
                error.close()
            raise QueueUnavailable() from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise QueueUnavailable() from None


class GoogleCaller:
    def __init__(self, *, audience: str, service_account_email: str, subject: str) -> None:
        if (
            not audience
            or not subject
            or not service_account_email.endswith(".gserviceaccount.com")
        ):
            raise ValueError("Explicit worker identity required")
        self.audience = audience
        self.email = service_account_email
        self.subject = subject
        self._request = GoogleRequest()

    def verify(self, authorization: str) -> None:
        if not authorization.startswith("Bearer ") or len(authorization) > 16384:
            raise CallerDenied()
        try:
            claims = verify_oauth2_token(  # type: ignore[no-untyped-call]
                authorization.removeprefix("Bearer "),
                self._request,
                audience=self.audience,
            )
        except Exception:
            raise CallerDenied() from None
        if (
            claims.get("iss") not in {"accounts.google.com", "https://accounts.google.com"}
            or claims.get("aud") != self.audience
            or claims.get("sub") != self.subject
            or claims.get("email") != self.email
            or claims.get("email_verified") is not True
        ):
            raise CallerDenied()
