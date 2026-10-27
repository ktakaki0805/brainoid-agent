"""CAL-02 default conversation registration, private output and persisted source fences."""

import json
import tempfile
from collections.abc import AsyncGenerator
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from brainoid_agent_core.ports import CallControl
from brainoid_agent_core.types import (
    ModelEvent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextDelta,
    TextPart,
    ToolCall,
)
from brainoid_api.auth.runtime import AuthServices, get_auth_services
from brainoid_api.routes.agents import reader_identity
from brainoid_api.services.calendar_events import CalendarEvents
from brainoid_api.services.private_sessions import PrivateSessionCreate
from brainoid_contracts import DisplayPolicy, ResourceId
from brainoid_memory.calendar_oauth import OAuthStart
from brainoid_memory.calendar_reader import CalendarQuery, CalendarReader, CalendarSelection
from brainoid_runs.state import KIND
from brainoid_worker.entry import Worker
from brainoid_worker.local import LocalCaller
from brainoid_worker.run_turn import RunTurn
from fakes.calendar_events import EventsProvider
from fakes.run_confirmation import MultiStageModel
from test_calendar_lifecycle import calendar
from test_turn_run import TurnRunFixture


class CalendarModel(MultiStageModel):
    async def stream(
        self, request: ModelRequest, *, control: CallControl
    ) -> AsyncGenerator[ModelEvent]:
        continued = any(message.role == "tool" for message in request.messages)
        async for value in super().stream(request, control=control):
            if continued and isinstance(value, TextDelta):
                value = TextDelta(text="SYNTHETIC_PRIVATE_APPOINTMENT")
            elif continued and isinstance(value, ModelResponse):
                value = ModelResponse(
                    message=ModelMessage(
                        role="assistant", parts=(TextPart(text="SYNTHETIC_PRIVATE_APPOINTMENT"),)
                    )
                )
            yield value


