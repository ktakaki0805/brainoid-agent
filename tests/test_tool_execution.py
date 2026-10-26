"""BA-RUN-06 / T-018,T-029: real policy with fake external effects and durable storage."""

import asyncio
import hashlib
import json
import unittest
from collections.abc import Callable
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal, cast
from unittest.mock import patch
from uuid import uuid4

from brainoid_agent_core.ports import CallControl, ToolExecutor
from brainoid_agent_core.types import Cancelled, Failed, ToolCall, ToolExecution, ToolResult
from brainoid_contracts import ResourceId
from brainoid_contracts.transactions import Transaction
from brainoid_policy.accounts import AccountUnavailable, account_stamp
from brainoid_policy.models import Action, ResourceKind, ResourceRef
from brainoid_policy.scopes import PolicyDenied
from brainoid_policy.transactions import transaction_policy
from brainoid_tools import (
    Arguments,
    Authority,
    Executor,
    HandlerResult,
    Invocation,
    Registration,
    Registry,
    ToolRejected,
)
from fakes.runtime import CancellationToken
from pydantic import JsonValue, create_model
from test_jobs import JobFixture


class DeleteArguments(Arguments):
    note_id: ResourceId
    expected_version: int


class PolicyAuthority:
    def __init__(self, fixture: JobFixture) -> None:
        self.fixture = fixture
        self.run_id = ResourceId(str(uuid4()))
        self.limit = 2
        self.allowed = frozenset({"notes.delete"})
        self.confirmation: Literal["not_required", "waiting", "confirmed", "rejected"] = (
            "not_required"
        )
        self.proposal: ResourceId | None = None
        self.checks = 0
        self.deny_on = 0

    def check(self, tx: Transaction, request: ToolExecution, arguments: Arguments) -> Authority:
        self.checks += 1
        if self.deny_on == self.checks or not isinstance(arguments, DeleteArguments):
            raise ToolRejected("permission_denied")
        f = self.fixture
        try:
            grant = transaction_policy(tx, ledger=f.ledger, now=f.clock.now).resource(
                f.tenant,
                f.subject,
                ResourceRef(tenant_id=f.tenant, kind=ResourceKind.NOTE, id=arguments.note_id),
                action=Action.WRITE,
            )
            stamp = account_stamp(tx, f.subject.user_id)
        except (PolicyDenied, AccountUnavailable):
            raise ToolRejected("permission_denied") from None
        bound_resource = grant.resource
        if grant.resource.version != arguments.expected_version:
            execution = tx.get(f"tenants/{f.tenant}/tool_executions/{request.tool_call_id}")
            effect = tx.get(f"tenants/{f.tenant}/fixture_effects/{request.tool_call_id}")
            bound_resource = grant.resource.model_copy(
                update={"version": arguments.expected_version}
            )
            if execution is None or effect != {
                "effect_id": execution["effect_id"],
                "fingerprint": execution["fingerprint"],
                "before": bound_resource.model_dump(mode="json"),
                "after_version": grant.resource.version,
            }:
                raise ToolRejected("permission_denied")
        return Authority(
            context=grant.context,
            run_id=self.run_id,
            receipt=hashlib.sha256(
                (stamp + grant.principal.stamp + bound_resource.model_dump_json()).encode()
            ).hexdigest(),
            allowed_tools=self.allowed,
            max_calls=self.limit,
            confirmation=self.confirmation,
            proposal_id=self.proposal,
        )


class Provider:
    def __init__(self, fixture: JobFixture) -> None:
        self.fixture = fixture
        self.calls: list[Invocation[DeleteArguments]] = []
        self.lookups: list[ResourceId] = []
        self.response = HandlerResult(status="succeeded", result={"body": "FIXTURE_SECRET_BODY"})
        self.hook: Callable[[], None] = lambda: None
        self.gate: asyncio.Event | None = None
        self.entered = asyncio.Event()

    async def execute(
        self, invocation: Invocation[DeleteArguments], *, control: CallControl
    ) -> HandlerResult:
        assert not self.fixture.store.in_transaction
        self.calls.append(invocation)
        self.entered.set()
        self.hook()
        if self.gate is not None:
            await self.gate.wait()
        return self.response

    async def reconcile(
        self, invocation: Invocation[DeleteArguments], *, control: CallControl
    ) -> HandlerResult:
        assert not self.fixture.store.in_transaction
        self.lookups.append(invocation.effect_id)
        return self.response


