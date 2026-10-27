"""Calendar HTTP composition and export release after the final threadpool response."""

import asyncio
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import brainoid_api.routes.calendar_proposals as proposal_routes
import brainoid_api.routes.conversations as conversation_routes
import brainoid_api.routes.operation_download as download_routes
import brainoid_api.services.calendar_proposals as proposal_service
from brainoid_api.adapters.local_export_objects import LocalExportObjects
from brainoid_api.auth.runtime import AuthServices, get_auth_services
from brainoid_api.routes.agents import reader_identity
from brainoid_api.routes.operation_download import ExportResponse
from brainoid_api.services.export_requests import ExportRequests
from brainoid_contracts.errors import APIError, ErrorCode
from brainoid_contracts.guards import ReleaseGuard
from brainoid_jobs.models import job_id
from brainoid_memory.calendar_proposal_access import check_origin
from brainoid_memory.export_manifest import KIND as EXPORT
from brainoid_memory.export_manifest import ExportCreate
from brainoid_memory.exports import Exports, registry_path
from brainoid_memory.sources import CanonicalSources
from brainoid_persistence.purge import FirestorePurgePages
from brainoid_policy.control_ledger import LedgerError, subject_key
from brainoid_policy.scopes import PolicyUnavailable
from brainoid_runs.memory_sources import SavedMessages
from brainoid_runs.state import KIND
from brainoid_worker.entry import Worker
from brainoid_worker.export_data import ExportData
from brainoid_worker.local import LOCAL_AUTHORIZATION, LocalCaller
from starlette.concurrency import run_in_threadpool

from fakes.purge import FakePurgePages


def unavailable() -> None:
    raise PolicyUnavailable()


