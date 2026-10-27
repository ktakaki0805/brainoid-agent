"""CAL05 exact stop/purge on Firestore, all provider I/O synthetic."""

import unittest

from brainoid_persistence.purge import FirestorePurgePages
from fakes.calendar_disconnect import DisconnectScenarios
from fakes.calendar_disconnect_regressions import DisconnectRegressions
from fakes.calendar_export_wait import CalendarExportWaitScenarios
from test_calendar_proposals_integration import CalendarProposalIntegrationTests


class CalendarDisconnectIntegrationTests(
    CalendarExportWaitScenarios,
    DisconnectRegressions,
    DisconnectScenarios,
    CalendarProposalIntegrationTests,
):
    def setUp(self) -> None:
        super().setUp()
        self.setup_disconnect()
        self.purge_pages = FirestorePurgePages(self.store._client)


def load_tests(loader, tests, pattern):  # type: ignore[no-untyped-def]
    return unittest.TestSuite(
        CalendarDisconnectIntegrationTests(name)
        for name in loader.getTestCaseNames(CalendarDisconnectIntegrationTests)
        if name.startswith("test_disconnect_")
    )
