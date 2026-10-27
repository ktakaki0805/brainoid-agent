"""CAL03 ordinary synthetic acceptance."""

import unittest
from unittest.mock import Mock, patch

from brainoid_api.adapters.google_calendar import GoogleCalendar
from brainoid_contracts.errors import APIError
from fakes import calendar_proposals
from pydantic import SecretStr
from requests.adapters import HTTPAdapter
from test_notifications import NotificationStore


class CalendarProposalTests(calendar_proposals.ProposalScenarios):
    def setUp(self) -> None:
        self.store = NotificationStore()
        self.setup_proposals()

    def test_standalone_expiry_reaper_is_bounded_and_idempotent(self) -> None:
        from brainoid_memory.calendar_proposals import proposal_path

        proposal = self.propose()
        self.clock.advance(300)
        self.proposals.service.expire_unsuspended(self.tenant, self.store, limit=1)
        self.proposals.service.expire_unsuspended(self.tenant, self.store, limit=1)
        self.assertEqual(
            self.store.data[proposal_path(self.tenant, proposal.proposal_id)]["status"], "expired"
        )


class ProposalTransportTests(unittest.TestCase):
    def test_target_get_fixed_encoded_ids_explicit_utc_no_redirect_retry_or_error_text(
        self,
    ) -> None:
        adapter = GoogleCalendar()
        self.addCleanup(adapter.close)
        response = Mock(status_code=200)
        response.json.return_value = {"id": "event"}
        with patch.object(adapter.session, "get", return_value=response) as get:
            adapter.event(SecretStr("SYNTHETIC_TOKEN"), "a/b?@example.test", "event/?.#")
            args, kwargs = get.call_args
            self.assertEqual(
                args[0],
                "https://www.googleapis.com/calendar/v3/calendars/a%2Fb%3F%40example.test/events/event%2F%3F.%23",
            )
            self.assertEqual(kwargs["params"], {"timeZone": "UTC"})
            self.assertFalse(kwargs["allow_redirects"])
            self.assertEqual(kwargs["timeout"], 10)
            transport = adapter.session.get_adapter("https://")
            assert isinstance(transport, HTTPAdapter)
            self.assertEqual(transport.max_retries.total, 0)
            for status in (301, 401, 403, 404, 429, 500):
                response.status_code = status
                with self.assertRaises(APIError):
                    adapter.event(SecretStr("SYNTHETIC_TOKEN"), "calendar", "event")
            get.side_effect = TimeoutError("SYNTHETIC_SECRET_PROVIDER_BODY")
            with self.assertRaises(APIError) as error:
                adapter.event(SecretStr("SYNTHETIC_TOKEN"), "calendar", "event")
            self.assertNotIn("SYNTHETIC", str(error.exception))
