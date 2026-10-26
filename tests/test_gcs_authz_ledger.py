"""Exercise the pinned real Storage SDK at the requests HTTP transport boundary."""

import base64
import json
import unittest
from email.message import Message
from email.parser import BytesParser
from io import BytesIO
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import google_crc32c
from brainoid_api.adapters.gcs_authz_ledger import GCSAuthzObjects
from brainoid_memory.deletion_ledger import DeletionIntent, DeletionLedger, DeletionTarget
from brainoid_policy.control_ledger import ControlLedger, LedgerConflict, LedgerError
from fakes.memory_repository import FACT, NOW, TENANT
from google.auth.credentials import AnonymousCredentials
from google.auth.transport.requests import AuthorizedSession
from google.cloud.storage import Client  # type: ignore[import-untyped]
from requests import PreparedRequest, Response
from requests.adapters import BaseAdapter
from test_authz_ledger import change
from urllib3.response import HTTPResponse


class StorageHTTP(BaseAdapter):
    """A JSON API fake, not a Blob-method mock; all SDK serialization/retry runs."""

    def __init__(self) -> None:
        super().__init__()
        self.objects: dict[str, bytes] = {}
        self.requests: list[PreparedRequest] = []
        self.lose_upload_response = False
        self.forbidden = False

    def send(self, request: PreparedRequest, *args: Any, **kwargs: Any) -> Response:
        self.requests.append(request)
        response = Response()
        response.request = request
        response.url = request.url or ""
        response.status_code = 200
        response._content_consumed = True
        response.headers["Content-Type"] = "application/json"
        url = urlsplit(response.url)
        query = parse_qs(url.query)

        def fail(status: int) -> Response:
            response.status_code = status
            response._content = json.dumps(
                {"error": {"code": status, "message": "synthetic"}}
            ).encode()
            return response

        if self.forbidden:
            return fail(403)
        if request.method == "POST":
            if query.get("ifGenerationMatch") != ["0"] or query.get("uploadType") != ["multipart"]:
                raise AssertionError("SDK omitted the immutable creation precondition")
            content_type = request.headers["Content-Type"]
            if isinstance(content_type, str):
                content_type = content_type.encode()
            assert isinstance(request.body, bytes)
            message = BytesParser().parsebytes(
                b"Content-Type: " + content_type + b"\r\n\r\n" + request.body
            )
            parts = message.get_payload()
            assert isinstance(parts, list)
            metadata, content = parts
            assert isinstance(metadata, Message) and isinstance(content, Message)
            metadata_bytes, data = (
                metadata.get_payload(decode=True),
                content.get_payload(decode=True),
            )
            assert isinstance(metadata_bytes, bytes) and isinstance(data, bytes)
            name = json.loads(metadata_bytes)["name"]
            if name in self.objects:
                return fail(412)
            self.objects[name] = data
            if self.lose_upload_response:
                self.lose_upload_response = False
                return fail(503)
            response._content = json.dumps(
                {
                    "name": name,
                    "generation": "1",
                    "size": str(len(data)),
                    "crc32c": base64.b64encode(google_crc32c.value(data).to_bytes(4)).decode(),
                }
            ).encode()
        elif "/o/" in url.path:
            name = unquote(url.path.split("/o/", 1)[1])
            if name not in self.objects:
                return fail(404)
            response.headers["Content-Type"] = "application/json"
            response._content = self.objects[name]
            response.headers["Content-Length"] = str(len(response._content))
            response.headers["x-goog-hash"] = (
                "crc32c="
                + base64.b64encode(google_crc32c.value(response._content).to_bytes(4)).decode()
            )
        elif request.method == "GET":
            prefix = query.get("prefix", [""])[0]
            response._content = json.dumps(
                {
                    "items": [
                        {"name": name, "generation": "1"}
                        for name in self.objects
                        if name.startswith(prefix)
                    ]
                }
            ).encode()
        else:
            raise AssertionError("Unexpected Storage mutation")
        response.raw = HTTPResponse(
            body=BytesIO(response._content), headers=response.headers, preload_content=False
        )
        return response

    def close(self) -> None:
        pass


class GCSAuthzTests(unittest.TestCase):
    def setUp(self) -> None:
        self.transport = StorageHTTP()
        credentials = AnonymousCredentials()  # type: ignore[no-untyped-call]
        session = AuthorizedSession(credentials)  # type: ignore[no-untyped-call]
        session.mount("https://", self.transport)
        self.addCleanup(session.close)
        client = Client(project="demo-brainoid", credentials=credentials, _http=session)
        self.objects = GCSAuthzObjects(client.bucket("synthetic-control-ledger"))
        self.ledger = ControlLedger(self.objects)

    def test_sdk_conditional_create_list_download_and_immutable_conflict(self) -> None:
        proposed = change()
        entry = self.ledger.prepare(proposed)
        self.assertEqual(self.ledger.latest(proposed.subject_key), entry)
        self.assertEqual(self.ledger.prepare(proposed), entry)
        with self.assertRaises(LedgerConflict):
            self.ledger.prepare(change())
        uploads = [r for r in self.transport.requests if r.method == "POST"]
        self.assertGreater(len(uploads), 2)
        for request in uploads:
            self.assertIn("ifGenerationMatch=0", request.url or "")
        self.assertFalse(
            any(r.method in {"PATCH", "PUT", "DELETE"} for r in self.transport.requests)
        )

    def test_sdk_retries_lost_write_response_with_same_generation_then_reads_412(self) -> None:
        self.transport.lose_upload_response = True
        proposed = change()
        self.ledger.prepare(proposed)
        first, retry = [r for r in self.transport.requests if r.method == "POST"][:2]
        self.assertEqual(first.url, retry.url)
        self.assertEqual(first.body, retry.body)
        self.assertEqual(len(self.transport.objects), 3)
        self.assertIsNotNone(self.ledger.latest(proposed.subject_key))

    def test_sdk_forbidden_is_sanitized_and_never_success(self) -> None:
        self.transport.forbidden = True
        for work in (
            lambda: self.objects.read("authz/v1/absent"),
            lambda: self.objects.create("authz/v1/absent", b"{}"),
            lambda: self.objects.names("authz/v1/"),
        ):
            with self.subTest(work=work), self.assertRaises(LedgerError) as failure:
                work()
            self.assertEqual(str(failure.exception), "authorization_ledger_unavailable")
        self.assertFalse(self.transport.objects)

    def test_deletion_namespace_is_independent_and_survives_upload_response_loss(self) -> None:
        ledger = DeletionLedger(self.objects)
        intent = DeletionIntent(
            operation_id=FACT,
            tenant_id=TENANT,
            request_hash="a" * 64,
            source_policy="suppress_reuse",
            created_at=NOW,
            targets=(DeletionTarget(kind="fact", resource_id=FACT, version=1, deletion_epoch=1),),
        )
        self.transport.lose_upload_response = True
        ledger.append(intent)
        self.assertEqual(ledger.get(FACT), intent)
        ledger.append(intent)
        self.assertEqual(list(self.transport.objects), [f"deletion/v1/operations/{FACT}.json"])
        with self.assertRaises(LedgerConflict):
            ledger.append(intent.model_copy(update={"request_hash": "b" * 64}))
        self.assertFalse(
            any(r.method in {"PUT", "PATCH", "DELETE"} for r in self.transport.requests)
        )
