"""APP-06 / FR-011 / T-018, T-036 through HTTP, common service and actual RunTurn."""

import copy
import json
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import yaml  # type: ignore[import-untyped]
from brainoid_agent_core.types import ToolCall
from brainoid_api.repositories.commands import CommandRequests
from brainoid_contracts import ResourceId
from brainoid_contracts.deletion import lifecycle_tenant_path
from brainoid_memory.commands import CommandCreate, CommandRecord, command_path
from brainoid_persistence.firestore import PersistenceUnavailable
from brainoid_policy.control_ledger import LedgerError
from brainoid_worker.home import HomeRuntime
from brainoid_worker.local import LOCAL_AUTHORIZATION
from fakes.home import HomePreambleModel, home_fixture
from fakes.memory_repository import OTHER, TENANT
from fakes.run_confirmation import ConfirmationModel
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from test_notes import NotesFixture, NotesWorkerFixture


def intent(body: CommandCreate) -> str:
    return "Set home state " + json.dumps(
        body.model_dump(mode="json"), ensure_ascii=False, sort_keys=True
    )


class CommandsHTTPTests(NotesFixture):
    def setUp(self) -> None:
        super().setUp()
        self.command_body, documents = home_fixture(TENANT, self.identity().user_id, self.now)
        self.store.data.update(documents)
        self.url = f"/v1/tenants/{TENANT}/commands"
        self.commands = CommandRequests(self.memories)

    def create(self, key: str = "home-create-key-001", **changes: Any) -> Any:
        return self.client.post(
            self.url,
            json={**self.command_body.model_dump(mode="json"), **changes},
            headers={"Idempotency-Key": key},
        )

    def cancel(
        self, identity: str, key: str = "home-cancel-key-001", version: int | None = None
    ) -> Any:
        return self.client.post(
            self.url + "/" + identity + ":cancel",
            headers={
                "Idempotency-Key": key,
                **({} if version is None else {"If-Match": f'"{version}"'}),
            },
        )

    def test_actual_http_contract_idempotency_actor_and_acceptance_only(self) -> None:
        first = self.create()
        self.assertEqual(first.status_code, 202, first.text)
        record = first.json()
        self.assertEqual(record["status"], "queued")
        self.assertEqual(first.headers["etag"], '"1"')
        saved = CommandRecord.model_validate(
            self.store.data[command_path(TENANT, ResourceId(record["id"]))]
        )
        self.assertEqual(saved.expires_at - saved.created_at, timedelta(seconds=30))
        self.assertEqual(saved.created_by, self.identity().user_id)
        self.assertEqual(saved.actor.user_id, self.identity().user_id)  # type: ignore[union-attr]
        self.assertEqual(self.create().json(), record)
        self.assertEqual(self.client.get(self.url + "/" + record["id"]).json(), record)
        self.assertEqual(len([p for p in self.store.data if "/commands/" in p]), 1)
        self.assertEqual(
            self.create(operation={"action": "light.turn_off", "parameters": {}}).status_code, 409
        )
        spec = yaml.safe_load(Path("specs/openapi.yaml").read_text())
        Draft202012Validator(
            {"$ref": "#/components/schemas/Command", "components": spec["components"]}
        ).validate(record)

    def test_t018_types_ranges_unknown_service_url_ambiguity_and_foreign_tenant(self) -> None:
        for operation in (
            {"action": "light.toggle", "parameters": {}},
            {"action": "lock.unlock", "parameters": {}},
            {"action": "https://fixture.invalid", "parameters": {}},
            *(
                {"action": "light.turn_on", "parameters": {"brightness_pct": x}}
                for x in (0, 101, True, "30", 2.5, None)
            ),
            *(
                {"action": "climate.set_temperature", "parameters": {"temperature_c": x}}
                for x in (15.9, 30.1, True, "20")
            ),
            {"action": "light.turn_off", "parameters": {"arbitrary": "x"}},
            {"action": "climate.set_hvac_mode", "parameters": {"hvac_mode": "emergency"}},
        ):
            self.assertEqual(self.create(operation=operation).status_code, 422, operation)
        self.assertEqual(self.create(target_entity_id="照明").status_code, 422)
        self.assertEqual(self.create(expires_at=self.now.isoformat()).status_code, 422)
        self.assertEqual(self.create(target_entity_id=str(uuid4())).status_code, 404)
        self.assertEqual(
            self.client.post(
                f"/v1/tenants/{OTHER}/commands",
                json=self.command_body.model_dump(mode="json"),
                headers={"Idempotency-Key": "cross-tenant-home-01"},
            ).status_code,
            404,
        )
        self.assertFalse(any("/commands/" in p for p in self.store.data))

    def test_allowed_climate_boundaries_are_typed_and_inventory_bound(self) -> None:
        target = next(d for p, d in self.store.data.items() if "/home_targets/" in p)
        for action, parameters in (
            ("climate.set_temperature", {"temperature_c": 16}),
            ("climate.set_temperature", {"temperature_c": 30.0}),
            ("climate.set_hvac_mode", {"hvac_mode": "cool"}),
        ):
            operation = {"action": action, "parameters": parameters}
            self.assertEqual(
                self.create("home-climate-denied", operation=operation).status_code, 404
            )
            target["allowed_actions"] = [action]
            self.assertEqual(
                self.create("home-climate-" + str(uuid4()), operation=operation).status_code, 202
            )
            target = next(d for p, d in self.store.data.items() if "/home_targets/" in p)
            target["allowed_actions"] = []

    def test_cancel_atomic_states_optional_ifmatch_replay_and_unknown(self) -> None:
        identity = self.create().json()["id"]
        self.assertEqual(self.cancel(identity, version=2).status_code, 412)
        cancelled = self.cancel(identity, "home-cancel-winner1")
        self.assertEqual(
            (cancelled.status_code, cancelled.json()["status"], cancelled.headers["etag"]),
            (200, "cancelled", '"2"'),
        )
        self.assertEqual(self.cancel(identity, "home-cancel-winner1").json(), cancelled.json())
        self.assertEqual(self.cancel(identity, "home-cancel-again-1").status_code, 409)
        for state in ("running", "outcome_unknown", "succeeded", "failed"):
            other = self.create("home-state-" + state).json()["id"]
            self.store.data[command_path(TENANT, ResourceId(other))].update(
                status=state,
                execution_id=str(uuid4()),
                version=2,
                observed_state={"power": "on"} if state == "succeeded" else None,
            )
            self.assertEqual(self.cancel(other, "home-cancel-" + state).status_code, 409)
            self.assertEqual(self.client.get(self.url + "/" + other).json()["status"], state)

    def test_expiry_exact_boundary_persists_new_version_and_never_renews_same_key(self) -> None:
        first = self.create().json()
        self.now += timedelta(seconds=29, microseconds=999999)
        self.assertEqual(self.client.get(self.url + "/" + first["id"]).json()["status"], "queued")
        self.now += timedelta(microseconds=1)
        self.assertEqual(self.cancel(first["id"]).status_code, 410)
        expired = self.client.get(self.url + "/" + first["id"])
        self.assertEqual((expired.json()["status"], expired.headers["etag"]), ("expired", '"2"'))
        self.assertEqual(self.create().json(), expired.json())
        self.assertEqual(self.client.get(self.url + "/" + first["id"]).json(), expired.json())
        self.assertEqual(expired.json()["expires_at"], first["expires_at"])
        for key, version in (
            ("home-cancel-key-001", None),
            ("home-expired-new-key", None),
            ("home-expired-old-etag", 1),
            ("home-expired-new-etag", 2),
        ):
            self.assertEqual(self.cancel(first["id"], key, version).status_code, 410)
        running = self.create("home-started-before-expiry").json()["id"]
        self.store.data[command_path(TENANT, ResourceId(running))].update(
            status="running",
            execution_id=str(uuid4()),
            version=2,
        )
        self.now += timedelta(seconds=30)
        self.assertEqual(self.cancel(running).status_code, 409)

    def test_current_entity_inventory_device_member_tenant_and_ledger_rechecked(self) -> None:
        identity = self.create().json()["id"]
        original = copy.deepcopy(self.store.data)
        entity = f"tenants/{TENANT}/entities/{self.command_body.target_entity_id}"
        device = f"tenants/{TENANT}/devices/{self.command_body.device_id}"
        inventory = next(p for p in self.store.data if "/home_targets/" in p)
        integration = next(p for p in self.store.data if "/integrations/" in p)
        member = next(
            p
            for p, d in self.store.data.items()
            if "/memberships/" in p and d.get("user_id") == self.identity().user_id
        )
        for path, change in (
            (entity, {"version": 2}),
            (entity, {"deleted": True}),
            (entity, {"tenant_id": OTHER}),
            (device, {"allowed_entity_ids": []}),
            (device, {"allowed_scopes": ["shared"]}),
            (device, {"connection_generation": 2}),
            (device, {"authorization_version": 2}),
            (device, {"status": "revoked"}),
            (inventory, {"version": 2}),
            (inventory, {"status": "revoked"}),
            (integration, {"device_id": str(uuid4())}),
            (integration, {"status": "revoked"}),
            (member, {"authorization_version": 2}),
            (f"tenants/{TENANT}", {"status": "deleting"}),
        ):
            self.store.data = copy.deepcopy(original)
            self.store.data[path].update(change)
            self.assertNotEqual(
                self.client.get(self.url + "/" + identity).status_code, 200, (path, change)
            )
            self.assertNotEqual(self.create().status_code, 202, (path, change))
        self.store.data = original
        with patch.object(self.ledger, "latest", side_effect=LedgerError()):
            self.assertEqual(self.client.get(self.url + "/" + identity).status_code, 503)
        self.store.data[lifecycle_tenant_path(TENANT)] = {"mode": "delete"}
        self.assertNotEqual(self.create().status_code, 202)

    def test_no_inventory_or_public_entity_external_assertion_is_not_control_authority(
        self,
    ) -> None:
        del self.store.data[next(p for p in self.store.data if "/home_targets/" in p)]
        self.assertEqual(self.create().status_code, 404)
        self.assertFalse(any("/commands/" in p for p in self.store.data))

    def test_lost_commit_response_recovers_same_command_under_current_authority(self) -> None:
        def lost() -> None:
            self.store.after_commit = lambda: None
            raise RuntimeError("synthetic response loss")

        self.store.after_commit = lost
        with self.assertRaises(RuntimeError):
            self.commands.create(self.identity(), TENANT, self.command_body, "home-lost-response")
        result = self.commands.create(
            self.identity(), TENANT, self.command_body, "home-lost-response"
        )
        self.assertEqual(result.status, "queued")
        self.assertEqual(len([p for p in self.store.data if "/commands/" in p]), 1)


