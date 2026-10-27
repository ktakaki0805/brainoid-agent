"""Real Calendar tool output, later turns and independent archive erasure."""

import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

from brainoid_api.services.calendar_disconnect import CalendarDisconnect
from brainoid_contracts import ResourceId
from brainoid_jobs.models import job_id
from brainoid_memory.calendar_export import CalendarCleanupPending
from brainoid_memory.calendar_oauth import OAuthStart
from brainoid_memory.calendar_reader import integration_path
from brainoid_memory.deletion_ledger import DeletionLedger
from brainoid_memory.lifecycle import KIND
from brainoid_worker.entry import Worker
from brainoid_worker.lifecycle_handlers import LifecycleHandlers
from brainoid_worker.local import LocalCaller
from brainoid_worker.purge_authority import PurgeJobs
from brainoid_worker.purge_resource import NoExternalCopies, PurgeResource
from fakes.authz_ledger import MemoryObjects
from fakes.memory_repository import TENANT
from fakes.purge import FakePurgePages
from test_calendar_lifecycle import calendar
from test_calendar_tool import CalendarToolTests
from test_exports import ExportFixture


class CalendarDisconnectDerivedTests(CalendarToolTests):
    def test_disconnect_real_calendar_tool_messages_later_turn_and_summary_are_erased(self) -> None:
        conversation, run = self.run_calendar()
        self.calendar_model.calls = ()
        second = ResourceId(
            self.turn(conversation, key="calendar-disconnect-next", text="続けて").json()["run_id"]
        )
        self.execute(second)
        messages = [
            (p, d)
            for p, d in self.store.data.items()
            if f"/conversations/{conversation}/messages/" in p
        ]
        assistant = next(doc for _, doc in messages if doc["role"] == "assistant")
        summary = f"tenants/{self.tenant}/conversations/{conversation}/summaries/00000000-0000-0000-0000-000000000099"
        self.store.data[summary] = {
            "tenant_id": self.tenant,
            "text": "SYNTHETIC_PRIVATE_APPOINTMENT",
            "sources": {
                "conversation_id": conversation,
                "messages": [{"resource_id": assistant["id"], "included": True}],
            },
        }
        ledger = DeletionLedger(MemoryObjects())
        service = CalendarDisconnect(self.authorization, ledger)
        service.disconnect(
            self.identity(), self.tenant, self.integration, "calendar-disconnect-derived", '"2"'
        )
        self.assertNotIn(
            "SYNTHETIC_PRIVATE_APPOINTMENT",
            self.client.get(f"{self.url}/{conversation}/messages").text,
        )
        job_repo = PurgeJobs(self.jobs, ledger)
        handler = PurgeResource(
            job_repo,
            FakePurgePages(cast(Any, self.store)),
            handlers=LifecycleHandlers(),
            hooks={
                "cache": NoExternalCopies(),
                "export": NoExternalCopies(),
                "calendar": self.authorization.secrets,
            },
            page_size=2,
            step_budget=20000,
        )
        worker = Worker(job_repo, LocalCaller(), {KIND: handler})
        integration = self.store.data[f"tenants/{self.tenant}/integrations/{self.integration}"]
        identity = job_id(self.tenant, KIND, ResourceId(integration["disconnect_operation_id"]))
        job_repo.activate(self.tenant, identity)
        delivery = job_repo.prepare_dispatch(self.tenant, identity, queue_name=KIND)
        assert delivery is not None
        worker.handle("Bearer local-synthetic-worker", delivery.model_dump(mode="json"))
        self.assertEqual(
            service.get(self.identity(), self.tenant, self.integration).cleanup_status, "complete"
        )
        self.assertNotIn("SYNTHETIC_PRIVATE_APPOINTMENT", json.dumps(self.store.data, default=str))
        for identity in (run, second):
            self.assertNotIn(f"tenants/{self.tenant}/runs/{identity}", self.store.data)
        self.assertNotIn(summary, self.store.data)
        for path, doc in messages:
            self.assertEqual(path in self.store.data, doc["role"] == "user")


class CalendarDisconnectExportTests(ExportFixture):
    def test_disconnect_archive_hook_survives_removed_producers_and_preserves_new_exports(
        self,
    ) -> None:
        authorization = calendar(
            self.tenants, Path(self.enterContext(tempfile.TemporaryDirectory()))
        )
        start = authorization.start(
            self.identity(),
            TENANT,
            OAuthStart(scope_profile="read", return_target="web"),
            "calendar-disconnect-export-oauth",
        )
        state = parse_qs(urlsplit(start.authorization_url).query)["state"][0]
        authorization.callback(state, "synthetic-code", None)
        integration = authorization.integrations(self.identity(), TENANT)[0].id
        old_export = self.ready()
        ledger = DeletionLedger(MemoryObjects())
        service = CalendarDisconnect(authorization, ledger)
        service.disconnect(
            self.identity(), TENANT, integration, "calendar-disconnect-export", '"1"'
        )
        saved = self.store.data[integration_path(TENANT, integration)]
        intent = ledger.get(ResourceId(saved["disconnect_operation_id"]))
        assert intent is not None
        self.now += timedelta(seconds=1)
        new_export = self.create_export("calendar-export-after-disconnect")
        with self.assertRaises(CalendarCleanupPending):
            self.generate(new_export)
        del self.store.data[integration_path(TENANT, integration)]
        del self.store.data[f"tenants/{TENANT}/calendar_authorizations/{saved['authorization_id']}"]
        jobs = PurgeJobs(self.exports.jobs, ledger)
        handler = PurgeResource(
            jobs,
            self.pages,
            handlers=LifecycleHandlers(),
            hooks={
                "cache": NoExternalCopies(),
                "export": self.exports,
                "calendar": authorization.secrets,
            },
            step_budget=20000,
        )
        identity = job_id(TENANT, KIND, intent.operation_id)
        jobs.activate(TENANT, identity)
        delivery = jobs.prepare_dispatch(TENANT, identity, queue_name=KIND)
        assert delivery is not None
        Worker(jobs, LocalCaller(), {KIND: handler}).handle(
            "Bearer local-synthetic-worker", delivery.model_dump(mode="json")
        )
        self.now += timedelta(seconds=61)
        self.generate(new_export)
        self.erased(old_export)
        self.assertTrue((self.storage.directory / (new_export + ".sealed")).exists())


def load_tests(loader, tests, pattern):  # type: ignore[no-untyped-def]
    return unittest.TestSuite(
        case(name)
        for case in (CalendarDisconnectDerivedTests, CalendarDisconnectExportTests)
        for name in loader.getTestCaseNames(case)
        if name.startswith("test_disconnect_")
    )
