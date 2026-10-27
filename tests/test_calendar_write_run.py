"""CAL04 run checkpoint tests with the strict synthetic store."""

import test_calendar_proposal_tool as fixture
from fakes.calendar_write_runs import RunWriteScenarios


class CalendarWriteRunTests(RunWriteScenarios, fixture.CalendarProposalToolTests):
    pass


def load_tests(loader, tests, pattern):  # type: ignore[no-untyped-def]
    import unittest

    return unittest.TestSuite(
        CalendarWriteRunTests(name)
        for name in loader.getTestCaseNames(CalendarWriteRunTests)
        if name.startswith("test_write_")
    )