class ProposalHTTPReleaseScenarios:
    def original_ledger_outage(self: Any) -> Any:
        original = self.ledger.latest
        device = subject_key(self.tenant, "device", self.release_pi.sub)
        owner = subject_key(self.tenant, "membership", self.identity().user_id)
        failed, healthy = [], []

        def unavailable_source(subject: str) -> Any:
            if subject == device:
                failed.append(subject)
                raise LedgerError()
            result = original(subject)
            if subject == owner:
                healthy.append(subject)
            return result

        return unavailable_source, failed, healthy

    def proposal_snapshot(self: Any) -> Any:
        return self.store.run(
            lambda tx: {
                name: tx.collection(f"tenants/{self.tenant}/{name}")
                for name in (
                    "calendar_proposals",
                    "run_proposals",
                    "runs",
                    "jobs",
                    "idempotency_records",
                )
            }
        )

    def assert_retryable_proposal_error(self: Any, response: Any) -> None:
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["error"]["code"], "temporarily_unavailable")
        self.assertIs(response.json()["error"]["retryable"], True)
        self.assertNotIn("normalized_request", response.text)

    def test_original_device_ledger_outage_is_retryable_for_owner_reads_and_reject(
        self: Any,
    ) -> None:
        client, _, run = self.release_fixture()
        proposal = self.conversations.get_run(self.identity(), self.tenant, run).waiting_proposal_id
        root = f"/v1/tenants/{self.tenant}"
        url = root + f"/calendar-proposals/{proposal}"
        self.assertEqual(client.get(url).status_code, 200)
        before = self.proposal_snapshot()
        unavailable_source, failed, healthy = self.original_ledger_outage()
        headers = {"Idempotency-Key": str(uuid4())}
        with patch.object(self.ledger, "latest", unavailable_source):
            for response in (
                client.get(url),
                client.post(url + ":reject", headers=headers),
                client.get(root + f"/runs/{run}"),
            ):
                self.assert_retryable_proposal_error(response)
        self.assertEqual(len(failed), 3)
        self.assertTrue(healthy, "Current owner ledger checks must succeed")
        self.assertEqual(self.proposal_snapshot(), before)
        recovered = client.get(url)
        self.assertEqual(recovered.status_code, 200, recovered.text)
        self.assertEqual(recovered.json()["status"], "pending")
        decided = client.post(url + ":reject", headers=headers)
        self.assertEqual(decided.status_code, 200, decided.text)
        self.assertEqual(decided.json()["status"], "declined")
        self.release_advance()
        self.assertEqual(client.get(url).status_code, 404)

    def creation_source_outage(self: Any, *, replay: bool) -> None:
        client, _, _ = self.release_fixture()
        client.app.dependency_overrides[reader_identity] = lambda: self.release_pi
        url = f"/v1/tenants/{self.tenant}/integrations/{self.arguments['integration_id']}/calendar-proposals"
        headers = {"Idempotency-Key": str(uuid4()), "X-Private-Session": self.release_session}
        if replay:
            initial = client.post(url, json=self.arguments["change"], headers=headers)
            self.assertEqual(initial.status_code, 201, initial.text)
        before = self.proposal_snapshot()
        provider = self.proposals.events.reader.provider
        calls = len(provider.calls)
        unavailable_source, failed, healthy = self.original_ledger_outage()

        def unavailable_origin(*args: Any, **kwargs: Any) -> None:
            # The earlier current-caller check succeeded. Fail the real ledger
            # only while the saved/initial origin is synchronously consumed.
            with patch.object(self.ledger, "latest", unavailable_source):
                check_origin(*args, **kwargs)

        with patch.object(proposal_service, "check_origin", unavailable_origin):
            response = client.post(url, json=self.arguments["change"], headers=headers)
        self.assert_retryable_proposal_error(response)
        self.assertTrue(failed)
        self.assertEqual(self.proposal_snapshot(), before)
        self.assertEqual(len(provider.calls), calls if replay else calls + 1)
        recovered = client.post(url, json=self.arguments["change"], headers=headers)
        self.assertEqual(recovered.status_code, 201, recovered.text)
        if replay:
            self.assertEqual(recovered.json(), initial.json())
            self.assertEqual(len(provider.calls), calls)

    def test_initial_create_preserves_synchronous_source_ledger_unavailability(self: Any) -> None:
        self.creation_source_outage(replay=False)

    def test_create_replay_preserves_synchronous_source_ledger_unavailability(self: Any) -> None:
        self.creation_source_outage(replay=True)

    def delayed_http(self: Any, module: Any, action: Any, *, status: int = 404) -> Any:
        calls = []

        async def delayed(*args: Any, **kwargs: Any) -> Any:
            result = await run_in_threadpool(*args, **kwargs)
            calls.append(True)
            self.release_advance()
            return result

        with patch.object(module, "run_in_threadpool", delayed):
            response = action()
        self.assertEqual(len(calls), 1)
        self.assertEqual(response.status_code, status, response.text)
        self.assertNotIn("normalized_request", response.text)
        self.assertNotIn("checkpoint_version", response.text)
        return response

    def test_proposal_get_keeps_guard_after_http_threadpool(self: Any) -> None:
        client, _, run = self.release_fixture()
        proposal = self.conversations.get_run(self.identity(), self.tenant, run).waiting_proposal_id
        url = f"/v1/tenants/{self.tenant}/calendar-proposals/{proposal}"
        self.assertEqual(client.get(url).status_code, 200)
        self.delayed_http(proposal_routes, lambda: client.get(url))
        self.assertEqual(client.get(url).status_code, 404)

    def decision_release(self: Any, *, cancel: bool) -> None:
        client, _, run = self.release_fixture()
        proposal = self.conversations.get_run(self.identity(), self.tenant, run).waiting_proposal_id
        url = (
            f"/v1/tenants/{self.tenant}/runs/{run}:cancel"
            if cancel
            else f"/v1/tenants/{self.tenant}/calendar-proposals/{proposal}:reject"
        )
        headers = {"Idempotency-Key": str(uuid4())}
        self.delayed_http(
            conversation_routes if cancel else proposal_routes,
            lambda: client.post(url, headers=headers),
        )
        canonical = self.store.run(
            lambda tx: tx.get(f"tenants/{self.tenant}/calendar_proposals/{proposal}")
        )
        self.assertEqual(canonical["status"], "cancelled" if cancel else "declined")
        saved_run = self.store.run(lambda tx: tx.get(f"tenants/{self.tenant}/runs/{run}"))
        self.assertEqual(saved_run["status"], "cancelled" if cancel else "queued")
        before = self.jobs.get(self.tenant, job_id(self.tenant, KIND, run))
        self.assertIsNotNone(before)
        self.assertEqual(client.post(url, headers=headers).status_code, 404)
        self.assertEqual(
            client.post(url, headers={"Idempotency-Key": str(uuid4())}).status_code, 404
        )
        self.assertEqual(self.jobs.get(self.tenant, job_id(self.tenant, KIND, run)), before)
        self.assertEqual(
            self.store.run(
                lambda tx: tx.get(f"tenants/{self.tenant}/calendar_proposals/{proposal}")
            ),
            canonical,
        )
        # Durable decisions and receipts exist even though their late response was refused.
        receipts = self.store.run(
            lambda tx: tx.collection(f"tenants/{self.tenant}/idempotency_records")
        )
        self.assertTrue(
            any(
                d["result"].get("resource_id") == (run if cancel else proposal) for _, d in receipts
            )
        )

    def test_reject_late_response_keeps_durable_decision_without_second_resume(self: Any) -> None:
        self.decision_release(cancel=False)

    def test_cancel_late_response_keeps_durable_decision_without_second_cancel(self: Any) -> None:
        self.decision_release(cancel=True)

    def create_release(self: Any, *, replay: bool) -> None:
        client, _, _ = self.release_fixture()
        client.app.dependency_overrides[reader_identity] = lambda: self.release_pi
        integration = self.arguments["integration_id"]
        url = f"/v1/tenants/{self.tenant}/integrations/{integration}/calendar-proposals"
        headers = {"Idempotency-Key": str(uuid4()), "X-Private-Session": self.release_session}
        body = self.arguments["change"]
        provider = self.proposals.events.reader.provider
        before = self.store.run(
            lambda tx: tx.collection(f"tenants/{self.tenant}/calendar_proposals")
        )
        initial = None
        if replay:
            initial = client.post(url, json=body, headers=headers)
            self.assertEqual(initial.status_code, 201, initial.text)
            count = len(provider.calls)
            healthy = client.post(url, json=body, headers=headers)
            self.assertEqual(healthy.status_code, 201, healthy.text)
            self.assertEqual(healthy.json(), initial.json())
            self.assertEqual(len(provider.calls), count)
        count = len(provider.calls)
        # Pi is itself the current caller: its expired session is the existing 403
        # operation boundary, before the original-source 404 used for owner reads.
        self.delayed_http(
            proposal_routes, lambda: client.post(url, json=body, headers=headers), status=403
        )
        self.assertEqual(len(provider.calls), count if replay else count + 1)
        after = self.store.run(
            lambda tx: tx.collection(f"tenants/{self.tenant}/calendar_proposals")
        )
        created = [d for p, d in after if p not in {path for path, _ in before}]
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["status"], "pending")
        if initial is not None:
            self.assertEqual(created[0]["args_hash"], initial.json()["args_hash"])
        receipts = self.store.run(
            lambda tx: tx.collection(f"tenants/{self.tenant}/idempotency_records")
        )
        self.assertEqual(
            sum(d["result"].get("resource_id") == created[0]["id"] for _, d in receipts), 1
        )
        self.assertEqual(
            client.get(
                f"/v1/tenants/{self.tenant}/calendar-proposals/{created[0]['id']}"
            ).status_code,
            404,
        )

    def test_pi_new_create_keeps_guard_after_http_threadpool(self: Any) -> None:
        self.create_release(replay=False)

    def test_pi_same_key_replay_keeps_guard_without_provider_io(self: Any) -> None:
        self.create_release(replay=True)

    def export_release_fixture(self: Any) -> Any:
        client, _, run = self.release_fixture()
        proposal = self.conversations.get_run(self.identity(), self.tenant, run).waiting_proposal_id
        directory = Path(self.enterContext(tempfile.TemporaryDirectory())) / "calendar-exports"
        storage = LocalExportObjects(directory, b"x" * 32, now=self.jobs.now)
        pages = (
            FakePurgePages(self.store) if hasattr(self, "clock") else FirestorePurgePages(self.db)
        )
        exports = Exports(
            self.jobs,
            storage,
            pages,
            CanonicalSources(SavedMessages()),
            now=self.jobs.now,
            credential_epoch=self.tokens.epoch,
        )
        requests = ExportRequests(exports, self.tenants)
        operation = requests.create(
            self.identity(), self.tenant, ExportCreate(kind="export", scope="my_data"), str(uuid4())
        ).operation_id
        delivery = exports.jobs.prepare_dispatch(
            self.tenant, job_id(self.tenant, EXPORT, operation)
        )
        self.assertIsNotNone(delivery)
        assert delivery is not None
        Worker(exports.jobs, LocalCaller(), {EXPORT: ExportData(exports)}).handle(
            LOCAL_AUTHORIZATION, delivery.model_dump(mode="json")
        )
        registry = self.store.run(lambda tx: tx.get(registry_path(operation)))
        self.assertEqual(registry["state"], "ready")
        self.assertTrue(
            any(
                e["kind"] == "calendar_proposals" and e["resource_id"] == proposal
                for e in registry["manifest"]
            )
        )
        client.app.dependency_overrides[get_auth_services] = lambda: AuthServices(
            tenants=self.tenants, exports=requests
        )
        url = f"/v1/tenants/{self.tenant}/operations/{operation}/download"
        healthy = client.get(url)
        self.assertEqual(healthy.status_code, 200, healthy.text)
        self.assertTrue(healthy.content.startswith(b"PK"))
        return client, requests, operation, url, healthy.content

    def export_boundary(self: Any, *, after_headers: bool, outage: bool) -> None:
        client, requests, operation, url, content = self.export_release_fixture()
        calls = []
        boundary = 3 if after_headers else 1

        async def delayed(fn: Any, *args: Any, **kwargs: Any) -> Any:
            result = await run_in_threadpool(fn, *args, **kwargs)
            if fn.__name__ == "release":
                calls.append(True)
                if len(calls) == boundary:
                    if outage:
                        return ReleaseGuard((*result.checks, unavailable))
                    self.release_advance()
            return result

        with patch.object(download_routes, "run_in_threadpool", delayed):
            if not after_headers:
                response = client.get(url)
                self.assertEqual(response.status_code, 503 if outage else 404, response.text)
                self.assertNotIn("SYNTHETIC", response.text)
            else:
                response = ExportResponse(
                    content * 500, requests, self.identity(), self.tenant, operation
                )
                sent = []

                async def send(message: Any) -> None:
                    sent.append(message)

                async def receive() -> Any:
                    return {"type": "http.disconnect"}

                with self.assertRaises(APIError) as failure:
                    asyncio.run(response({}, receive, send))
                self.assertEqual(
                    failure.exception.code,
                    ErrorCode.TEMPORARILY_UNAVAILABLE if outage else ErrorCode.RESOURCE_NOT_FOUND,
                )
                self.assertEqual(
                    [m["status"] for m in sent if m["type"] == "http.response.start"], [200]
                )
                self.assertEqual(
                    [len(m["body"]) for m in sent if m["type"] == "http.response.body"], [65536]
                )
        self.assertEqual(len(calls), boundary)
        self.assertEqual(client.get(url).status_code, 200 if outage else 404)

    def test_calendar_export_header_expiry_is_404(self: Any) -> None:
        self.export_boundary(after_headers=False, outage=False)

    def test_calendar_export_header_unavailable_is_503(self: Any) -> None:
        self.export_boundary(after_headers=False, outage=True)

    def test_calendar_export_body_expiry_aborts_next_chunk(self: Any) -> None:
        self.export_boundary(after_headers=True, outage=False)

    def test_calendar_export_body_unavailable_aborts_next_chunk(self: Any) -> None:
        self.export_boundary(after_headers=True, outage=True)