class CommandsWorkerTests(NotesWorkerFixture):
    def setUp(self) -> None:
        super().setUp()
        self.command_body, documents = home_fixture(
            self.tenant, self.identity().user_id, self.clock.now()
        )
        self.store.data.update(documents)

    def test_actual_default_tool_run_creates_correlated_queued_command_and_fixed_ack(self) -> None:
        run = self.call(
            "home.set_state", self.command_body.model_dump(mode="json"), intent(self.command_body)
        )
        self.assertEqual(self.get_run(run).json()["status"], "completed")
        self.assertEqual(len(self.model.requests), 1)
        saved = self.store.data[f"tenants/{self.tenant}/runs/{run}"]
        effect = saved["home_command"]["effect_id"]
        command = CommandRequests(self.memories).get(
            self.identity(), self.tenant, ResourceId(effect)
        )
        self.assertEqual(command.status, "queued")
        receipt = self.store.data[
            f"tenants/{self.tenant}/tool_executions/{saved['home_command']['tool_call_id']}"
        ]
        self.assertEqual(receipt["effect_kind"], "command")
        self.assertEqual(receipt["result"]["command_id"], effect)
        self.assertEqual(receipt["result"]["result"], {"accepted": True, "command_id": effect})
        final = self.store.data[
            f"tenants/{self.tenant}/conversations/{saved['conversation_id']}/messages/{saved['final_message_id']}"
        ]
        self.assertEqual(final["text"], HomeRuntime.UPDATE_ACK)

    def test_actual_run_negation_ambiguous_name_mismatched_model_and_external_data_do_not_authorize(
        self,
    ) -> None:
        for text in (
            "Do not " + intent(self.command_body),
            "照明をつけて",
            "Explain " + intent(self.command_body),
            intent(self.command_body).replace("40", "60"),
        ):
            self.call("home.set_state", self.command_body.model_dump(mode="json"), text)
            self.assertFalse(any("/commands/" in p for p in self.store.data))

    def test_actual_run_rechecks_current_target_after_model(self) -> None:
        self.configure_calls(
            (
                ToolCall(
                    call_key="home",
                    name="home.set_state",
                    arguments_json=self.command_body.model_dump_json(),
                ),
            )
        )
        conversation = self.conversation()
        run = ResourceId(self.turn(conversation, text=intent(self.command_body)).json()["run_id"])
        self.store.data[f"tenants/{self.tenant}/devices/{self.command_body.device_id}"][
            "allowed_entity_ids"
        ] = []
        self.execute(run)
        self.assertFalse(any("/commands/" in p for p in self.store.data))

    def test_model_success_preamble_never_reaches_sse_and_derived_user_has_no_authority(
        self,
    ) -> None:
        self.configure_calls(
            (
                ToolCall(
                    call_key="home",
                    name="home.set_state",
                    arguments_json=self.command_body.model_dump_json(),
                ),
            )
        )
        model = ConfirmationModel(self.command_body.target_entity_id)
        model.call = ToolCall(
            call_key="home",
            name="home.set_state",
            arguments_json=self.command_body.model_dump_json(),
        )
        model.prefix = "UNOBSERVED_PHYSICAL_SUCCESS " * 100
        self.handler.model = model
        conversation = self.conversation()
        run = ResourceId(self.turn(conversation, text=intent(self.command_body)).json()["run_id"])
        self.execute(run)
        self.assertEqual(self.get_run(run).json()["status"], "completed")
        events = [d for p, d in self.store.data.items() if f"/runs/{run}/events/" in p]
        self.assertNotIn("UNOBSERVED_PHYSICAL_SUCCESS", json.dumps(events, default=str))
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(len([p for p in self.store.data if "/commands/" in p]), 1)

        self.configure_calls((model.call,))
        conversation = self.conversation()
        run = ResourceId(self.turn(conversation, text=intent(self.command_body)).json()["run_id"])
        saved = self.store.data[f"tenants/{self.tenant}/runs/{run}"]
        message = self.store.data[
            f"tenants/{self.tenant}/conversations/{conversation}/messages/{saved['input_message_id']}"
        ]
        message["source_refs"] = [{"kind": "note", "resource_id": str(uuid4()), "version": 1}]
        self.execute(run)
        self.assertEqual(len([p for p in self.store.data if "/commands/" in p]), 1)

    def test_multistage_home_preamble_resume_replay_and_rejected_attempt_have_no_sse_text(
        self,
    ) -> None:
        calls = (
            ToolCall(call_key="read", name="notes.list", arguments_json="{}"),
            ToolCall(
                call_key="home",
                name="home.set_state",
                arguments_json=self.command_body.model_dump_json(),
            ),
        )
        self.configure_calls(calls)
        model = HomePreambleModel(calls)
        self.handler.model = model
        conversation = self.conversation()
        accepted = self.turn(conversation, text=intent(self.command_body))
        run = ResourceId(accepted.json()["run_id"])
        generate = self.handler._generate

        def stop_after_read(*args: Any, **kwargs: Any) -> Any:
            if args[3].get("model_step") == 1:
                raise PersistenceUnavailable()
            return generate(*args, **kwargs)

        with patch.object(self.handler, "_generate", side_effect=stop_after_read):
            with self.assertRaises(PersistenceUnavailable):
                self.execute(run)
        saved = self.store.data[f"tenants/{self.tenant}/runs/{run}"]
        self.assertEqual(saved["model_step"], 1)
        self.assertIn("UNOBSERVED_PHYSICAL_SUCCESS", saved["completed_text"])
        self.assertFalse(
            any(
                d["type"] == "text.delta"
                for p, d in self.store.data.items()
                if f"/runs/{run}/events/" in p
            )
        )
        self.clock.advance(6)
        delivery = self.delivery(run)
        self.worker.handle(LOCAL_AUTHORIZATION, delivery.model_dump(mode="json"))
        self.worker.handle(LOCAL_AUTHORIZATION, delivery.model_dump(mode="json"))
        self.assertEqual(self.get_run(run).json()["status"], "completed")
        self.assertEqual(len(model.requests), 2)
        self.assertEqual(len([p for p in self.store.data if "/commands/" in p]), 1)
        self.assertEqual(
            self.turn(conversation, text=intent(self.command_body)).json(), accepted.json()
        )
        sse = self.client.get(f"/v1/tenants/{self.tenant}/runs/{run}/events")
        self.assertEqual(sse.status_code, 200, sse.text)
        self.assertNotIn("UNOBSERVED_PHYSICAL_SUCCESS", sse.text)
        self.assertIn(HomeRuntime.UPDATE_ACK, sse.text)
        events = [
            d
            for p, d in self.store.data.items()
            if f"/runs/{run}/events/" in p and d["type"] == "text.delta"
        ]
        self.assertEqual("".join(d["payload"]["text"] for d in events), HomeRuntime.UPDATE_ACK)

        # Same multiple model steps, but no direct user authority for the write.
        self.configure_calls(calls)
        self.handler.model = HomePreambleModel(calls)
        conversation = self.conversation()
        rejected = ResourceId(
            self.turn(conversation, text="Do not " + intent(self.command_body)).json()["run_id"]
        )
        self.execute(rejected)
        sse = self.client.get(f"/v1/tenants/{self.tenant}/runs/{rejected}/events")
        self.assertEqual(self.get_run(rejected).json()["status"], "failed")
        self.assertNotIn("UNOBSERVED_PHYSICAL_SUCCESS", sse.text)
        self.assertNotIn(HomeRuntime.UPDATE_ACK, sse.text)
        self.assertEqual(len([p for p in self.store.data if "/commands/" in p]), 1)

    def test_home_enabled_ordinary_conversation_releases_final_text_with_read_tool(self) -> None:
        read = ToolCall(call_key="read", name="notes.list", arguments_json="{}")
        self.configure_calls(
            (
                read,
                ToolCall(
                    call_key="home",
                    name="home.set_state",
                    arguments_json=self.command_body.model_dump_json(),
                ),
            )
        )
        model = HomePreambleModel((read,))
        model.preamble = "ノートを確認します。"
        self.handler.model = model
        conversation = self.conversation()
        run = ResourceId(self.turn(conversation, text="ノートを説明して").json()["run_id"])
        self.execute(run)
        self.assertEqual(self.get_run(run).json()["status"], "completed")
        events = [
            d
            for p, d in self.store.data.items()
            if f"/runs/{run}/events/" in p and d["type"] == "text.delta"
        ]
        self.assertEqual(
            "".join(d["payload"]["text"] for d in events), model.preamble + model.final_text
        )
        self.assertFalse(any("/commands/" in p for p in self.store.data))

    def test_unaccepted_direct_home_completed_read_restart_and_replay_are_fixed(self) -> None:
        read = ToolCall(call_key="read", name="notes.list", arguments_json="{}")
        home = ToolCall(
            call_key="home",
            name="home.set_state",
            arguments_json=self.command_body.model_dump_json(),
        )
        for mode, home_allowed in (
            ("direct", True),
            ("read", True),
            ("resume", True),
            ("checkpoint", True),
            ("direct", False),
            ("resume", False),
        ):
            with self.subTest(mode=mode, home_allowed=home_allowed):
                self.configure_calls((read, home) if home_allowed else (read,))
                model = HomePreambleModel(() if mode == "direct" else (read,))
                model.final_text = "照明をつけました。家電の操作は成功しました。" * 80
                self.handler.model = model
                text = intent(self.command_body)
                if not home_allowed:
                    text = text.replace("Set home state ", "家電の状態を設定 ", 1)
                conversation = self.conversation()
                accepted = self.turn(conversation, text=text).json()
                run = ResourceId(accepted["run_id"])
                if mode == "resume":
                    generate = self.handler._generate

                    def stop_after_read(*args: Any, generate: Any = generate, **kwargs: Any) -> Any:
                        if args[3].get("model_step") == 1:
                            raise PersistenceUnavailable()
                        return generate(*args, **kwargs)

                    with patch.object(self.handler, "_generate", side_effect=stop_after_read):
                        with self.assertRaises(PersistenceUnavailable):
                            self.execute(run)
                    self.clock.advance(6)
                elif mode == "checkpoint":
                    with patch.object(self.jobs, "complete", side_effect=PersistenceUnavailable):
                        with self.assertRaises(PersistenceUnavailable):
                            self.execute(run)
                    self.assertEqual(len(model.requests), 2)
                    self.clock.advance(6)
                self.execute(run)
                saved = self.store.data[f"tenants/{self.tenant}/runs/{run}"]
                self.assertEqual(saved["status"], "completed")
                self.assertNotIn("home_command", saved)
                self.assertFalse(any("/commands/" in p for p in self.store.data))
                self.assertEqual(len(model.requests), 1 if mode == "direct" else 2)
                final = self.store.data[
                    f"tenants/{self.tenant}/conversations/{conversation}/messages/{saved['final_message_id']}"
                ]
                self.assertEqual(final["text"], HomeRuntime.NOT_ACCEPTED)
                self.assertIsNone(final["model_message"])
                self.assertIsNone(final["model_messages"])
                self.assertEqual(final["tool_bindings"], [])
                self.assertEqual(saved["continuation"], [])
                if mode != "direct":
                    self.assertTrue(saved["purge_dependencies"])
                self.assertEqual(self.turn(conversation, text=text).json(), accepted)
                sse = self.client.get(f"/v1/tenants/{self.tenant}/runs/{run}/events")
                repeated = self.client.get(f"/v1/tenants/{self.tenant}/runs/{run}/events")
                self.assertEqual(sse.status_code, 200, sse.text)
                self.assertEqual(sse.text, repeated.text)
                self.assertIn(HomeRuntime.NOT_ACCEPTED, sse.text)
                self.assertNotIn("UNOBSERVED_PHYSICAL_SUCCESS", sse.text)
                self.assertNotIn("照明をつけました", sse.text)
                self.assertNotIn(HomeRuntime.UPDATE_ACK, sse.text)
                history = self.client.get(
                    f"/v1/tenants/{self.tenant}/conversations/{conversation}/messages"
                )
                self.assertEqual(history.status_code, 200, history.text)
                self.assertNotIn("照明をつけました", history.text)

    def test_home_marker_without_command_cannot_release_accepted_ack_on_resume(self) -> None:
        home = ToolCall(
            call_key="home",
            name="home.set_state",
            arguments_json=self.command_body.model_dump_json(),
        )
        self.configure_calls((home,))
        model = HomePreambleModel((home,))
        self.handler.model = model
        conversation = self.conversation()
        run = ResourceId(self.turn(conversation, text=intent(self.command_body)).json()["run_id"])
        with patch.object(self.jobs, "complete", side_effect=PersistenceUnavailable):
            with self.assertRaises(PersistenceUnavailable):
                self.execute(run)
        saved = self.store.data[f"tenants/{self.tenant}/runs/{run}"]
        effect = saved["home_command"]["effect_id"]
        del self.store.data[f"tenants/{self.tenant}/commands/{effect}"]
        self.clock.advance(6)
        self.execute(run)
        sse = self.client.get(f"/v1/tenants/{self.tenant}/runs/{run}/events")
        self.assertNotIn(HomeRuntime.UPDATE_ACK, sse.text)
        self.assertNotIn("UNOBSERVED_PHYSICAL_SUCCESS", sse.text)
        self.assertEqual(len(model.requests), 1)
        self.assertNotIn("final_message_id", self.store.data[f"tenants/{self.tenant}/runs/{run}"])

    def test_intent_recognition_uses_exact_typed_admission_grammar(self) -> None:
        canonical = intent(self.command_body)
        arguments = HomeRuntime.parse_intent(canonical)
        self.assertIsNotNone(arguments)
        assert arguments is not None
        self.assertTrue(HomeRuntime.intent(canonical, arguments))
        for text in (
            "Do not " + canonical,
            "Explain " + canonical,
            canonical + " ",
            canonical.replace("40", "true"),
            "Set home state {}",
            "Set home state not JSON",
        ):
            self.assertIsNone(HomeRuntime.parse_intent(text), text)