class ToolExecutionTests(JobFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.authority = PolicyAuthority(self)
        self.provider = Provider(self)
        self.registry = Registry()
        self.registry.register(Registration("notes.delete", DeleteArguments, self.provider))
        self.executor = Executor(self.store, self.registry, self.authority, self.clock)
        self.cancel = CancellationToken()
        self.control = CallControl(self.cancel, 30)
        context = self.store.run(
            lambda tx: (
                transaction_policy(tx, ledger=self.ledger, now=self.clock.now)
                .resource(self.tenant, self.subject, self.ref, action=Action.WRITE)
                .context
            )
        )
        self.request = ToolExecution(
            context=context,
            tool_call_id=ResourceId(str(uuid4())),
            idempotency_key="FIXTURE_PRIVATE_KEY",
            call=ToolCall(
                call_key="provider-local-id",
                name="notes.delete",
                arguments_json=json.dumps({"note_id": self.ref.id, "expected_version": 1}),
            ),
        )

    async def execute(
        self, request: ToolExecution | None = None
    ) -> ToolResult | Failed | Cancelled:
        result = await self.executor.execute(request or self.request, control=self.control)
        assert isinstance(result, (ToolResult, Failed, Cancelled))
        return result

    def rejected_call(self, name: str, value: object) -> ToolExecution:
        return self.request.model_copy(
            update={
                "call": ToolCall(
                    call_key="provider-local-id", name=name, arguments_json=json.dumps(value)
                )
            }
        )

    async def test_transaction_retries_obey_read_before_write_and_do_not_repeat_io(self) -> None:
        class ReadFirst:
            def __init__(self, tx: Transaction) -> None:
                self.tx = tx
                self.written = False

            def __getattr__(self, name: str) -> Any:
                method = getattr(self.tx, name)

                def checked(*args: object) -> object:
                    if name in {"set", "create", "delete"}:
                        self.written = True
                    elif self.written:
                        raise AssertionError("Read after a transaction write")
                    return method(*args)

                return checked

        original = self.store.run

        def guarded[T](work: Callable[[Transaction], T]) -> T:
            return original(lambda tx: work(cast(Transaction, ReadFirst(tx))))

        self.store.retry_once = True
        with patch.object(self.store, "run", guarded):
            result = await self.execute()
            self.assertEqual(await self.execute(), result)
        assert isinstance(result, ToolResult)
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(len(self.provider.calls), 1)

    async def test_typed_success_is_durable_retry_safe_and_content_free_audit(self) -> None:
        self.store.retry_once = True
        port: ToolExecutor = self.executor
        first = await port.execute(self.request, control=self.control)
        self.assertIsInstance(first, ToolResult)
        assert isinstance(first, ToolResult)
        self.assertEqual(first.status, "succeeded")
        self.assertEqual(await self.execute(), first)
        self.assertEqual(len(self.provider.calls), 1)
        self.assertIsInstance(self.provider.calls[0].arguments, DeleteArguments)
        self.assertEqual(self.provider.calls[0].effect_id, first.operation_id)
        audits = [v for k, v in self.store.data.items() if "/tool_audit/" in k]
        self.assertEqual({v["status"] for v in audits}, {"started", "succeeded"})
        serialized = json.dumps(audits)
        for secret in (
            "FIXTURE_SECRET_BODY",
            "FIXTURE_PRIVATE_KEY",
            "provider-local-id",
            "arguments",
            "token",
        ):
            self.assertNotIn(secret, serialized)
        self.assertIn(str(self.request.tool_call_id), serialized)
        self.provider.response.result["body"] = "changed"
        first.result["body"] = "also changed"
        replay = await self.execute()
        assert isinstance(replay, ToolResult)
        self.assertEqual(replay.result["body"], "FIXTURE_SECRET_BODY")

    async def test_tenant_injection_unknown_tool_and_forbidden_nested_fields(self) -> None:
        for name, args in (
            (
                "notes.delete",
                {"note_id": self.ref.id, "expected_version": 1, "tenant_id": str(uuid4())},
            ),
            ("calendar.confirm", {"proposal_id": str(uuid4())}),
            ("PROMPT_TOKEN_REVEAL", {}),
            ("notes.delete", {"note_id": self.ref.id, "expected_version": True}),
            ("notes.delete", {"note_id": self.ref.id, "expected_version": 1, "approved": True}),
        ):
            with self.subTest(name=name, args=args):
                result = await self.execute(self.rejected_call(name, args))
                self.assertEqual(result, Failed(code="invalid_arguments"))
        self.assertFalse(self.provider.calls)
        self.assertNotIn("PROMPT_TOKEN_REVEAL", json.dumps(self.store.data))

    async def test_allowlist_and_current_membership_are_required_on_replay(self) -> None:
        self.authority.allowed = frozenset()
        self.assertEqual(await self.execute(), Failed(code="permission_denied"))
        self.authority.allowed = frozenset({"notes.delete"})
        self.assertIsInstance(await self.execute(), ToolResult)
        self.store.data[self.member_path]["status"] = "revoked"
        self.assertEqual(await self.execute(), Failed(code="permission_denied"))
        self.assertEqual(len(self.provider.calls), 1)

    async def test_target_other_tenant_and_changed_version_are_rejected(self) -> None:
        others = [
            d
            for p, d in self.store.data.items()
            if "/notes/" in p and d["tenant_id"] != self.tenant
        ]
        self.assertTrue(others)
        result = await self.execute(
            self.rejected_call("notes.delete", {"note_id": others[0]["id"], "expected_version": 1})
        )
        self.assertEqual(result, Failed(code="permission_denied"))
        self.store.data[self.source_path]["version"] = 2
        self.assertEqual(await self.execute(), Failed(code="permission_denied"))
        self.assertFalse(self.provider.calls)

    async def test_recheck_immediately_before_provider_and_after_effect(self) -> None:
        self.authority.deny_on = 2
        self.assertEqual(await self.execute(), Failed(code="permission_denied"))
        self.assertFalse(self.provider.calls)
        self.authority.deny_on = 0
        replay = await self.execute()
        assert isinstance(replay, ToolResult)
        self.assertEqual(replay.status, "outcome_unknown")
        # New call, revoked while the external effect runs: suppress result/body.
        request = self.request.model_copy(
            update={"tool_call_id": ResourceId(str(uuid4())), "idempotency_key": "second"}
        )
        self.provider.hook = lambda: self.store.data[self.member_path].update(status="revoked")
        self.assertEqual(await self.execute(request), Failed(code="permission_denied"))
        self.assertEqual(len(self.provider.calls), 1)

    async def test_response_loss_requires_explicit_reconciliation(self) -> None:
        def lose() -> None:
            raise TimeoutError("FIXTURE_TOKEN provider response disappeared")

        self.provider.hook = lose
        first = await self.execute()
        assert isinstance(first, ToolResult)
        self.assertEqual(first.status, "outcome_unknown")
        self.executor = Executor(self.store, self.registry, self.authority, self.clock)
        self.assertEqual(await self.execute(), first)
        result = await self.executor.reconcile(self.request, control=self.control)
        assert isinstance(result, ToolResult)
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.operation_id, first.operation_id)
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(self.provider.lookups, [first.operation_id])
        self.assertNotIn("FIXTURE_TOKEN", json.dumps(self.store.data))

    async def test_lookup_absence_during_inflight_effect_never_becomes_failure(self) -> None:
        self.provider.gate = asyncio.Event()
        task = asyncio.create_task(self.execute())
        await self.provider.entered.wait()
        self.provider.response = HandlerResult(status="failed", error_code="not_found")
        uncertain = await self.executor.reconcile(self.request, control=self.control)
        assert isinstance(uncertain, ToolResult)
        self.assertEqual(uncertain.status, "outcome_unknown")
        self.provider.response = HandlerResult(status="succeeded", result={"confirmed": True})
        self.provider.gate.set()
        result = await task
        assert isinstance(result, ToolResult)
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(await self.execute(), result)
        self.assertEqual(len(self.provider.calls), 1)

    async def test_account_and_ledger_changes_reject_execution(self) -> None:
        self.store.data[self.member_path]["authorization_version"] = 2
        self.assertEqual(await self.execute(), Failed(code="permission_denied"))
        self.store.data[self.member_path]["authorization_version"] = 1
        profile = next(
            d
            for p, d in self.store.data.items()
            if p.startswith("users/") and d["user_id"] == self.subject.user_id
        )
        profile["status"] = "deleting"
        self.assertEqual(await self.execute(), Failed(code="permission_denied"))
        self.assertFalse(self.provider.calls)

    async def test_finish_commit_response_loss_replays_committed_success(self) -> None:
        original = self.store.run
        calls = 0

        def lose[T](work: Callable[[Transaction], T]) -> T:
            nonlocal calls
            calls += 1
            result = original(work)
            if calls == 3:
                raise TimeoutError("lost final commit response")
            return result

        with patch.object(self.store, "run", lose):
            result = await self.execute()
        assert isinstance(result, ToolResult)
        self.assertEqual(result.status, "outcome_unknown")
        replay = await self.execute()
        assert isinstance(replay, ToolResult)
        self.assertEqual(replay.status, "succeeded")
        self.assertEqual(len(self.provider.calls), 1)

    async def test_command_correlations_and_cooperative_live_cancellation(self) -> None:
        registry = Registry()
        registry.register(Registration("notes.delete", DeleteArguments, self.provider, "command"))
        self.executor = Executor(self.store, registry, self.authority, self.clock)
        self.provider.gate = asyncio.Event()
        task = asyncio.create_task(self.execute())
        await self.provider.entered.wait()
        self.cancel.cancel()
        self.provider.gate.set()
        result = await task
        assert isinstance(result, ToolResult)
        self.assertEqual(result.status, "outcome_unknown")
        self.assertIsNotNone(result.command_id)
        self.assertIsNone(result.operation_id)

    async def test_result_return_rechecks_authority_after_commit(self) -> None:
        self.authority.deny_on = 4
        result = await self.execute()
        self.assertEqual(result, Failed(code="permission_denied"))
        self.assertEqual(len(self.provider.calls), 1)
        self.authority.deny_on = 0
        replay = await self.execute()
        assert isinstance(replay, ToolResult)
        self.assertEqual(replay.status, "succeeded")

    async def test_expired_deadline_after_commit_suppresses_body_but_retains_result(self) -> None:
        original = self.store.run
        calls = 0

        def delayed[T](work: Callable[[Transaction], T]) -> T:
            nonlocal calls
            calls += 1
            result = original(work)
            if calls == 3:
                self.clock.advance(31)
            return result

        with patch.object(self.store, "run", delayed):
            result = await self.execute()
        assert isinstance(result, ToolResult)
        self.assertEqual(result.status, "outcome_unknown")
        self.assertEqual(result.result, {})
        self.control = CallControl(self.cancel, 60)
        replay = await self.execute()
        assert isinstance(replay, ToolResult)
        self.assertEqual(replay.status, "succeeded")
        self.assertEqual(len(self.provider.calls), 1)

    async def test_own_completed_operation_proves_version_transition_without_skipping_iam(
        self,
    ) -> None:
        def mutate() -> None:
            def commit(tx: Transaction) -> None:
                current = transaction_policy(tx, ledger=self.ledger, now=self.clock.now).resource(
                    self.tenant, self.subject, self.ref, action=Action.WRITE
                )
                source = tx.get(self.source_path)
                execution = tx.get(
                    f"tenants/{self.tenant}/tool_executions/{self.request.tool_call_id}"
                )
                assert source is not None and execution is not None
                tx.set(self.source_path, {**source, "version": source["version"] + 1})
                tx.create(
                    f"tenants/{self.tenant}/fixture_effects/{self.request.tool_call_id}",
                    {
                        "effect_id": self.provider.calls[-1].effect_id,
                        "fingerprint": execution["fingerprint"],
                        "before": current.resource.model_dump(mode="json"),
                        "after_version": source["version"] + 1,
                    },
                )

            self.store.run(commit)

        self.provider.hook = mutate
        result = await self.execute()
        assert isinstance(result, ToolResult)
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(await self.execute(), result)
        self.assertEqual(len(self.provider.calls), 1)
        self.store.data[self.source_path]["version"] = 3
        self.assertEqual(await self.execute(), Failed(code="permission_denied"))

    async def test_start_commit_response_loss_never_reissues_effect(self) -> None:
        original = self.store.run
        lose_next = True

        def lose[T](work: Callable[[Transaction], T]) -> T:
            nonlocal lose_next
            result = original(work)
            if lose_next:
                lose_next = False
                raise TimeoutError("lost commit response")
            return result

        with patch.object(self.store, "run", lose):
            result = await self.execute()
        assert isinstance(result, ToolResult)
        self.assertEqual(result.status, "outcome_unknown")
        self.assertFalse(self.provider.calls)
        self.assertEqual(await self.execute(), result)

    async def test_concurrent_replay_and_task_cancellation_preserve_started_marker(self) -> None:
        self.provider.gate = asyncio.Event()
        task = asyncio.create_task(self.execute())
        await self.provider.entered.wait()
        duplicate = await self.execute()
        assert isinstance(duplicate, ToolResult)
        self.assertEqual(duplicate.status, "outcome_unknown")
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(await self.execute(), duplicate)
        self.assertEqual(len(self.provider.calls), 1)

    async def test_run_budget_idempotency_and_call_id_collisions(self) -> None:
        self.authority.limit = 1
        await self.execute()
        await self.execute()
        new_id = ResourceId(str(uuid4()))
        same_key = self.request.model_copy(update={"tool_call_id": new_id})
        self.assertEqual(await self.execute(same_key), Failed(code="conflict"))
        new_call = same_key.model_copy(update={"idempotency_key": "new-key"})
        self.assertEqual(await self.execute(new_call), Failed(code="rate_limited"))
        different_body = self.rejected_call(
            "notes.delete", {"note_id": self.ref.id, "expected_version": 2}
        )
        # Same target revision is denied even before idempotency comparison.
        self.assertEqual(await self.execute(different_body), Failed(code="permission_denied"))
        changed_key = self.request.model_copy(update={"idempotency_key": "changed"})
        self.assertEqual(await self.execute(changed_key), Failed(code="conflict"))
        self.assertEqual(len(self.provider.calls), 1)

    async def test_waiting_confirmation_and_explicit_failure_are_correlated(self) -> None:
        self.authority.confirmation = "waiting"
        self.authority.proposal = ResourceId(str(uuid4()))
        result = await self.execute()
        assert isinstance(result, ToolResult)
        self.assertEqual(result.status, "waiting_confirmation")
        self.assertEqual(result.proposal_id, self.authority.proposal)
        self.assertFalse(self.provider.calls)
        self.assertEqual(await self.execute(), result)
        self.authority.confirmation = "not_required"
        self.authority.proposal = None
        self.provider.response = HandlerResult(status="failed", error_code="rejected")
        request = self.request.model_copy(
            update={"tool_call_id": ResourceId(str(uuid4())), "idempotency_key": "failure"}
        )
        failed = await self.execute(request)
        assert isinstance(failed, ToolResult)
        self.assertEqual(failed.status, "failed")
        self.assertIsNotNone(failed.operation_id)
        self.assertEqual(await self.execute(request), failed)

    async def test_handler_malformed_and_nonfinite_results_are_unknown(self) -> None:
        self.provider.response.result["bad"] = float("nan")
        result = await self.execute()
        assert isinstance(result, ToolResult)
        self.assertEqual(result.status, "outcome_unknown")
        self.assertNotIn("NaN", json.dumps(self.store.data))

    async def test_cancel_before_start_and_deadline_after_effect(self) -> None:
        self.cancel.cancel()
        self.assertEqual(await self.execute(), Cancelled())
        self.assertFalse(self.provider.calls)
        self.cancel = CancellationToken()
        self.control = CallControl(self.cancel, 30)
        self.provider.hook = lambda: self.clock.advance(31)
        result = await self.execute()
        assert isinstance(result, ToolResult)
        self.assertEqual(result.status, "outcome_unknown")


