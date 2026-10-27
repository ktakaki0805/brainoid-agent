"""Actual HTTP transport configuration is verified without Google network access."""

import base64
import json
import threading
import traceback
import unittest
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, cast
from unittest.mock import Mock, patch

from brainoid_api.adapters.google_oauth_http import OAuthHTTP, SecretManagerHTTP
from brainoid_memory.calendar_oauth import SecretUnavailable
from google.auth import crypt
from google.auth.credentials import AnonymousCredentials, Credentials
from google.auth.exceptions import RefreshError
from google.oauth2 import service_account
from requests import PreparedRequest, Response
from requests.adapters import HTTPAdapter

NAME = "projects/synthetic-project/secrets/calendar-00000000-0000-0000-0000-000000000001"
TOKEN_URI = "https://oauth2.googleapis.com/token"


class SyntheticSigner(crypt.Signer):
    @property
    def key_id(self) -> str:
        return "synthetic-key"

    def sign(self, message: bytes) -> bytes:
        return b"SYNTHETIC_SIGNATURE"


def reply(
    request: PreparedRequest,
    status: int,
    body: dict[str, Any],
    headers: dict[str, str] | None = None,
) -> Response:
    response = Response()
    response.status_code = status
    response.url = request.url or ""
    response.request = request
    response._content = json.dumps(body).encode()
    response.headers.update(headers or {})
    return response


class DoubleRefreshCredential(Credentials):
    """Exercise SDK repeated transport calls, including after caught failures."""

    def refresh(self, request: Callable[..., Any]) -> None:
        try:
            request(TOKEN_URI, method="POST", body=b"SYNTHETIC_ASSERTION")
        except RefreshError:
            pass
        request(TOKEN_URI, method="POST", body=b"SYNTHETIC_ASSERTION")


