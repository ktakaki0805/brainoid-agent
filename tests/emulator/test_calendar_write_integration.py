"""CAL04 confirmations on Firestore transactions with synthetic provider I/O."""

import json
from concurrent.futures import ThreadPoolExecutor

import test_calendar_proposals_integration as fixture
from brainoid_memory.calendar_writer import KIND
from brainoid_memory.forget import Operation
from brainoid_persistence.firestore import PersistenceUnavailable
from fakes.calendar_write_runs import RunWriteScenarios
from fakes.calendar_writes import WriteScenarios


class CalendarWriteIntegrationTests(WriteScenarios, fixture.CalendarProposalIntegrationTests):
    def test_concurrent_confirm_keys_share_operation(self) -> None:
        self.setup_writes()
        proposal = self.propose()

        def attempt(key: str) -> tuple[str, Operation | None]:
            try:
                return key, self.confirm_write(proposal, key)
            except PersistenceUnavailable:
                # The bounded transaction budget can exhaust under contention.
                # Drain every concurrent call, then retry only that same intent/key.
                return key, None

        with ThreadPoolExecutor(max_workers=4) as pool:
            attempts = list(
                pool.map(
                    attempt,
                    [
                        "calendar-concurrent-same",
                        "calendar-concurrent-same",
                        "calendar-concurrent-other",
                        "calendar-concurrent-third",
                    ],
                )
            )
        unavailable = sum(result is None for _, result in attempts)
        print(
            json.dumps(
                {
                    "calendar_confirm_contention": {
                        "responses": len(attempts) - unavailable,
                        "unavailable": unavailable,
                        "same_key_replays": unavailable,
                    }
                }
            )
        )
        results = [
            self.confirm_write(proposal, key) if result is None else result
            for key, result in attempts
        ]
        self.assertEqual(len({result.operation_id for result in results}), 1)
        operations = self.store.run(lambda tx: tx.collection(f"tenants/{self.tenant}/operations"))
        jobs = self.store.run(lambda tx: tx.collection(f"tenants/{self.tenant}/jobs"))
        self.assertEqual(len([v for _, v in operations if v["operation"]["kind"] == KIND]), 1)
        self.assertEqual(len([v for _, v in jobs if v["kind"] == KIND]), 1)
        self.assertEqual(self.writer_provider.calls, [])
        self.execute_write(results[0])
        self.assertEqual(len(self.writer_provider.calls), 1)
        self.assertEqual(self.saved_operation(results[0])["operation"]["status"], "succeeded")


class CalendarWriteRunIntegrationTests(
    RunWriteScenarios, fixture.CalendarProposalRunIntegrationTests
):
    pass


def load_tests(loader, tests, pattern):  # type: ignore[no-untyped-def]
    import unittest

    return unittest.TestSuite(
        [
            loader.loadTestsFromTestCase(CalendarWriteIntegrationTests),
            unittest.TestSuite(
                CalendarWriteRunIntegrationTests(name)
                for name in loader.getTestCaseNames(CalendarWriteRunIntegrationTests)
                if name.startswith("test_write_")
            ),
        ]
    )
