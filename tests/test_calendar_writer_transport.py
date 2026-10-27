"""Concrete Google request semantics without external HTTP access."""

import unittest
from unittest.mock import Mock, patch

from brainoid_api.adapters.google_calendar_writer import GoogleCalendarWriter
from pydantic import SecretStr


class CalendarWriterTransportTests(unittest.TestCase):
    def test_fixed_encoded_path_conditional_patch_no_retry_and_empty_delete(self) -> None:
        adapter = GoogleCalendarWriter()
        self.addCleanup(adapter.close)
        reply = Mock(status_code=200)
        reply.json.return_value = {"id": "event"}
        with patch.object(adapter.session, "request", return_value=reply) as send:
            adapter.write(
                SecretStr("SYNTHETIC"),
                "update",
                "a/b?@example.test",
                "event/?.#",
                '"etag"',
                {"summary": "fixed"},
            )
            args, kwargs = send.call_args
            self.assertEqual(
                args,
                (
                    "PATCH",
                    "https://www.googleapis.com/calendar/v3/calendars/a%2Fb%3F%40example.test/events/event%2F%3F.%23",
                ),
            )
            self.assertEqual(kwargs["headers"]["If-Match"], '"etag"')
            self.assertEqual(kwargs["params"], {"sendUpdates": "none"})
            self.assertEqual(kwargs["timeout"], 10)
            self.assertFalse(kwargs["allow_redirects"])
            self.assertFalse(adapter.session.trust_env)
            self.assertEqual(adapter.session.get_adapter("https://").max_retries.total, 0)  # type: ignore[attr-defined]
            reply.status_code = 204
            reply.json.side_effect = AssertionError("empty response must not be decoded")
            self.assertEqual(
                adapter.write(SecretStr("SYNTHETIC"), "delete", "c", "e", '"etag"', None).status,
                204,
            )
            send.side_effect = TimeoutError("SYNTHETIC_SECRET")
            before = send.call_count
            result = adapter.write(SecretStr("SYNTHETIC"), "create", "c", "e", None, {})
            self.assertEqual(result.status, 0)
            self.assertEqual(send.call_count - before, 1)
            self.assertIsNone(result.body)
