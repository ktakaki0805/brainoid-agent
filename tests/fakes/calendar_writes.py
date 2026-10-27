"""Synthetic mutation transport and CAL04 acceptance shared with real transactions."""

from collections.abc import Callable
from copy import deepcopy
from typing import Any
from unittest.mock import patch

from brainoid_api.services.calendar_proposals import CalendarConfirm, Proposals
from brainoid_contracts import FirebaseUid
from brainoid_contracts.errors import APIError
from brainoid_jobs import Jobs
from brainoid_jobs.models import job_id
from brainoid_memory.calendar_proposals import proposal_path
from brainoid_memory.calendar_writer import KIND, ProviderReply, event_id
from brainoid_memory.forget import operation_path
from brainoid_policy.models import UserSubject
from brainoid_runs.state import RunState
from brainoid_worker.calendar_write import CalendarWrite
from brainoid_worker.entry import Worker
from brainoid_worker.local import LocalCaller


class WriteProvider:
    def __init__(self) -> None:
        self.events: dict[str, Any] = {}
        self.calls: list[Any] = []
        self.after: Callable[[], None] = lambda: None
        self.lost = False
        self.rejected: int | None = None

    def inspect(self, token: Any, calendar: str, event: str) -> ProviderReply:
        value = self.events.get(event)
        return ProviderReply(404) if value is None else ProviderReply(200, deepcopy(value))

    def write(
        self, token: Any, action: str, calendar: str, event: str, etag: str | None, body: Any
    ) -> ProviderReply:
        self.calls.append((action, calendar, event, etag, deepcopy(body)))
        if self.rejected is not None:
            return ProviderReply(self.rejected)
        previous = self.events.get(event)
        if action == "create" and previous is not None:
            return ProviderReply(409)
        if action != "create" and (previous is None or previous.get("etag") != etag):
            return ProviderReply(412)
        if action == "delete":
            del self.events[event]
            reply = ProviderReply(204)
        else:
            value = {**(previous or {}), **deepcopy(body), "id": event, "etag": '"written-v4"'}
            self.events[event] = value
            reply = ProviderReply(200, deepcopy(value))
        self.after()
        if self.lost:
            raise TimeoutError("SYNTHETIC_POST_SUCCESS_LOSS")
        return reply


