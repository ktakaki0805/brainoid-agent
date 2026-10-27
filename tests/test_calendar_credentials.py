"""Provider auth classification must not confuse quotas/ACLs with grant revocation."""

import unittest
from unittest.mock import Mock, patch

from brainoid_api.adapters.google_calendar import GoogleCalendar
from brainoid_api.adapters.google_calendar_writer import GoogleCalendarWriter
from brainoid_contracts.errors import APIError
from brainoid_memory.calendar_credentials import CalendarCredentialsRejected, credential_failure
from pydantic import SecretStr


class CalendarCredentialTests(unittest.TestCase):
    def test_classification_and_concrete_transport_never_refresh_or_expose_provider_body(
        self,
    ) -> None:
        reader, writer = GoogleCalendar(), GoogleCalendarWriter()
        self.addCleanup(reader.close)
        self.addCleanup(writer.close)
        for status, reason, expected in (
            (401, "authError", "credentials_expired_or_revoked"),
            (403, "insufficientPermissions", "permission_revoked"),
            (403, "rateLimitExceeded", None),
            (403, "userRateLimitExceeded", None),
            (403, "quotaExceeded", None),
            (403, "forbiddenForNonOrganizer", None),
            (403, "forbidden", None),
            (404, "notFound", None),
        ):
            with self.subTest(status=status, reason=reason):
                body = {
                    "error": {
                        "errors": [{"reason": reason}],
                        "message": "SYNTHETIC_PROVIDER_SECRET",
                    }
                }
                self.assertEqual(credential_failure(status, body), expected)
                response = Mock(status_code=status)
                response.json.return_value = body
                with patch.object(reader.session, "get", return_value=response) as send:
                    with self.assertRaises(APIError) as failure:
                        reader.event(SecretStr("SYNTHETIC_ACCESS"), "private", "event")
                    self.assertEqual(
                        isinstance(failure.exception, CalendarCredentialsRejected),
                        expected is not None,
                    )
                    self.assertEqual(send.call_count, 1)
                    self.assertNotIn("SYNTHETIC", str(failure.exception))
                with patch.object(writer.session, "request", return_value=response) as send:
                    reply = writer.inspect(SecretStr("SYNTHETIC_ACCESS"), "private", "event")
                    self.assertEqual(reply.reauth_reason, expected)
                    self.assertIsNone(reply.body)
                    self.assertEqual(send.call_count, 1)
