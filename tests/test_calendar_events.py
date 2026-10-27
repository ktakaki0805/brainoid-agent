"""CAL-02 time, provider transport and shared reader acceptance cases."""

import unittest
from datetime import UTC, datetime
from unittest.mock import Mock, patch

from brainoid_api.adapters.google_calendar import GoogleCalendar
from brainoid_contracts.errors import APIError
from brainoid_memory.calendar_reader import CalendarEvent, CalendarQuery, normalize
from fakes import calendar_events
from pydantic import SecretStr
from test_notifications import NotificationStore


class CalendarEventsTests(calendar_events.CalendarEventsScenarios):
    def setUp(self) -> None:
        self.store = NotificationStore()
        self.setup_events()


class CalendarTimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.query = CalendarQuery.model_validate(
            {
                "calendar_id": "a/b ?@example.test",
                "time_min": "2026-03-01T00:00:00Z",
                "time_max": "2026-04-01T00:00:00Z",
            }
        )

    def normalized(self, **changes: object) -> CalendarEvent:
        return normalize(
            {"timeZone": "America/New_York", "items": [calendar_events.event(**changes)]},
            self.query,
            datetime.now(UTC),
        )[0][0]

    def test_all_day_exclusive_end_and_dst_fall_date_crossing(self) -> None:
        value = self.normalized(start={"date": "2026-03-07"}, end={"date": "2026-03-10"})
        self.assertEqual(value.model_dump(mode="json")["end"], {"date": "2026-03-10"})
        value = self.normalized(
            start={"dateTime": "2026-11-01T01:30:00-04:00"},
            end={"dateTime": "2026-11-01T01:30:00-05:00"},
        )
        from brainoid_memory.calendar_reader import CalendarDateTime

        assert isinstance(value.start, CalendarDateTime) and isinstance(value.end, CalendarDateTime)
        self.assertEqual((value.end.date_time - value.start.date_time).total_seconds(), 3600)
        value = self.normalized(
            start={"dateTime": "2026-03-07T23:30:00-05:00"},
            end={"dateTime": "2026-03-08T00:30:00-05:00"},
        )
        assert isinstance(value.end, CalendarDateTime)
        self.assertEqual(value.end.date_time.day, 8)

    def test_missing_offset_zone_mismatch_gap_fold_and_mixed_endpoints(self) -> None:
        for start in (
            {"dateTime": "2026-03-08T01:30:00"},
            {"dateTime": "2026-03-08T02:30:00", "timeZone": "America/New_York"},
            {"dateTime": "2026-11-01T01:30:00", "timeZone": "America/New_York"},
            {"dateTime": "2026-03-08T01:30:00-04:00"},
            {"dateTime": "2026-03-08T01:30:00Z", "timeZone": "Invalid"},
            {"date": "2026-03-08"},
        ):
            with self.subTest(start=start), self.assertRaises(ValueError):
                self.normalized(start=start)
        self.assertEqual(self.normalized(summary=None).summary, "予定")

    def test_provider_http_fixed_path_no_redirect_retry_secret_errors(self) -> None:
        adapter = GoogleCalendar()
        self.addCleanup(adapter.close)
        self.assertFalse(adapter.session.trust_env)
        response = Mock(status_code=200)
        response.json.return_value = {"timeZone": "UTC", "items": []}
        with patch.object(adapter.session, "get", return_value=response) as get:
            adapter.events(SecretStr("SYNTHETIC_TOKEN"), self.query, None)
            args, kwargs = get.call_args
            self.assertEqual(
                args[0],
                "https://www.googleapis.com/calendar/v3/calendars/a%2Fb%20%3F%40example.test/events",
            )
            self.assertEqual(kwargs["timeout"], 10)
            self.assertFalse(kwargs["allow_redirects"])
            for status in (301, 401, 403, 404, 429, 500):
                response.status_code = status
                with self.assertRaises(APIError):
                    adapter.events(SecretStr("SYNTHETIC_TOKEN"), self.query, None)
            get.side_effect = TimeoutError("SYNTHETIC_PRIVATE_PROVIDER_TEXT")
            with self.assertRaises(APIError) as error:
                adapter.events(SecretStr("SYNTHETIC_TOKEN"), self.query, None)
            self.assertNotIn("SYNTHETIC", str(error.exception))