class WriteScenarios:
    def setup_writes(self: Any) -> None:
        self.proposals = Proposals(self.events, write_enabled=True)
        self.write_jobs = Jobs(
            self.reader.store,
            self.tenants._ledger,
            now=self.clock.now,
            allow_untracked_bootstrap=True,
        )
        self.writer_provider = WriteProvider()
        self.write_handler = CalendarWrite(
            self.write_jobs,
            self.reader,
            self.writer_provider,
            state=RunState(
                self.tenants._ledger,
                now=self.clock.now,
                credential_epoch=None,
                allow_untracked_bootstrap=True,
            ),
        )
        self.write_worker = Worker(self.write_jobs, LocalCaller(), {KIND: self.write_handler})

    def confirm_write(self: Any, proposal: Any, key: str = "calendar-confirm-fixture") -> Any:
        return self.proposals.confirm_for_release(
            self.identity(),
            self.tenant,
            proposal.proposal_id,
            CalendarConfirm(args_hash=proposal.args_hash, confirmed=True),
            key,
        ).release()

    def execute_write(self: Any, operation: Any) -> None:
        identity = job_id(self.tenant, KIND, operation.operation_id)
        delivery = self.write_jobs.prepare_dispatch(self.tenant, identity)
        if delivery is not None:
            self.write_worker.handle(
                "Bearer local-synthetic-worker", delivery.model_dump(mode="json")
            )

    def saved_operation(self: Any, operation: Any) -> Any:
        return self.store.run(
            lambda tx: tx.get(operation_path(self.tenant, operation.operation_id))
        )

    def test_confirm_atomic_different_keys_one_operation_job_and_no_inline_write(self: Any) -> None:
        self.setup_writes()
        proposal = self.propose()
        accepted = self.confirm_write(proposal)
        self.assertEqual(accepted, self.confirm_write(proposal))
        self.assertEqual(accepted, self.confirm_write(proposal, "calendar-another-confirm"))
        self.assertEqual(self.writer_provider.calls, [])
        jobs = self.store.run(lambda tx: tx.collection(f"tenants/{self.tenant}/jobs"))
        self.assertEqual(len([d for _, d in jobs if d["kind"] == KIND]), 1)
        self.clock.advance(301)
        self.assertEqual(self.confirm_write(proposal), accepted)
        self.execute_write(accepted)
        self.assertEqual(self.saved_operation(accepted)["operation"]["status"], "succeeded")
        self.execute_write(accepted)
        self.write_handler.reconcile(
            self.write_jobs.get(self.tenant, job_id(self.tenant, KIND, accepted.operation_id))
        )
        self.assertEqual(len(self.writer_provider.calls), 1)

    def test_hash_owner_expiry_and_unconfigured_confirm_fail_closed(self: Any) -> None:
        self.setup_writes()
        proposal = self.propose()
        with self.assertRaises(APIError):
            self.proposals.confirm_for_release(
                self.identity(),
                self.tenant,
                proposal.proposal_id,
                CalendarConfirm(args_hash="0" * 64, confirmed=True),
                "calendar-invalid-hash",
            )
        for uid in ("demo-a-owner", "demo-b-member"):
            with self.assertRaises(APIError):
                self.proposals.confirm_for_release(
                    self.identity(FirebaseUid(uid)),
                    self.tenant,
                    proposal.proposal_id,
                    CalendarConfirm(args_hash=proposal.args_hash, confirmed=True),
                    "calendar-other-owner",
                )
        with self.assertRaises(APIError):
            Proposals(self.events).confirm_for_release(
                self.identity(),
                self.tenant,
                proposal.proposal_id,
                CalendarConfirm(args_hash=proposal.args_hash, confirmed=True),
                "calendar-disabled-write",
            )
        with self.assertRaises(APIError):
            self.proposals.confirm_for_release(
                UserSubject(user_id=self.identity().user_id),
                self.tenant,
                proposal.proposal_id,
                CalendarConfirm(args_hash=proposal.args_hash, confirmed=True),
                "calendar-not-http-user",
            )
        self.clock.advance(300)
        with self.assertRaises(APIError):
            self.confirm_write(proposal)
        self.assertEqual(self.writer_provider.calls, [])

    def test_create_timeout_reconciles_marker_and_intent_without_second_write(self: Any) -> None:
        self.setup_writes()
        proposal = self.propose()
        accepted = self.confirm_write(proposal)
        self.writer_provider.lost = True
        self.execute_write(accepted)
        self.assertEqual(self.saved_operation(accepted)["operation"]["status"], "succeeded")
        self.assertEqual(len(self.writer_provider.calls), 1)
        self.assertEqual(
            self.writer_provider.calls[0][2],
            event_id(
                self.store.run(lambda tx: tx.get(proposal_path(self.tenant, proposal.proposal_id)))
            ),
        )

    def test_same_event_id_collision_is_conflict(self: Any) -> None:
        self.setup_writes()
        proposal = self.propose()
        accepted = self.confirm_write(proposal)
        saved = self.store.run(lambda tx: tx.get(proposal_path(self.tenant, proposal.proposal_id)))
        self.writer_provider.events[event_id(saved)] = {
            "id": event_id(saved),
            "summary": "somebody else",
        }
        self.execute_write(accepted)
        result = self.saved_operation(accepted)["operation"]
        self.assertEqual((result["status"], result["error_code"]), ("failed", "conflict"))

    def test_post_success_lease_loss_preserves_fact(self: Any) -> None:
        self.setup_writes()
        proposal = self.propose()
        accepted = self.confirm_write(proposal)
        self.writer_provider.after = lambda: self.clock.advance(61)
        self.execute_write(accepted)
        self.assertEqual(self.saved_operation(accepted)["operation"]["status"], "succeeded")
        self.assertEqual(len(self.writer_provider.calls), 1)

    def test_revoked_before_start_and_after_start_fence(self: Any) -> None:
        for started in (False, True):
            with self.subTest(started=started):
                self.setup_writes()
                proposal = self.propose(key="calendar-revoke-proposal-" + str(started))
                accepted = self.confirm_write(proposal, "calendar-revoke-confirm-" + str(started))

                def revoke() -> None:
                    path = f"tenants/{self.tenant}/integrations/{self.integration}"

                    def change(tx: Any) -> None:
                        record = tx.get(path)
                        tx.set(path, {**record, "status": "revoked"})

                    self.store.run(change)

                if started:
                    original = self.write_jobs.start_effect

                    def start(*args: Any, original: Any = original, **kwargs: Any) -> Any:
                        result = original(*args, **kwargs)
                        revoke()
                        return result

                    with patch.object(self.write_jobs, "start_effect", start):
                        self.execute_write(accepted)
                else:
                    revoke()
                    self.execute_write(accepted)
                self.assertEqual(self.writer_provider.calls, [])
                path = f"tenants/{self.tenant}/integrations/{self.integration}"

                def restore(tx: Any, path: str = path) -> None:
                    record = tx.get(path)
                    tx.set(path, {**record, "status": "active"})

                self.store.run(restore)

    def test_update_delete_etag_conflict_and_ambiguous_delete(self: Any) -> None:
        self.setup_writes()
        initial = deepcopy(self.events_provider.pages[None]["items"][0])
        for action, lost in (("update", True), ("delete", False), ("delete", True)):
            with self.subTest(action=action, lost=lost):
                self.writer_provider.events["opaque-event"] = {
                    **initial,
                    "colorId": "7",
                    "extendedProperties": {"private": {"other": "keep"}},
                }
                change = {
                    "calendar_id": self.body["calendar_id"],
                    "event_id": "opaque-event",
                    "provider_etag": '"provider-v3"',
                    "action": action,
                }
                if action == "update":
                    change = {**self.body, **change}
                proposal = self.propose(change, "calendar-target-" + action + str(lost))
                operation = self.confirm_write(
                    proposal, "calendar-target-confirm-" + action + str(lost)
                )
                self.writer_provider.lost = lost
                self.execute_write(operation)
                expected = "outcome_unknown" if action == "delete" and lost else "succeeded"
                self.assertEqual(self.saved_operation(operation)["operation"]["status"], expected)
                self.assertEqual(self.writer_provider.calls[-1][3], '"provider-v3"')
                if action == "update":
                    self.assertEqual(self.writer_provider.events["opaque-event"]["colorId"], "7")
                    self.assertEqual(
                        self.writer_provider.events["opaque-event"]["extendedProperties"][
                            "private"
                        ]["other"],
                        "keep",
                    )
        self.writer_provider.lost = False
        self.writer_provider.events["opaque-event"] = {**initial, "etag": '"someone-changed"'}
        proposal = self.propose(
            {
                **self.body,
                "action": "update",
                "event_id": "opaque-event",
                "provider_etag": '"provider-v3"',
            },
            "calendar-conflict-new",
        )
        operation = self.confirm_write(proposal, "calendar-conflict-confirm")
        count = len(self.writer_provider.calls)
        self.execute_write(operation)
        self.assertEqual(self.saved_operation(operation)["operation"]["error_code"], "conflict")
        self.assertEqual(len(self.writer_provider.calls), count)

    def test_confirm_rejects_immutable_tampering(self: Any) -> None:
        self.setup_writes()
        proposal = self.propose()
        path = proposal_path(self.tenant, proposal.proposal_id)
        original = self.store.run(lambda tx: tx.get(path))
        mutations = [
            {**original, "owner_user_id": "other"},
            {**original, "args_hash": "0" * 64},
            {**original, "expires_at": "2099-01-01T00:00:00Z"},
            *[
                {**original, "normalized_request": {**original["normalized_request"], field: value}}
                for field, value in (
                    ("summary", "changed"),
                    ("calendar_id", "other"),
                    ("event_id", "other"),
                    ("provider_etag", "other"),
                )
            ],
        ]
        for changed in mutations:
            self.store.run(lambda tx, changed=changed: tx.set(path, changed))
            with self.assertRaises(APIError):
                self.confirm_write(proposal)
        self.store.run(lambda tx: tx.set(path, original))
        self.assertEqual(self.writer_provider.calls, [])

    def test_acceptance_response_loss_and_result_commit_response_loss(self: Any) -> None:
        from brainoid_contracts.idempotency import Idempotency
        from brainoid_persistence.firestore import PersistenceUnavailable

        self.setup_writes()
        proposal = self.propose()
        execute = Idempotency.execute

        def lost(instance: Any, **kwargs: Any) -> Any:
            execute(instance, **kwargs)
            raise PersistenceUnavailable()

        with patch.object(Idempotency, "execute", lost), self.assertRaises(PersistenceUnavailable):
            self.confirm_write(proposal)
        accepted = self.confirm_write(proposal)
        record = self.write_handler.record
        triggered: list[bool] = []

        def lost_result(*args: Any, **kwargs: Any) -> None:
            record(*args, **kwargs)
            if args[1] == "succeeded" and not triggered:
                triggered.append(True)
                raise PersistenceUnavailable()

        with (
            patch.object(self.write_handler, "record", lost_result),
            self.assertRaises(PersistenceUnavailable),
        ):
            self.execute_write(accepted)
        self.assertEqual(triggered, [True])
        self.assertEqual(self.saved_operation(accepted)["operation"]["status"], "succeeded")
        self.write_handler.recover(self.tenant, self.store)
        self.assertEqual(len(self.writer_provider.calls), 1)

    def test_unknown_never_reissues_mutation_and_requires_matching_intent(self: Any) -> None:
        self.setup_writes()
        proposal = self.propose()
        accepted = self.confirm_write(proposal)
        self.writer_provider.lost = True

        def changed() -> None:
            event = next(iter(self.writer_provider.events.values()))
            event["summary"] = "later change with same id and marker"

        self.writer_provider.after = changed
        self.execute_write(accepted)
        self.assertEqual(self.saved_operation(accepted)["operation"]["status"], "outcome_unknown")
        self.write_handler.recover(self.tenant, self.store)
        self.execute_write(accepted)
        self.assertEqual(len(self.writer_provider.calls), 1)
        self.assertEqual(self.saved_operation(accepted)["operation"]["status"], "outcome_unknown")

    def test_post_success_revocation_retains_fact_and_blocks_private_get(self: Any) -> None:
        self.setup_writes()
        proposal = self.propose()
        accepted = self.confirm_write(proposal)

        def revoke() -> None:
            path = f"tenants/{self.tenant}/integrations/{self.integration}"

            def update(tx: Any) -> None:
                record = tx.get(path)
                tx.set(path, {**record, "status": "revoked"})

            self.store.run(update)

        self.writer_provider.after = revoke
        self.execute_write(accepted)
        self.assertEqual(self.saved_operation(accepted)["operation"]["status"], "succeeded")
        with self.assertRaises(APIError):
            self.proposals.operation_for_release(
                self.identity(), self.tenant, accepted.operation_id
            ).release()
        self.assertEqual(len(self.writer_provider.calls), 1)

    def test_abrupt_stop_without_finally_recovery_after_dispatch_refuses_started_job(
        self: Any,
    ) -> None:
        self.setup_writes()
        proposal = self.propose()
        accepted = self.confirm_write(proposal)

        def crash() -> None:
            raise SystemExit("synthetic abrupt process exit")

        self.writer_provider.after = crash
        with (
            patch.object(self.write_handler, "reconcile", return_value=None),
            self.assertRaises(SystemExit),
        ):
            self.execute_write(accepted)
        self.assertEqual(self.saved_operation(accepted)["operation"]["status"], "running")
        self.clock.advance(61)
        self.assertIsNone(
            self.write_jobs.prepare_dispatch(
                self.tenant, job_id(self.tenant, KIND, proposal.proposal_id)
            )
        )
        restarted = CalendarWrite(
            self.write_jobs, self.reader, self.writer_provider, state=self.write_handler.state
        )
        restarted.recover(self.tenant, self.store, limit=1)
        self.assertEqual(self.saved_operation(accepted)["operation"]["status"], "succeeded")
        restarted.recover(self.tenant, self.store, limit=1)
        self.assertEqual(len(self.writer_provider.calls), 1)

    def test_http_confirm_schema_operation_route_and_no_store(self: Any) -> None:
        from brainoid_api.auth.runtime import AuthServices, get_auth_services

        self.setup_writes()
        proposal = self.propose()
        with self.events_client() as client:
            client.app.dependency_overrides[get_auth_services] = lambda: AuthServices(
                tenants=self.tenants,
                calendar=self.service,
                calendar_events=self.events,
                calendar_write_enabled=True,
            )
            path = f"/v1/tenants/{self.tenant}/calendar-proposals/{proposal.proposal_id}:confirm"
            content = {"args_hash": proposal.args_hash, "confirmed": True}
            for invalid in (
                {**content, "confirmed": 1},
                {**content, "confirmed": False},
                {**content, "run_id": proposal.proposal_id},
                {**content, "change": self.body},
            ):
                self.assertEqual(
                    client.post(
                        path, json=invalid, headers={"Idempotency-Key": "calendar-http-invalid"}
                    ).status_code,
                    422,
                )
            result = client.post(
                path, json=content, headers={"Idempotency-Key": "calendar-http-confirm"}
            )
            self.assertEqual(result.status_code, 202, result.text)
            self.assertEqual(result.headers["cache-control"], "no-store")
            fetched = client.get(f"/v1/tenants/{self.tenant}/operations/{proposal.proposal_id}")
            self.assertEqual(fetched.status_code, 200, fetched.text)
            self.assertEqual(fetched.json(), result.json())
            self.assertNotIn("SYNTHETIC_PRIVATE", fetched.text)

    def test_checkpoint_pre_send_death_closes_via_public_recovery(self: Any) -> None:
        self.pre_send_death(dispatch=True)

    def test_checkpoint_pre_send_death_recovers_without_dispatcher(self: Any) -> None:
        self.pre_send_death(dispatch=False)

    def pre_send_death(self: Any, *, dispatch: bool) -> None:
        self.setup_writes()
        proposal = self.propose()
        accepted = self.confirm_write(proposal)
        start_effect = self.write_jobs.start_effect
        reached: list[bool] = []

        def crash(*args: Any, **kwargs: Any) -> Any:
            start_effect(*args, **kwargs)
            reached.append(True)
            raise SystemExit("synthetic death after checkpoint before business start")

        with (
            patch.object(self.write_jobs, "start_effect", crash),
            patch.object(self.write_handler, "reconcile", return_value=None),
            self.assertRaises(SystemExit),
        ):
            self.execute_write(accepted)
        self.assertEqual(reached, [True])
        self.assertIsNone(self.saved_operation(accepted).get("started_at"))
        # Recovery must not steal the still-live generation's ability to send.
        self.write_handler.recover(self.tenant, self.store)
        self.assertEqual(self.saved_operation(accepted)["operation"]["status"], "pending")
        self.clock.advance(61)
        if dispatch:
            self.assertIsNone(
                self.write_jobs.prepare_dispatch(
                    self.tenant, job_id(self.tenant, KIND, proposal.proposal_id)
                )
            )
        for _ in range(2):
            self.write_handler.recover(self.tenant, self.store)
        operation = self.saved_operation(accepted)["operation"]
        self.assertEqual((operation["status"], operation["error_code"]), ("failed", "not_started"))
        self.assertEqual(self.writer_provider.calls, [])

    def test_unstarted_retry_exhaustion_is_terminal_but_pending_retry_is_not(self: Any) -> None:
        from brainoid_contracts.errors import ErrorCode

        self.setup_writes()
        proposal = self.propose()
        accepted = self.confirm_write(proposal)
        identity = job_id(self.tenant, KIND, proposal.proposal_id)
        self.write_handler.recover(self.tenant, self.store)
        self.assertEqual(self.saved_operation(accepted)["operation"]["status"], "pending")
        with patch.object(
            self.reader, "token", side_effect=APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)
        ) as token:
            for attempt in range(5):
                self.execute_write(accepted)
                self.write_handler.recover(self.tenant, self.store)
                expected = "failed" if attempt == 4 else "pending"
                self.assertEqual(self.saved_operation(accepted)["operation"]["status"], expected)
                self.clock.advance(6)
            self.assertEqual(token.call_count, 5)
        job = self.write_jobs.get(self.tenant, identity)
        self.assertEqual((job.status, job.reason), ("failed", "attempts_exhausted"))
        self.assertEqual(self.saved_operation(accepted)["operation"]["error_code"], "not_started")
        self.assertIsNone(self.saved_operation(accepted).get("started_at"))
        self.assertEqual(self.writer_provider.calls, [])

    def composed_memories(self: Any) -> Any:
        from brainoid_api.repositories.memories import Memories
        from brainoid_memory.deletion_ledger import DeletionLedger

        return Memories(
            self.store,
            self.tenants,
            self.tenants._cursors,
            ledger=self.ledger,
            now=self.clock.now,
            allow_untracked_bootstrap=True,
            deletion_ledger=DeletionLedger(self.objects),
        )

    def test_operation_http_source_denials_never_fall_through(self: Any) -> None:
        from brainoid_api.auth.runtime import AuthServices, get_auth_services
        from brainoid_policy.control_ledger import LedgerError

        self.setup_writes()
        proposal = self.propose()
        self.confirm_write(proposal)
        memories = self.composed_memories()
        source = proposal_path(self.tenant, proposal.proposal_id)
        original = self.store.run(lambda tx: tx.get(source))
        with self.events_client() as client:
            client.app.dependency_overrides[get_auth_services] = lambda: AuthServices(
                tenants=self.tenants,
                calendar=self.service,
                calendar_events=self.events,
                memories=memories,
            )
            url = f"/v1/tenants/{self.tenant}/operations/{proposal.proposal_id}"
            self.assertEqual(client.get(url).status_code, 200)
            for value in (None, {**original, "suppress_reuse": True}):
                self.store.run(
                    lambda tx, value=value: (
                        tx.delete(source) if value is None else tx.set(source, value)
                    )
                )
                with patch.object(memories, "operation", wraps=memories.operation) as fallback:
                    response = client.get(url)
                    self.assertEqual(response.status_code, 404, response.text)
                    fallback.assert_not_called()
            self.store.run(lambda tx: tx.set(source, original))
            verify_origin = Proposals.verify_origin

            def unavailable_origin(instance: Any, tx: Any, record: Any) -> None:
                # Fail the saved producer/source fence after the HTTP identity
                # and current integration have already passed their own checks.
                with patch.object(self.ledger, "latest", side_effect=LedgerError()):
                    verify_origin(instance, tx, record)

            with (
                patch.object(Proposals, "verify_origin", unavailable_origin),
                patch.object(memories, "operation", wraps=memories.operation) as fallback,
            ):
                self.assertEqual(client.get(url).status_code, 503)
                fallback.assert_not_called()
            client.app.dependency_overrides[get_auth_services] = lambda: AuthServices(
                tenants=self.tenants, memories=memories
            )
            response = client.get(url)
            self.assertEqual(response.status_code, 404, response.text)
            self.assertNotIn("calendar_write", response.text)

    def test_refined_success_clears_public_proposal_and_export_error(self: Any) -> None:
        from brainoid_memory.export_projection import project

        self.setup_writes()
        proposal = self.propose()
        accepted = self.confirm_write(proposal)
        originals: list[Any] = []

        def changed() -> None:
            event = next(iter(self.writer_provider.events.values()))
            originals.append(deepcopy(event))
            event["summary"] = "SYNTHETIC_POST_WRITE_CHANGE"

        self.writer_provider.after = changed
        self.writer_provider.lost = True
        self.execute_write(accepted)
        self.assertEqual(self.saved_operation(accepted)["operation"]["status"], "outcome_unknown")
        self.writer_provider.events[originals[0]["id"]] = originals[0]
        self.write_handler.recover(self.tenant, self.store)
        public = self.proposals.get(self.identity(), self.tenant, proposal.proposal_id)
        self.assertEqual(public.status, "succeeded")
        self.assertIsNone(public.error_code)
        self.assertNotIn("error_code", public.model_dump(mode="json"))
        self.assertNotIn("error_code", self.saved_operation(accepted)["operation"])
        memories = self.composed_memories()

        def projection(tx: Any) -> Any:
            access = memories._open(tx, self.identity(), self.tenant, None)
            return project(
                tx, access, proposal_path(self.tenant, proposal.proposal_id), scope="private"
            )

        exported = memories.store.run(projection)
        self.assertIsNotNone(exported)
        self.assertEqual(exported[1]["status"], "succeeded")
        self.assertNotIn("error_code", exported[1])
        with self.events_client() as client:
            response = client.get(
                f"/v1/tenants/{self.tenant}/calendar-proposals/{proposal.proposal_id}"
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertNotIn("error_code", response.json())
