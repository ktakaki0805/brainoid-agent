"""Real UI controllers, loopback HTTP and official Firestore; synthetic auth/provider only."""

import os
import socket
import subprocess
import threading
import time

import uvicorn
from brainoid_api.app import create_app
from brainoid_api.auth.runtime import AuthServices, authenticated_identity, get_auth_services
from brainoid_api.routes.agents import reader_identity
from brainoid_contracts import ResourceId
from brainoid_memory.forget import Operation
from fakes.calendar_disconnect import DisconnectScenarios
from fakes.calendar_writes import WriteScenarios
from test_calendar_proposals_integration import CalendarProposalIntegrationTests

from scripts.demo_emulator import ROOT


class UiCalendarIntegrationTests(
    WriteScenarios, DisconnectScenarios, CalendarProposalIntegrationTests
):
    def test_controller_fixed_proposal_exact_retry_and_disconnect(self) -> None:
        self.setup_writes()
        self.setup_disconnect()
        app = create_app()
        app.dependency_overrides[get_auth_services] = lambda: AuthServices(
            tenants=self.tenants,
            calendar=self.service,
            calendar_events=self.events,
            calendar_disconnect=self.disconnects,
            calendar_write_enabled=True,
        )
        # This suite tests current UI/HTTP ownership with an explicit synthetic identity.
        # Official Firebase bearer verification remains in the existing OAuth/IAM suites.
        app.dependency_overrides[reader_identity] = self.identity
        app.dependency_overrides[authenticated_identity] = self.identity

        @app.post("/fixture/execute/{operation_id}")
        def execute(operation_id: str) -> dict[str, str]:
            operation = self.proposals.find_operation_for_release(
                self.identity(), self.tenant, ResourceId(operation_id)
            )
            assert operation is not None
            self.writer_provider.lost = True

            def changed() -> None:
                event = next(iter(self.writer_provider.events.values()))
                event["summary"] = "Synthetic later provider change"

            self.writer_provider.after = changed
            self.execute_write(Operation.model_validate(operation.release()))
            return {"status": "executed"}

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            server = uvicorn.Server(
                uvicorn.Config(app, access_log=False, log_level="error", ws="none")
            )
            thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]})
            thread.start()
            try:
                for _ in range(200):
                    if server.started:
                        break
                    self.assertTrue(thread.is_alive())
                    time.sleep(0.05)
                self.assertTrue(server.started)
                result = subprocess.run(
                    ["node", "tests/ui/calendar_emulator.mjs"],
                    cwd=ROOT,
                    env={
                        **os.environ,
                        "UI_API_URL": f"http://127.0.0.1:{listener.getsockname()[1]}",
                        "UI_TENANT": str(self.tenant),
                        "UI_INTEGRATION": str(self.integration),
                        "UI_UID": str(self.uid),
                        "UI_NOW": self.clock.now().isoformat(),
                    },
                    capture_output=True,
                    text=True,
                    timeout=90,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("Calendar HTTP Firestore controller passed", result.stdout)
                self.assertEqual(len(self.writer_provider.calls), 1)
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                self.assertFalse(thread.is_alive(), "Owned Calendar HTTP listener did not stop")


def load_tests(loader, tests, pattern):  # type: ignore[no-untyped-def]
    import unittest

    return unittest.TestSuite(
        [UiCalendarIntegrationTests("test_controller_fixed_proposal_exact_retry_and_disconnect")]
    )
