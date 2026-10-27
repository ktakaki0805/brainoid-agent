"""CAL05 acceptance shared by strict transactions and real Firestore Emulator."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from typing import Any, cast
from unittest.mock import patch
from uuid import uuid4

from brainoid_api.auth.runtime import AuthServices, get_auth_services
from brainoid_api.services.calendar_disconnect import CalendarDisconnect
from brainoid_contracts import FirebaseUid, ResourceId
from brainoid_contracts.errors import APIError
from brainoid_jobs import Jobs
from brainoid_jobs.models import job_id
from brainoid_memory.calendar_credentials import CalendarCredentialsRejected
from brainoid_memory.calendar_oauth import SecretUnavailable
from brainoid_memory.calendar_reader import integration_path
from brainoid_memory.calendar_writer import ProviderReply
from brainoid_memory.deletion_ledger import DeletionLedger
from brainoid_memory.forget import operation_path
from brainoid_memory.lifecycle import KIND
from brainoid_policy.control_ledger import LedgerError
from brainoid_worker.entry import Worker
from brainoid_worker.lifecycle_handlers import LifecycleHandlers
from brainoid_worker.local import LocalCaller
from brainoid_worker.purge_authority import PurgeJobs
from brainoid_worker.purge_resource import NoExternalCopies, PurgeResource
from fastapi import FastAPI

from fakes.calendar_writes import WriteScenarios


class DisconnectScenarios:
    def setup_disconnect(self: Any) -> None:
        self.deletion_ledger = DeletionLedger(self.objects)
        self.disconnects = CalendarDisconnect(self.service, self.deletion_ledger)

    def disconnect(self: Any, key: str = "calendar-disconnect-key", version: int = 2) -> Any:
        return self.disconnects.disconnect(
            self.identity(), self.tenant, self.integration, key, f'"{version}"'
        )

    def drain_disconnect(self: Any, operation: ResourceId | None = None) -> None:
        self.handler = PurgeResource(
            self.purge_jobs,
            self.purge_pages,
            handlers=LifecycleHandlers(),
            hooks={
                "cache": NoExternalCopies(),
                "export": getattr(self, "calendar_exports", NoExternalCopies()),
                "calendar": self.secrets,
            },
            page_size=3,
            step_budget=20000,
        )
        worker = Worker(self.purge_jobs, LocalCaller(), {KIND: self.handler})
        if operation is None:
            record = self.get(integration_path(self.tenant, self.integration))
            operation = ResourceId(record["disconnect_operation_id"])
        identity = job_id(self.tenant, KIND, operation)
        self.purge_jobs.activate(self.tenant, identity)
        delivery = self.purge_jobs.prepare_dispatch(self.tenant, identity, queue_name=KIND)
        assert delivery is not None
        worker.handle("Bearer local-synthetic-worker", delivery.model_dump(mode="json"))

    def prepare_purge(self: Any) -> None:
        self.purge_jobs = PurgeJobs(
            Jobs(self.store, self.ledger, now=self.clock.now, allow_untracked_bootstrap=True),
            self.deletion_ledger,
        )

    def test_disconnect_http_metadata_owner_etag_replay_and_completion(self: Any) -> None:
        with self.events_client() as client:
            cast(FastAPI, client.app).dependency_overrides[get_auth_services] = lambda: (
                AuthServices(
                    tenants=self.tenants,
                    calendar=self.service,
                    calendar_events=self.events,
                    calendar_disconnect=self.disconnects,
                )
            )
            url = f"/v1/tenants/{self.tenant}/integrations/{self.integration}"
            for headers, code in (
                ({}, 428),
                ({"If-Match": '"1"', "Idempotency-Key": "calendar-disconnect-http"}, 412),
            ):
                self.assertEqual(client.delete(url, headers=headers).status_code, code)
            headers = {"If-Match": '"2"', "Idempotency-Key": "calendar-disconnect-http"}
            accepted = client.delete(url, headers=headers)
            self.assertEqual(accepted.status_code, 202, accepted.text)
            self.assertEqual(accepted.json()["status"], "revoked")
            self.assertEqual(accepted.json()["cleanup_status"], "pending")
            self.assertEqual(accepted.json()["stop_reason"], "user_disconnected")
            self.assertEqual(accepted.json()["provider_revocation"], "not_attempted_scope_unsafe")
            self.assertTrue(accepted.json()["reconnect_allowed"])
            self.assertEqual(client.delete(url, headers=headers).json(), accepted.json())
            self.assertEqual(client.get(url).headers["etag"], '"3"')
            for secret in (
                "secret_ref",
                "authorization_id",
                "SYNTHETIC_ACCESS",
                "SYNTHETIC_REFRESH",
            ):
                self.assertNotIn(secret, accepted.text)
            self.prepare_purge()
            self.drain_disconnect()
            current = client.get(url)
            self.assertEqual(current.json()["cleanup_status"], "complete", current.text)
            self.assertEqual(client.delete(url, headers=headers).status_code, 204)

    def test_disconnect_does_not_authorize_another_owner_or_tenant(self: Any) -> None:
        for user in ("demo-a-owner", "demo-b-member"):
            with self.assertRaises(APIError):
                self.disconnects.disconnect(
                    self.identity(FirebaseUid(user)),
                    self.tenant,
                    self.integration,
                    "calendar-disconnect-other",
                    '"2"',
                )
        other = ResourceId("00000000-0000-0000-0000-000000000002")
        with self.assertRaises(APIError):
            self.disconnects.get(self.identity(), other, self.integration)
        self.assertEqual(
            self.get(integration_path(self.tenant, self.integration))["status"], "active"
        )

    def test_disconnect_stops_pending_confirm_old_jobs_and_new_reads(self: Any) -> None:
        WriteScenarios.setup_writes(self)
        pending = self.propose()
        confirmed = self.propose(key="calendar-disconnect-confirmed")
        operation = WriteScenarios.confirm_write(self, confirmed)
        before = len(self.events_provider.calls)
        self.disconnect()
        with self.assertRaises(APIError):
            WriteScenarios.confirm_write(self, pending)
        with self.assertRaises(APIError):
            self.events.list(self.identity(), self.tenant, self.integration, self.query)
        WriteScenarios.execute_write(self, operation)
        self.assertEqual(self.writer_provider.calls, [])
        self.assertEqual(len(self.events_provider.calls), before)

    def test_disconnect_between_secret_access_and_send_and_late_read(self: Any) -> None:
        get = self.secrets.get

        def stop(reference: str) -> bytes:
            value = cast(bytes, get(reference))
            self.disconnect()
            return value

        before = len(self.events_provider.calls)
        with patch.object(self.secrets, "get", side_effect=stop), self.assertRaises(APIError):
            self.events.list(self.identity(), self.tenant, self.integration, self.query)
        self.assertEqual(len(self.events_provider.calls), before)

    def test_disconnect_late_read_drops_body_and_confirm_race_never_sends(self: Any) -> None:
        WriteScenarios.setup_writes(self)
        proposal = self.propose()
        ready = Event()

        def confirm() -> Any:
            ready.wait(10)
            try:
                return WriteScenarios.confirm_write(self, proposal)
            except APIError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            confirmation = pool.submit(confirm)
            ready.set()
            self.disconnect()
            operation = confirmation.result(20)
        if operation is not None:
            WriteScenarios.execute_write(self, operation)
        self.assertEqual(self.writer_provider.calls, [])

    def test_disconnect_in_provider_read_drops_late_result(self: Any) -> None:
        self.events_provider.after = lambda: self.disconnect()
        before = len(self.events_provider.calls)
        with self.assertRaises(APIError):
            self.events.list(self.identity(), self.tenant, self.integration, self.query)
        self.assertEqual(len(self.events_provider.calls), before + 1)
        with self.assertRaises(APIError):
            self.events.list(self.identity(), self.tenant, self.integration, self.query)
        self.assertEqual(len(self.events_provider.calls), before + 1)

    def test_disconnect_expired_write_and_inspect_normalize_without_retries(self: Any) -> None:
        WriteScenarios.setup_writes(self)
        proposal = self.propose()
        operation = WriteScenarios.confirm_write(self, proposal)
        self.writer_provider.rejected = 401
        WriteScenarios.execute_write(self, operation)
        self.assertEqual(
            self.disconnects.get(self.identity(), self.tenant, self.integration).status,
            "reauth_required",
        )
        self.assertEqual(len(self.writer_provider.calls), 1)
        WriteScenarios.execute_write(self, operation)
        self.assertEqual(len(self.writer_provider.calls), 1)

    def test_disconnect_ambiguous_started_write_keeps_unknown_without_inspecting_revoked_grant(
        self: Any,
    ) -> None:
        WriteScenarios.setup_writes(self)
        proposal = self.propose()
        operation = WriteScenarios.confirm_write(self, proposal)
        self.writer_provider.lost = True
        self.writer_provider.after = lambda: self.disconnect()
        with patch.object(
            self.writer_provider, "inspect", return_value=ProviderReply(401)
        ) as inspect:
            WriteScenarios.execute_write(self, operation)
            self.assertEqual(inspect.call_count, 0)
        self.assertEqual(
            self.get(operation_path(self.tenant, operation.operation_id))["operation"]["status"],
            "outcome_unknown",
        )
        self.prepare_purge()
        self.drain_disconnect()
        self.assertEqual(
            self.get(operation_path(self.tenant, operation.operation_id))["operation"]["status"],
            "outcome_unknown",
        )

    def test_disconnect_late_provider_success_preserves_observed_write(self: Any) -> None:
        WriteScenarios.setup_writes(self)
        proposal = self.propose()
        operation = WriteScenarios.confirm_write(self, proposal)
        self.writer_provider.after = lambda: self.disconnect()
        WriteScenarios.execute_write(self, operation)
        saved = self.get(operation_path(self.tenant, operation.operation_id))
        self.assertEqual(saved["operation"]["status"], "succeeded")
        self.assertEqual(len(self.writer_provider.calls), 1)
        self.prepare_purge()
        self.drain_disconnect()
        self.assertEqual(
            self.get(operation_path(self.tenant, operation.operation_id))["operation"]["status"],
            "succeeded",
        )

    def test_disconnect_same_user_multiple_integrations_new_registration_and_slot_fences(
        self: Any,
    ) -> None:
        original = self.get(integration_path(self.tenant, self.integration))
        auth = self.get(
            f"tenants/{self.tenant}/calendar_authorizations/{original['authorization_id']}"
        )
        other = next(record for record in self.records() if record["id"] != self.integration)
        self.disconnect()
        state, _ = self.start("calendar-disconnect-reconnect")
        self.service.callback(state, "synthetic-new-code", None)
        new = next(
            record
            for record in self.records()
            if record["id"] not in (self.integration, other["id"])
        )
        self.prepare_purge()
        self.drain_disconnect()
        self.secrets.get(other["secret_ref"])
        self.secrets.get(new["secret_ref"])
        self.assertEqual(
            self.get(integration_path(self.tenant, ResourceId(new["id"])))["status"], "active"
        )
        with self.assertRaises(SecretUnavailable):
            self.secrets.get(original["secret_ref"])
        with self.assertRaises(SecretUnavailable):
            self.secrets.put(
                ResourceId(auth["token_slot"]),
                tenant=self.tenant,
                user=self.uid,
                binding=auth["secret_binding"],
                content=b"SYNTHETIC_LATE",
                expires_at=None,
            )

    def test_disconnect_preserves_other_user_and_same_user_other_tenant_secrets(self: Any) -> None:
        original_tenant, original_user = self.tenant, self.uid
        state, own_auth = self.start("calendar-multi-tenant-before")
        self.service.callback(state, "synthetic-code", None)
        own_record = self.get(f"tenants/{self.tenant}/calendar_authorizations/{own_auth}")
        self.uid = FirebaseUid("demo-a-owner")
        state, other_auth = self.start("calendar-other-owner-before")
        self.service.callback(state, "synthetic-code", None)
        other_owner = self.get(f"tenants/{self.tenant}/calendar_authorizations/{other_auth}")
        self.uid = original_user
        other_tenant = next(
            ResourceId(path.split("/")[1])
            for path, _ in self.store.run(lambda tx: tx.collection("tenants"))
            if path != f"tenants/{original_tenant}"
        )
        member = self.store.run(
            lambda tx: tx.query(
                f"tenants/{original_tenant}/memberships", {"user_id": original_user}
            )
        )[0][1]
        self.put(
            f"tenants/{other_tenant}/memberships/{uuid4()}", {**member, "tenant_id": other_tenant}
        )
        self.tenant = other_tenant
        state, other_tenant_auth = self.start("calendar-other-tenant-before")
        self.service.callback(state, "synthetic-code", None)
        other = self.get(f"tenants/{other_tenant}/calendar_authorizations/{other_tenant_auth}")
        self.tenant = original_tenant
        self.disconnect()
        self.prepare_purge()
        self.drain_disconnect()
        for record in (own_record, other_owner, other):
            self.secrets.get(record["secret_ref"])
            self.assertEqual(
                self.get(f"tenants/{record['tenant_id']}/calendar_authorizations/{record['id']}")[
                    "status"
                ],
                "connected",
            )

    def test_disconnect_expired_prewrite_inspect_never_starts_mutation(self: Any) -> None:
        WriteScenarios.setup_writes(self)
        proposal = self.propose(
            {
                **self.body,
                "action": "update",
                "event_id": "opaque-event",
                "provider_etag": '"provider-v3"',
            }
        )
        operation = WriteScenarios.confirm_write(self, proposal)
        with patch.object(
            self.writer_provider, "inspect", return_value=ProviderReply(401)
        ) as inspect:
            WriteScenarios.execute_write(self, operation)
            self.assertEqual(inspect.call_count, 1)
        self.assertEqual(self.writer_provider.calls, [])
        self.assertEqual(
            self.disconnects.get(self.identity(), self.tenant, self.integration).status,
            "reauth_required",
        )

    def test_disconnect_external_secret_failure_keeps_pending_after_canonical_erasure(
        self: Any,
    ) -> None:
        original = self.get(integration_path(self.tenant, self.integration))
        self.disconnect()
        self.prepare_purge()
        with patch.object(self.secrets, "invalidate", side_effect=SecretUnavailable()):
            self.drain_disconnect()
        self.assertIsNone(
            self.store.run(
                lambda tx: tx.get(
                    f"tenants/{self.tenant}/calendar_authorizations/{original['authorization_id']}"
                )
            )
        )
        self.assertEqual(
            self.disconnects.get(self.identity(), self.tenant, self.integration).cleanup_status,
            "pending",
        )
        self.secrets.get(original["secret_ref"])
        self.clock.advance(60)
        self.drain_disconnect()
        self.assertEqual(
            self.disconnects.get(self.identity(), self.tenant, self.integration).cleanup_status,
            "complete",
        )
        with self.assertRaises(SecretUnavailable):
            self.secrets.get(original["secret_ref"])

    def test_disconnect_ledger_failure_recovery_never_reopens_integration(self: Any) -> None:
        with (
            patch.object(self.deletion_ledger, "append", side_effect=LedgerError()),
            self.assertRaises(APIError),
        ):
            self.disconnect()
        self.assertEqual(
            self.disconnects.get(self.identity(), self.tenant, self.integration).cleanup_status,
            "pending",
        )
        with self.assertRaises(APIError):
            self.events.list(self.identity(), self.tenant, self.integration, self.query)
        self.disconnect()
        self.prepare_purge()
        self.drain_disconnect()
        self.assertEqual(
            self.disconnects.get(self.identity(), self.tenant, self.integration).cleanup_status,
            "complete",
        )

    def test_disconnect_exact_missing_authorization_children_and_unrelated_control_ghost(
        self: Any,
    ) -> None:
        original = self.get(integration_path(self.tenant, self.integration))
        self.disconnect()
        auth_path = f"tenants/{self.tenant}/calendar_authorizations/{original['authorization_id']}"
        child = auth_path + "/synthetic_children/opaque"
        unrelated = f"tenants/{self.tenant}/operations/{uuid4()}/synthetic_children/opaque"
        self.put(child, {"tenant_id": self.tenant, "body": "SYNTHETIC_ACCEPTED_CHILD"})
        self.put(unrelated, {"body": "SYNTHETIC_UNRELATED_CONTROL_CHILD"})
        self.store.run(lambda tx: tx.delete(auth_path))
        self.prepare_purge()
        self.drain_disconnect()
        self.assertIsNone(self.store.run(lambda tx: tx.get(child)))
        self.assertEqual(self.get(unrelated)["body"], "SYNTHETIC_UNRELATED_CONTROL_CHILD")
        with self.assertRaises(SecretUnavailable):
            self.secrets.get(original["secret_ref"])
        self.assertEqual(
            self.disconnects.get(self.identity(), self.tenant, self.integration).cleanup_status,
            "complete",
        )

    def test_disconnect_expiry_stops_repeated_calls_and_old_failure_cannot_stop_new_version(
        self: Any,
    ) -> None:
        def expired(*args: Any) -> Any:
            raise CalendarCredentialsRejected("credentials_expired_or_revoked")

        with patch.object(self.events_provider, "events", side_effect=expired) as send:
            for _ in range(2):
                with self.assertRaises(APIError):
                    self.events.list(self.identity(), self.tenant, self.integration, self.query)
            self.assertEqual(send.call_count, 1)
        current = self.disconnects.get(self.identity(), self.tenant, self.integration)
        self.assertEqual(current.status, "reauth_required")
        self.assertEqual(current.stop_reason, "credentials_expired_or_revoked")
        self.assertEqual(current.cleanup_status, "not_requested")
        old = self.get(integration_path(self.tenant, self.integration))
        fresh = {**old, "status": "active", "version": old["version"] + 1, "stop_reason": None}
        self.put(integration_path(self.tenant, self.integration), fresh)
        self.reader.reauthentication(old, "permission_revoked")
        self.assertEqual(self.get(integration_path(self.tenant, self.integration)), fresh)
        self.assertNotIn("SYNTHETIC", json.dumps(current.model_dump(mode="json")))
