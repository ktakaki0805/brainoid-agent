"""CAL04 strict fake acceptance."""

from fakes import calendar_proposals, calendar_writes
from test_notifications import NotificationStore


class CalendarWriteTests(calendar_writes.WriteScenarios, calendar_proposals.ProposalScenarios):
    def setUp(self) -> None:
        self.store = NotificationStore()
        self.setup_proposals()