class SchemaTests(unittest.TestCase):
    def test_packaged_schema_matches_authoritative_spec(self) -> None:
        authoritative = Path(__file__).resolve().parents[1] / "specs/tool-contracts.schema.json"
        self.assertEqual(
            json.loads(authoritative.read_text()),
            json.loads(files("brainoid_tools").joinpath("tool-contracts.schema.json").read_text()),
        )

    def test_all_declared_tools_validate_canonical_schema_before_business_type(self) -> None:
        registry = Registry()
        schema: dict[str, Any] = json.loads(
            files("brainoid_tools").joinpath("tool-contracts.schema.json").read_text()
        )
        # Every branch is registered using a deliberately broad fixture type. The canonical
        # schema must still enforce all fields and nested rules independently of that type.
        for entry in schema["oneOf"]:
            definition = schema["$defs"][entry["$ref"].split("/")[-1]]
            name = definition["properties"]["name"]["const"]
            args = definition["properties"]["arguments"]
            while "$ref" in args:
                args = schema["$defs"][args["$ref"].split("/")[-1]]
            properties = args.get("properties", {})
            if "oneOf" in args:
                properties = {
                    key: val
                    for branch in args["oneOf"]
                    for key, val in branch.get("properties", {}).items()
                }
            fields: dict[str, Any] = {key: (JsonValue, None) for key in properties}
            model = create_model(
                "FixtureArguments",
                __base__=Arguments,
                **fields,
            )
            registry.register(Registration(name, model, None))  # type: ignore[arg-type]
            with self.assertRaises(ToolRejected, msg=name):
                registry.validate(
                    ToolCall(call_key="k", name=name, arguments_json='{"tenant_id":"injected"}')
                )
        with self.assertRaises(ValueError):
            registry.register(Registration("calendar.confirm", Arguments, None))  # type: ignore[arg-type]

    def test_json_parser_rejects_duplicate_nonfinite_large_and_deep_inputs(self) -> None:
        registry = Registry()
        registry.register(Registration("notes.delete", DeleteArguments, None))  # type: ignore[arg-type]
        for raw in (
            '{"note_id":"x","note_id":"y","expected_version":1}',
            '{"x":NaN}',
            '{"x":1e10000}',
            "[]",
            "[" * 1000 + "]" * 1000,
            '"' + "a" * 65536 + '"',
        ):
            with self.subTest(raw=raw[:60]), self.assertRaises(ToolRejected):
                registry.validate(ToolCall(call_key="k", name="notes.delete", arguments_json=raw))

    def test_nested_domain_calendar_formats_and_scope_rules(self) -> None:
        class Home(Arguments):
            device_id: ResourceId
            target_entity_id: ResourceId
            operation: dict[str, JsonValue]

        class Calendar(Arguments):
            integration_id: ResourceId
            change: dict[str, JsonValue]

        registry = Registry()
        registry.register(Registration("home.set_state", Home, None, "command"))  # type: ignore[arg-type]
        registry.register(Registration("calendar.propose", Calendar, None))  # type: ignore[arg-type]
        home: dict[str, Any] = {
            "device_id": str(uuid4()),
            "target_entity_id": str(uuid4()),
            "operation": {"action": "light.turn_on", "parameters": {"brightness_pct": 50}},
        }
        registry.validate(
            ToolCall(call_key="k", name="home.set_state", arguments_json=json.dumps(home))
        )
        for operation in (
            {"action": "shell.exec", "parameters": {}},
            {"action": "light.turn_on", "parameters": {"brightness_pct": 101}},
            {"action": "light.turn_on", "parameters": {"tenant_id": "x"}},
        ):
            with self.assertRaises(ToolRejected):
                registry.validate(
                    ToolCall(
                        call_key="k",
                        name="home.set_state",
                        arguments_json=json.dumps({**home, "operation": operation}),
                    )
                )
        calendar: dict[str, Any] = {
            "action": "create",
            "calendar_id": "fixture",
            "summary": "fixture",
            "start": {"date_time": "2026-10-26T10:00:00Z", "timezone": "Asia/Tokyo"},
            "end": {"date": "2026-10-27"},
        }
        registry.validate(
            ToolCall(
                call_key="k",
                name="calendar.propose",
                arguments_json=json.dumps({"integration_id": str(uuid4()), "change": calendar}),
            )
        )
        for start in (
            {"date_time": "2026-10-26T10:00:00", "timezone": "Asia/Tokyo"},
            {"date_time": "2026-10-26T10:00:00Z", "timezone": "Invalid/FIXTURE"},
            {"date": "2026-02-30"},
            {"date": "2026-10-27", "timezone": "UTC"},
        ):
            with self.assertRaises(ToolRejected):
                registry.validate(
                    ToolCall(
                        call_key="k",
                        name="calendar.propose",
                        arguments_json=json.dumps(
                            {"integration_id": str(uuid4()), "change": {**calendar, "start": start}}
                        ),
                    )
                )