class CalendarToolTests(TurnRunFixture):
    def setUp(self) -> None:
        super().setUp()
        self.authorization = calendar(
            self.tenants, Path(self.enterContext(tempfile.TemporaryDirectory()))
        )
        started = self.authorization.start(
            self.identity(),
            self.tenant,
            OAuthStart(scope_profile="read", return_target="mobile"),
            "calendar-tool-oauth-key",
        )
        state = parse_qs(urlsplit(started.authorization_url).query)["state"][0]
        self.authorization.callback(state, "synthetic", None)
        self.integration = self.authorization.integrations(self.identity(), self.tenant)[0].id
        self.provider = EventsProvider()
        self.reader = CalendarReader(
            self.store,
            self.provider,
            self.authorization.secrets,
            self.tenants._cursors,
            now=self.clock.now,
        )
        self.calendar = CalendarEvents(self.authorization, self.reader, self.sessions)
        self.query = CalendarQuery.model_validate(
            {
                "calendar_id": "private-calendar",
                "time_min": "2026-03-01T00:00:00Z",
                "time_max": "2026-04-01T00:00:00Z",
            }
        )
        self.calendar.select(
            self.identity(),
            self.tenant,
            self.integration,
            CalendarSelection(allowed_calendar_ids=[self.query.calendar_id]),
            "calendar-tool-select-key",
            '"1"',
        )
        self.arguments = {
            "integration_id": self.integration,
            **self.query.model_dump(mode="json", exclude_none=True),
        }
        self.configure()
        self.app.dependency_overrides[get_auth_services] = lambda: AuthServices(
            tenants=self.tenants,
            devices=self.repo,
            private_sessions=self.sessions,
            conversations=self.conversations,
            calendar=self.authorization,
            calendar_events=self.calendar,
        )

    def configure(self) -> None:
        self.store.data[f"tenants/{self.tenant}/agents/{self.agent_id}"]["allowed_tools"] = [
            "calendar.list"
        ]
        self.calendar_model = CalendarModel(
            (
                ToolCall(
                    call_key="calendar",
                    name="calendar.list",
                    arguments_json=json.dumps(self.arguments),
                ),
            )
        )
        self.model = self.calendar_model  # type: ignore[assignment]
        self.handler = RunTurn(
            self.jobs, self.model, credential_epoch=self.tokens.epoch, calendar_reader=self.reader
        )
        self.worker = Worker(self.jobs, LocalCaller(), {KIND: self.handler})

    def run_calendar(self, *, private: bool = True) -> tuple[ResourceId, ResourceId]:
        conversation = self.conversation(private=private)
        run = ResourceId(self.turn(conversation, text="3月の本人の予定を確認して").json()["run_id"])
        self.execute(run)
        return conversation, run

    def test_default_registered_tool_same_service_private_conversation_and_next_turn(self) -> None:
        conversation, run = self.run_calendar()
        self.assertEqual(self.get_run(run).json()["status"], "completed", self.get_run(run).text)
        self.assertEqual(len(self.model.requests), 2)
        self.assertIn("SYNTHETIC_PRIVATE_APPOINTMENT", self.model.requests[-1].model_dump_json())
        self.assertTrue(self.store.data[f"tenants/{self.tenant}/runs/{run}"]["calendar_sources"])
        messages = self.client.get(f"{self.url}/{conversation}/messages").json()["items"]
        self.assertTrue(any("SYNTHETIC_PRIVATE_APPOINTMENT" in m["text"] for m in messages))
        self.calendar_model.calls = ()
        second = ResourceId(
            self.turn(conversation, key="calendar-tool-next-turn", text="続けて").json()["run_id"]
        )
        self.execute(second)
        self.assertEqual(self.get_run(second).json()["status"], "completed")
        self.assertTrue(self.store.data[f"tenants/{self.tenant}/runs/{second}"]["calendar_sources"])
        location = f"tenants/{self.tenant}/integrations/{self.integration}"
        self.store.data[location]["status"] = "revoked"
        for identity in (run, second):
            self.assertNotEqual(self.get_run(identity).status_code, 200)
            self.assertNotEqual(
                self.client.get(f"/v1/tenants/{self.tenant}/runs/{identity}/events").status_code,
                200,
            )
        listed = self.client.get(f"{self.url}/{conversation}/messages")
        self.assertNotIn("SYNTHETIC_PRIVATE_APPOINTMENT", listed.text)
        previous = len(self.model.requests)
        third = ResourceId(
            self.turn(conversation, key="calendar-tool-revoked-turn").json()["run_id"]
        )
        self.execute(third)
        self.assertEqual(len(self.model.requests), previous)

    def test_shared_conversation_other_owner_and_model_scope_injection_denied(self) -> None:
        before = len(self.provider.calls)
        _, run = self.run_calendar(private=False)
        self.assertNotEqual(self.get_run(run).json()["status"], "completed")
        self.assertEqual(len(self.provider.calls), before)
        self.arguments["owner_user_id"] = "another-owner"
        self.configure()
        _, run = self.run_calendar()
        self.assertNotEqual(self.get_run(run).json()["status"], "completed")
        self.assertEqual(len(self.provider.calls), before)

    def test_late_revocation_blocks_private_receipt_checkpoint_and_stream(self) -> None:
        location = f"tenants/{self.tenant}/integrations/{self.integration}"
        self.provider.after = lambda: self.store.data[location].update(status="revoked")
        _, run = self.run_calendar()
        raw = json.dumps(self.store.data, default=str)
        self.assertNotIn("SYNTHETIC_PRIVATE_APPOINTMENT", raw)
        self.assertNotEqual(self.get_run(run).json()["status"], "completed")

    def test_unrepresentable_cursor_default_tool_exposes_no_partial_provider_page(self) -> None:
        page = self.provider.pages[None]
        page["nextPageToken"] = "あ" * 100
        page["items"][0]["summary"] = "SYNTHETIC_CURSOR_PRIVATE_EVENT"
        self.run_calendar()
        records = [
            value
            for path, value in self.store.data.items()
            if "/tool_executions/" in path and path.count("/") == 3
        ]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["result"]["status"], "failed")
        self.assertEqual(records[0]["result"]["error_code"], "unavailable")
        self.assertEqual(records[0]["result"]["result"], {})
        self.assertNotIn("SYNTHETIC_CURSOR_PRIVATE_EVENT", json.dumps(self.store.data, default=str))
        for request in self.model.requests:
            self.assertNotIn("SYNTHETIC_CURSOR_PRIVATE_EVENT", request.model_dump_json())

    def test_pi_private_session_read_scope_current_family_and_expiry(self) -> None:
        credentials = self.credentials()
        device = self.store.data[f"tenants/{self.tenant}/devices/{credentials.device_id}"]
        device["capabilities"] = ["display"]
        device["allowed_scopes"] = ["conversation", "calendar.read"]
        identity = self.repo.authenticate(credentials.access_token)
        self.app.dependency_overrides[reader_identity] = lambda: identity
        url = f"/v1/tenants/{self.tenant}/integrations/{self.integration}/calendar-events"
        params = self.query.model_dump(mode="json", exclude_none=True)
        self.assertNotEqual(self.client.get(url, params=params).status_code, 200)
        session = self.sessions.start(
            self.identity(),
            self.tenant,
            identity.sub,
            PrivateSessionCreate(acknowledge_shared_space=True, allowed_scopes=["conversation"]),
            "calendar-pi-session-1",
            "calendar-request",
        )
        self.assertNotEqual(
            self.client.get(
                url, params=params, headers={"X-Private-Session": session.id}
            ).status_code,
            200,
        )
        self.sessions.end(
            self.identity(),
            self.tenant,
            session.id,
            "calendar-pi-session-end",
            '"1"',
            "calendar-request",
        )
        session = self.sessions.start(
            self.identity(),
            self.tenant,
            identity.sub,
            PrivateSessionCreate(
                acknowledge_shared_space=True, allowed_scopes=["conversation", "calendar.read"]
            ),
            "calendar-pi-session-2",
            "calendar-request",
        )
        headers = {"X-Private-Session": session.id}
        response = self.client.get(url, params=params, headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.app.dependency_overrides[reader_identity] = lambda: self.identity()
        conversation = self.conversation(private=True, display=DisplayPolicy.AUTHENTICATED_SESSION)
        self.app.dependency_overrides[reader_identity] = lambda: identity
        accepted = self.client.post(
            f"{self.url}/{conversation}/turns",
            json={"text": "本人の予定", "output_mode": "text"},
            headers={**headers, "Idempotency-Key": "calendar-pi-tool-turn"},
        )
        self.assertEqual(accepted.status_code, 202, accepted.text)
        run = ResourceId(accepted.json()["run_id"])
        self.execute(run)
        result = self.client.get(f"/v1/tenants/{self.tenant}/runs/{run}", headers=headers)
        self.assertEqual(result.json()["status"], "completed", result.text)
        family_path = "device_credential_families/" + identity.family_id
        family = dict(self.store.data[family_path])
        self.store.data[family_path]["status"] = "revoked"
        before = len(self.provider.calls)
        self.assertNotEqual(self.client.get(url, params=params, headers=headers).status_code, 200)
        self.assertEqual(len(self.provider.calls), before)
        self.store.data[family_path] = family
        self.clock.advance(61)
        self.assertNotEqual(self.client.get(url, params=params, headers=headers).status_code, 200)
        self.assertNotEqual(
            self.client.get(
                f"/v1/tenants/{self.tenant}/runs/{run}/events", headers=headers
            ).status_code,
            200,
        )

    def test_actual_calendar_export_revokes_and_lifecycle_purges_all_copies(self) -> None:
        import io
        import zipfile

        from brainoid_api.adapters.local_export_objects import LocalExportObjects
        from brainoid_api.services.deletion_requests import DeleteTenant, DeletionRequests
        from brainoid_api.services.export_requests import ExportRequests
        from brainoid_contracts.errors import APIError
        from brainoid_jobs.models import job_id
        from brainoid_memory import CanonicalSources
        from brainoid_memory.calendar_oauth import SecretUnavailable
        from brainoid_memory.deletion_ledger import DeletionLedger
        from brainoid_memory.export_manifest import KIND as EXPORT_KIND
        from brainoid_memory.export_manifest import ExportCreate
        from brainoid_memory.exports import Exports
        from brainoid_memory.lifecycle import KIND as PURGE_KIND
        from brainoid_runs.memory_sources import SavedMessages
        from brainoid_worker.entry import Execution
        from brainoid_worker.export_data import ExportData
        from brainoid_worker.lifecycle_handlers import LifecycleHandlers
        from brainoid_worker.local import LOCAL_AUTHORIZATION
        from brainoid_worker.purge_authority import PurgeJobs
        from brainoid_worker.purge_resource import NoExternalCopies, PurgeResource
        from fakes.authz_ledger import MemoryObjects
        from fakes.purge import FakePurgePages

        conversation, run = self.run_calendar()
        directory = Path(self.enterContext(tempfile.TemporaryDirectory())) / "exports"
        objects = LocalExportObjects(directory, b"c" * 32, now=self.clock.now)
        pages = FakePurgePages(self.store)  # type: ignore[arg-type]
        exports = Exports(
            self.jobs, objects, pages, CanonicalSources(SavedMessages()), now=self.clock.now
        )
        requests = ExportRequests(exports, self.tenants)
        operation = requests.create(
            self.identity(),
            self.tenant,
            ExportCreate(kind="export", scope="my_data"),
            "calendar-export-request",
        )
        delivery = self.jobs.prepare_dispatch(
            self.tenant, job_id(self.tenant, EXPORT_KIND, operation.operation_id)
        )
        assert delivery is not None
        Worker(self.jobs, LocalCaller(), {EXPORT_KIND: ExportData(exports)}).handle(
            LOCAL_AUTHORIZATION, delivery.model_dump(mode="json")
        )
        content = requests.download(self.identity(), self.tenant, operation.operation_id)
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            body = archive.read("data.json").decode()
        self.assertIn("SYNTHETIC_PRIVATE_APPOINTMENT", body)
        self.assertNotIn("SYNTHETIC_ACCESS", body)
        self.assertNotIn("tool_bindings", body)
        location = f"tenants/{self.tenant}/integrations/{self.integration}"
        secret = self.store.data[location]["secret_ref"]
        self.store.data[location]["status"] = "revoked"
        with self.assertRaises(APIError):
            requests.download(self.identity(), self.tenant, operation.operation_id)

        ledger = DeletionLedger(MemoryObjects())
        deletion = DeletionRequests(self.store, ledger, self.ledger, now=self.clock.now)
        operation = deletion.create(
            self.identity(),
            DeleteTenant(
                kind="delete_tenant",
                confirmation_name=self.store.data[f"tenants/{self.tenant}"]["name"],
            ),
            "calendar-delete-tenant",
            self.tenant,
        )
        purge_jobs = PurgeJobs(self.jobs, ledger)
        handler = PurgeResource(
            purge_jobs,
            pages,
            handlers=LifecycleHandlers(),
            hooks={
                "cache": NoExternalCopies(),
                "export": exports,
                "calendar": self.authorization.secrets,
            },
        )
        job = job_id(self.tenant, PURGE_KIND, operation.operation_id)
        purge_jobs.activate(self.tenant, job)
        delivery = purge_jobs.prepare_dispatch(self.tenant, job, queue_name=PURGE_KIND)
        assert delivery is not None
        lease = purge_jobs.acquire(delivery, known_handler=lambda kind: kind == PURGE_KIND)
        assert lease is not None
        execution = Execution(purge_jobs, lease)
        for _ in range(5000):
            if handler.step(execution):
                purge_jobs.complete(lease, result={"purged": True}, commit=handler.commit)
                break
        else:
            self.fail("purge failed to terminate")
        self.assertNotIn("SYNTHETIC_PRIVATE_APPOINTMENT", json.dumps(self.store.data, default=str))
        self.assertNotIn(f"tenants/{self.tenant}/runs/{run}", self.store.data)
        self.assertNotIn(f"tenants/{self.tenant}/conversations/{conversation}", self.store.data)
        with self.assertRaises(SecretUnavailable):
            self.authorization.secrets.get(secret)
