"""CAL05 strict fake acceptance."""

import unittest

from fakes.calendar_disconnect import DisconnectScenarios
from fakes.calendar_disconnect_regressions import DisconnectRegressions
from fakes.calendar_export_wait import CalendarExportWaitScenarios
from fakes.calendar_proposals import ProposalScenarios
from fakes.purge import FakePurgePages
from test_notifications import NotificationStore


class CalendarDisconnectTests(
    CalendarExportWaitScenarios, DisconnectRegressions, DisconnectScenarios, ProposalScenarios
):
    def setUp(self) -> None:
        self.store = NotificationStore()
        self.setup_proposals()
        self.setup_disconnect()
        self.purge_pages = FakePurgePages(self.store)


def load_tests(loader, tests, pattern):  # type: ignore[no-untyped-def]
    return unittest.TestSuite(
        CalendarDisconnectTests(name)
        for name in loader.getTestCaseNames(CalendarDisconnectTests)
        if name.startswith("test_disconnect_")
    )