class CalendarCredentialTransportTests(unittest.TestCase):
    def adapter(self, credential: Credentials | None = None) -> SecretManagerHTTP:
        if credential is None:
            credential = service_account.Credentials(  # type: ignore[no-untyped-call]
                SyntheticSigner(),
                "synthetic@synthetic-project.iam.gserviceaccount.com",
                TOKEN_URI,
            )
        adapter = SecretManagerHTTP(credentials=credential, replication={"automatic": {}})
        self.addCleanup(adapter.close)
        return adapter

    @staticmethod
    def token(request: PreparedRequest, **kwargs: Any) -> Response:
        return reply(
            request,
            200,
            {"access_token": "SYNTHETIC_CLOUD_ACCESS", "expires_in": 3600, "token_type": "Bearer"},
        )

    @staticmethod
    def resource(request: PreparedRequest, **kwargs: Any) -> Response:
        return reply(request, 200, {"name": NAME})

    def test_service_account_refresh_then_cached_token_with_bounded_transport(self) -> None:
        adapter = self.adapter()
        with (
            patch.object(
                adapter.auth_session.get_adapter("https://"), "send", side_effect=self.token
            ) as auth,
            patch.object(
                adapter.session.get_adapter("https://"), "send", side_effect=self.resource
            ) as gsm,
            patch.object(
                adapter.auth_session, "request", wraps=adapter.auth_session.request
            ) as auth_call,
            self.assertNoLogs(level="DEBUG"),
        ):
            adapter.create(NAME)
            adapter.create(NAME)
        self.assertEqual(auth.call_count, 1)
        self.assertEqual(gsm.call_count, 2)
        sent = auth.call_args.args[0]
        self.assertEqual(sent.url, TOKEN_URI)
        self.assertEqual(sent.method, "POST")
        self.assertIn(b"assertion=", sent.body)
        self.assertEqual(auth.call_args.kwargs["timeout"], 10)
        self.assertFalse(auth_call.call_args.kwargs["allow_redirects"])
        for call in gsm.call_args_list:
            self.assertEqual(call.args[0].headers["authorization"], "Bearer SYNTHETIC_CLOUD_ACCESS")
            self.assertEqual(call.kwargs["timeout"], 10)

    def test_service_account_redirect_and_retryable_errors_never_reach_second_endpoint(
        self,
    ) -> None:
        for status in (307, 503):
            with self.subTest(status=status):
                adapter = self.adapter()

                def failure(
                    request: PreparedRequest, status: int = status, **kwargs: Any
                ) -> Response:
                    return reply(
                        request,
                        status,
                        {"error": "SYNTHETIC_PRIVATE_ERROR"},
                        {"Location": "https://redirected.example.test/token"},
                    )

                with (
                    patch.object(
                        adapter.auth_session.get_adapter("https://"), "send", side_effect=failure
                    ) as auth,
                    patch.object(adapter.session.get_adapter("https://"), "send") as gsm,
                    patch("google.auth._exponential_backoff.time.sleep") as sleep,
                    self.assertNoLogs(level="DEBUG"),
                ):
                    try:
                        adapter.create(NAME)
                    except SecretUnavailable as error:
                        rendered = "".join(traceback.format_exception(error))
                    else:
                        self.fail("refresh error must close the operation")
                self.assertEqual(auth.call_count, 1)
                self.assertEqual(auth.call_args.args[0].url, TOKEN_URI)
                gsm.assert_not_called()
                sleep.assert_not_called()
                self.assertNotIn("SYNTHETIC_PRIVATE_ERROR", rendered)
                self.assertNotIn("SYNTHETIC_SIGNATURE", rendered)
                self.assertNotIn("assertion=", rendered)
                self.assertNotIn("redirected.example.test", rendered)

    def test_service_account_auth_network_error_is_sanitized_without_retry(self) -> None:
        adapter = self.adapter()
        with (
            patch.object(
                adapter.auth_session.get_adapter("https://"),
                "send",
                side_effect=RuntimeError("SYNTHETIC_ASSERTION private credential"),
            ) as auth,
            patch.object(adapter.session.get_adapter("https://"), "send") as gsm,
            self.assertNoLogs(level="DEBUG"),
        ):
            try:
                adapter.create(NAME)
            except SecretUnavailable as error:
                rendered = "".join(traceback.format_exception(error))
            else:
                self.fail("network error must close the operation")
        self.assertEqual(auth.call_count, 1)
        gsm.assert_not_called()
        self.assertNotIn("SYNTHETIC_ASSERTION", rendered)

    def test_next_operation_can_refresh_after_prior_failure(self) -> None:
        adapter = self.adapter()
        calls = []

        def send(request: PreparedRequest, **kwargs: Any) -> Response:
            calls.append(request)
            return reply(request, 503, {}) if len(calls) == 1 else self.token(request)

        with (
            patch.object(adapter.auth_session.get_adapter("https://"), "send", side_effect=send),
            patch.object(
                adapter.session.get_adapter("https://"), "send", side_effect=self.resource
            ) as gsm,
        ):
            with self.assertRaises(SecretUnavailable):
                adapter.create(NAME)
            adapter.create(NAME)
        self.assertEqual(len(calls), 2)
        self.assertEqual(gsm.call_count, 1)

    def test_concurrent_service_account_refreshes_have_independent_budgets(self) -> None:
        adapter = self.adapter()
        entered = threading.Barrier(2)

        def send(request: PreparedRequest, **kwargs: Any) -> Response:
            entered.wait(timeout=5)
            return self.token(request)

        with (
            patch.object(
                adapter.auth_session.get_adapter("https://"), "send", side_effect=send
            ) as auth,
            patch.object(
                adapter.session.get_adapter("https://"), "send", side_effect=self.resource
            ) as gsm,
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            first = pool.submit(adapter.create, NAME)
            second = pool.submit(adapter.create, NAME)
            first.result(timeout=10)
            second.result(timeout=10)
        self.assertEqual(auth.call_count, 2)
        self.assertEqual(gsm.call_count, 2)

    def test_repeated_sdk_calls_are_blocked_for_each_concurrent_and_new_operation(self) -> None:
        for status in (200, 503):
            with self.subTest(status=status):
                adapter = self.adapter(DoubleRefreshCredential())  # type: ignore[no-untyped-call]
                entered = threading.Barrier(2)

                def send(
                    request: PreparedRequest,
                    status: int = status,
                    entered: threading.Barrier = entered,
                    **kwargs: Any,
                ) -> Response:
                    entered.wait(timeout=5)
                    return reply(request, status, {})

                def create(adapter: SecretManagerHTTP = adapter) -> None:
                    with self.assertRaises(SecretUnavailable):
                        adapter.create(NAME)

                with (
                    patch.object(
                        adapter.auth_session.get_adapter("https://"), "send", side_effect=send
                    ) as auth,
                    patch.object(adapter.session.get_adapter("https://"), "send") as gsm,
                    ThreadPoolExecutor(max_workers=2) as pool,
                ):
                    for _ in range(2):
                        first, second = pool.submit(create), pool.submit(create)
                        first.result(timeout=10)
                        second.result(timeout=10)
                self.assertEqual(auth.call_count, 4)
                gsm.assert_not_called()

    def test_service_account_resource_error_never_reauthenticates_or_replays(self) -> None:
        for status in (307, 401, 503):
            with self.subTest(status=status):
                adapter = self.adapter()

                def send(request: PreparedRequest, status: int = status, **kwargs: Any) -> Response:
                    return reply(request, status, {}, {"Location": "https://other.example.test"})

                with (
                    patch.object(
                        adapter.auth_session.get_adapter("https://"), "send", side_effect=self.token
                    ) as auth,
                    patch.object(
                        adapter.session.get_adapter("https://"), "send", side_effect=send
                    ) as gsm,
                ):
                    with self.assertRaises(SecretUnavailable):
                        adapter.create(NAME)
                self.assertEqual(auth.call_count, 1)
                self.assertEqual(gsm.call_count, 1)


class CalendarHTTPAdapterTests(unittest.TestCase):
    def test_oauth_form_timeout_no_redirect_no_retry_and_sanitized_errors(self) -> None:
        adapter = OAuthHTTP()
        self.addCleanup(adapter.close)
        self.assertFalse(adapter.session.trust_env)
        self.assertEqual(
            cast(HTTPAdapter, adapter.session.get_adapter("https://")).max_retries.total, 0
        )
        response = Mock(status_code=200)
        response.json.return_value = {"access_token": "SYNTHETIC_ACCESS"}
        with patch.object(adapter.session, "post", return_value=response) as post:
            self.assertEqual(
                adapter("https://oauth2.googleapis.com/token", {"code": "synthetic-code"}),
                response.json.return_value,
            )
            post.assert_called_once_with(
                "https://oauth2.googleapis.com/token",
                data={"code": "synthetic-code"},
                timeout=10,
                allow_redirects=False,
            )
        for status in (302, 400, 429, 500):
            response.status_code = status
            with patch.object(adapter.session, "post", return_value=response) as post:
                with self.assertRaises(SecretUnavailable):
                    adapter("https://oauth2.googleapis.com/token", {"code": "synthetic-code"})
                self.assertEqual(post.call_count, 1)
        with patch.object(adapter.session, "post", side_effect=RuntimeError("SYNTHETIC_ACCESS")):
            with self.assertRaises(SecretUnavailable) as error:
                adapter("https://oauth2.googleapis.com/token", {})
        self.assertNotIn("SYNTHETIC_ACCESS", str(error.exception))

    def test_secret_manager_endpoint_payload_exact_version_and_destroy(self) -> None:
        adapter = SecretManagerHTTP(
            credentials=AnonymousCredentials(),  # type: ignore[no-untyped-call]
            replication={"automatic": {}},
        )
        self.addCleanup(adapter.close)
        self.assertFalse(adapter.session.trust_env)
        self.assertEqual(
            cast(HTTPAdapter, adapter.session.get_adapter("https://")).max_retries.total, 0
        )
        self.assertFalse(adapter.auth_session.trust_env)
        self.assertEqual(
            cast(HTTPAdapter, adapter.auth_session.get_adapter("https://")).max_retries.total, 0
        )
        responses = []
        for value in (
            {"name": NAME},
            {"name": NAME + "/versions/1"},
            {
                "name": NAME + "/versions/1",
                "payload": {"data": base64.b64encode(b"synthetic-secret").decode()},
            },
            {"name": NAME + "/versions/1", "state": "ENABLED"},
            {"name": NAME + "/versions/1", "state": "DESTROYED"},
        ):
            response = Mock(status_code=200)
            response.json.return_value = value
            responses.append(response)
        with patch.object(adapter.session, "request", side_effect=responses) as request:
            adapter.create(NAME)
            reference = adapter.add(NAME, b"synthetic-secret")
            self.assertEqual(adapter.access(reference), b"synthetic-secret")
            adapter.destroy(reference)
            self.assertEqual(request.call_count, 5)
            calls = request.call_args_list
            self.assertEqual(
                calls[0].args[1],
                "https://secretmanager.googleapis.com/v1/projects/synthetic-project/secrets?secretId="
                + NAME.rsplit("/", 1)[1],
            )
            self.assertEqual(
                calls[1].kwargs["json"],
                {"payload": {"data": base64.b64encode(b"synthetic-secret").decode()}},
            )
            for call in calls:
                self.assertEqual(call.kwargs["timeout"], 10)
                self.assertFalse(call.kwargs["allow_redirects"])
        for reference in (NAME + "/versions/latest", NAME + "/versions/2", "https://evil.test"):
            with self.assertRaises(SecretUnavailable):
                adapter.access(reference)

    def test_secret_errors_do_not_escape_or_retry_and_destroy_receipt_is_checked(self) -> None:
        adapter = SecretManagerHTTP(
            credentials=AnonymousCredentials(),  # type: ignore[no-untyped-call]
            replication={"automatic": {}},
        )
        self.addCleanup(adapter.close)
        with patch.object(
            adapter.session, "request", side_effect=RuntimeError("SYNTHETIC_REFRESH")
        ) as request:
            with self.assertRaises(SecretUnavailable) as error:
                adapter.add(NAME, b"SYNTHETIC_REFRESH")
            self.assertEqual(request.call_count, 1)
        self.assertNotIn("SYNTHETIC_REFRESH", str(error.exception))
        response = Mock(status_code=200)
        response.json.return_value = {"name": NAME + "/versions/1", "state": "DESTROYED"}
        with patch.object(adapter.session, "request", return_value=response) as request:
            adapter.destroy(NAME + "/versions/1")
            self.assertEqual(request.call_count, 1)
