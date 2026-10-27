"""Run-derived CAL04 confirmations through real producers and the normal worker."""

from typing import Any
from unittest.mock import patch

from brainoid_api.services.calendar_proposals import CalendarConfirm, Proposals
from brainoid_contracts import ResourceId
from brainoid_jobs.models import job_id
from brainoid_memory.calendar_proposals import proposal_path
from brainoid_memory.calendar_writer import KIND
from brainoid_memory.forget import operation_path
from brainoid_worker.calendar_write import CalendarWrite
from brainoid_worker.entry import Worker
from brainoid_worker.local import LOCAL_AUTHORIZATION, LocalCaller
from brainoid_worker.resume_run import Confirmations

from fakes.calendar_writes import WriteProvider


class RunWriteScenarios:
    def writes(self: Any) -> None:
        self.proposals = Proposals(self.proposals.events, self.conversations, write_enabled=True)
        self.write_provider = WriteProvider()
        self.write_handler = CalendarWrite(
            self.jobs,
            self.proposals.events.reader,
            self.write_provider,
            state=self.handler.state,
            confirmations=Confirmations(
                self.jobs,
                self.handler.state,
                registry=self.handler.tool_registry,
                result_authorizer=self.handler.execution_authorizer,
            ),
        )
        self.write_worker = Worker(self.jobs, LocalCaller(), {KIND: self.write_handler})

    def write_read(self: Any, path: str) -> Any:
        return self.jobs.store.run(lambda tx: tx.get(path))

    def write_start(self: Any) -> tuple[Any, Any]:
        result = self.start_proposal()
        if isinstance(result, tuple):
            return result
        return result, self.proposal(result)

    def write_confirm(self: Any, identity: Any) -> Any:
        record = self.write_read(proposal_path(self.tenant, identity))
        return self.proposals.confirm_for_release(
            self.identity(),
            self.tenant,
            identity,
            CalendarConfirm(args_hash=record["args_hash"], confirmed=True),
            "calendar-run-confirm-fixture",
        ).release()

    def write_execute(self: Any, operation: Any) -> None:
        delivery = self.jobs.prepare_dispatch(
            self.tenant, job_id(self.tenant, KIND, operation.operation_id)
        )
        if delivery is not None:
            self.write_worker.handle(LOCAL_AUTHORIZATION, delivery.model_dump(mode="json"))

    def test_write_run_success_resume_once_and_history_reuse(self: Any) -> None:
        self.writes()
        run, proposal = self.write_start()
        operation = self.write_confirm(proposal)
        self.write_execute(operation)
        self.assertEqual(
            self.write_read(operation_path(self.tenant, proposal))["operation"]["status"],
            "succeeded",
        )
        waiting = self.write_read(f"tenants/{self.tenant}/run_proposals/{proposal}")
        self.assertEqual(waiting["result_disposition"], "queued")
        self.assertEqual(waiting["supplied_result"]["status"], "succeeded")
        self.write_handler.reconcile(
            self.jobs.get(self.tenant, job_id(self.tenant, KIND, proposal))
        )
        self.execute(run)
        self.assertEqual(
            self.conversations.get_run(self.identity(), self.tenant, run).status, "completed"
        )
        self.assertEqual(len(self.write_provider.calls), 1)

    def test_write_early_confirm_is_joined_at_suspend(self: Any) -> None:
        self.writes()
        original = Confirmations.suspend
        operations = []

        def early(instance: Any, execution: Any, request: Any, result: Any) -> None:
            operation = self.write_confirm(result.proposal_id)
            operations.append(operation)
            self.write_execute(operation)
            self.write_handler.recover(self.tenant, self.jobs.store)
            self.assertEqual(
                self.write_read(operation_path(self.tenant, result.proposal_id))["operation"][
                    "status"
                ],
                "pending",
            )
            self.assertEqual(self.write_provider.calls, [])
            original(instance, execution, request, result)

        with patch.object(Confirmations, "suspend", early):
            run, proposal = self.write_start()
        # The early delivery was safely deferred. A new generation starts after suspend.
        if hasattr(self, "clock"):
            self.clock.advance(6)
        else:
            from datetime import timedelta

            self.now += timedelta(seconds=6)
        self.write_execute(operations[0])
        self.assertEqual(
            self.write_read(operation_path(self.tenant, proposal))["operation"]["status"],
            "succeeded",
        )
        self.execute(run)
        self.assertEqual(
            self.conversations.get_run(self.identity(), self.tenant, run).status, "completed"
        )
        self.assertEqual(len(self.write_provider.calls), 1)

    def test_write_pending_cancel_prevents_effect(self: Any) -> None:
        self.writes()
        run, proposal = self.write_start()
        operation = self.write_confirm(proposal)
        self.conversations.cancel_run(self.identity(), self.tenant, run, "calendar-cancel-pending")
        self.write_execute(operation)
        self.assertEqual(self.write_provider.calls, [])
        self.assertEqual(
            self.conversations.get_run(self.identity(), self.tenant, run).status, "cancelled"
        )

    def test_write_late_success_after_cancel_records_without_resume(self: Any) -> None:
        self.writes()
        run, proposal = self.write_start()
        operation = self.write_confirm(proposal)
        self.write_provider.after = lambda: self.conversations.cancel_run(
            self.identity(), self.tenant, run, "calendar-cancel-late-result"
        )
        self.write_execute(operation)
        saved = self.write_read(operation_path(self.tenant, proposal))
        self.assertEqual(saved["operation"]["status"], "succeeded")
        waiting = self.write_read(f"tenants/{self.tenant}/run_proposals/{proposal}")
        self.assertTrue(waiting["result_redacted"])
        self.assertEqual(waiting["result_disposition"], "recorded")
        self.assertEqual(
            self.conversations.get_run(self.identity(), self.tenant, run).status, "cancelled"
        )
        self.assertEqual(len(self.write_provider.calls), 1)

    def test_write_original_pi_session_expiry_before_start(self: Any) -> None:
        self.release_fixture()
        self.writes()
        proposals = self.jobs.store.run(
            lambda tx: tx.collection(f"tenants/{self.tenant}/calendar_proposals")
        )
        canonical = next(doc for _, doc in proposals if doc.get("run_id") is not None)
        operation = self.write_confirm(ResourceId(canonical["id"]))
        self.release_advance()
        self.write_execute(operation)
        self.write_handler.recover(self.tenant, self.jobs.store)
        self.assertEqual(self.write_provider.calls, [])
        saved = self.write_read(operation_path(self.tenant, operation.operation_id))
        self.assertEqual(saved["operation"]["status"], "failed")

    def test_write_confirmed_before_suspension_restart_keeps_original_deadline(self: Any) -> None:
        from datetime import timedelta

        from brainoid_jobs import LeaseLost

        self.writes()
        captured: list[Any] = []

        def interrupted(instance: Any, execution: Any, request: Any, result: Any) -> None:
            captured.append(
                (execution.lease, result.proposal_id, self.write_confirm(result.proposal_id))
            )
            raise LeaseLost()

        with patch.object(Confirmations, "suspend", interrupted):
            if hasattr(self, "turn"):
                conversation = self.conversation(private=True)
                run = ResourceId(self.turn(conversation).json()["run_id"])
            else:
                run = self.accept(self.conversation(private=True)).run_id
            self.execute(run)
        self.assertEqual(len(captured), 1)
        if hasattr(self, "clock"):
            self.clock.advance(301)
        else:
            self.now += timedelta(seconds=301)
        before = self.write_read(proposal_path(self.tenant, captured[0][1]))["expires_at"]
        self.restart()
        self.writes()
        self.execute(run)
        self.assertEqual(
            self.write_read(proposal_path(self.tenant, captured[0][1]))["expires_at"], before
        )
        self.write_execute(captured[0][2])
        self.assertEqual(
            self.write_read(operation_path(self.tenant, captured[0][1]))["operation"]["status"],
            "succeeded",
        )
        self.assertEqual(len(self.write_provider.calls), 1)

    def test_write_unknown_consumed_once_later_business_fact_can_improve(self: Any) -> None:
        from copy import deepcopy

        self.writes()
        run, proposal = self.write_start()
        operation = self.write_confirm(proposal)
        original: list[Any] = []

        def changed() -> None:
            event = next(iter(self.write_provider.events.values()))
            original.append(deepcopy(event))
            event["summary"] = "later edit makes first reconciliation inconclusive"

        self.write_provider.after = changed
        self.write_provider.lost = True
        self.write_execute(operation)
        self.assertEqual(
            self.write_read(operation_path(self.tenant, proposal))["operation"]["status"],
            "outcome_unknown",
        )
        self.execute(run)
        completed = self.write_read(f"tenants/{self.tenant}/runs/{run}")
        self.assertEqual(completed["status"], "failed")
        self.assertEqual(completed["error_code"], "outcome_unknown")
        self.write_provider.events[original[0]["id"]] = original[0]
        self.write_handler.recover(self.tenant, self.jobs.store)
        self.assertEqual(
            self.write_read(operation_path(self.tenant, proposal))["operation"]["status"],
            "succeeded",
        )
        self.assertEqual(self.write_read(f"tenants/{self.tenant}/runs/{run}"), completed)
        waiting = self.write_read(f"tenants/{self.tenant}/run_proposals/{proposal}")
        self.assertEqual(waiting["supplied_result"]["status"], "outcome_unknown")
        self.assertEqual(len(self.write_provider.calls), 1)

    def test_write_pre_send_death_resumes_failure_once(self: Any) -> None:
        from datetime import timedelta

        self.writes()
        run, proposal = self.write_start()
        operation = self.write_confirm(proposal)
        original = self.jobs.start_effect
        reached: list[bool] = []

        def crash(*args: Any, **kwargs: Any) -> Any:
            original(*args, **kwargs)
            reached.append(True)
            raise SystemExit("synthetic pre-send death")

        with (
            patch.object(self.jobs, "start_effect", crash),
            patch.object(self.write_handler, "reconcile", return_value=None),
            self.assertRaises(SystemExit),
        ):
            self.write_execute(operation)
        self.assertEqual(reached, [True])
        if hasattr(self, "clock"):
            self.clock.advance(61)
        else:
            self.now += timedelta(seconds=61)
        self.assertIsNone(
            self.jobs.prepare_dispatch(self.tenant, job_id(self.tenant, KIND, proposal))
        )
        self.write_handler.recover(self.tenant, self.jobs.store)
        waiting = self.write_read(f"tenants/{self.tenant}/run_proposals/{proposal}")
        self.assertEqual(waiting["supplied_result"]["status"], "failed")
        self.assertEqual(waiting["result_disposition"], "queued")
        self.write_handler.recover(self.tenant, self.jobs.store)
        self.assertEqual(
            self.write_read(f"tenants/{self.tenant}/run_proposals/{proposal}"), waiting
        )
        self.execute(run)
        self.assertEqual(
            self.conversations.get_run(self.identity(), self.tenant, run).status, "completed"
        )
        self.assertEqual(self.write_provider.calls, [])
        self.assertEqual(
            self.write_read(operation_path(self.tenant, proposal))["operation"]["error_code"],
            "not_started",
        )

    def test_write_cancelled_unstarted_recovery_records_without_resume(self: Any) -> None:
        self.writes()
        run, proposal = self.write_start()
        operation = self.write_confirm(proposal)
        self.conversations.cancel_run(
            self.identity(), self.tenant, run, "calendar-r2-cancel-unstarted"
        )
        self.write_execute(operation)
        for _ in range(2):
            self.write_handler.recover(self.tenant, self.jobs.store)
        self.assertEqual(
            self.write_read(operation_path(self.tenant, proposal))["operation"]["status"], "failed"
        )
        waiting = self.write_read(f"tenants/{self.tenant}/run_proposals/{proposal}")
        self.assertEqual(waiting["result_disposition"], "recorded")
        self.assertTrue(waiting["result_redacted"])
        self.assertEqual(
            self.conversations.get_run(self.identity(), self.tenant, run).status, "cancelled"
        )
        self.assertEqual(self.write_provider.calls, [])
