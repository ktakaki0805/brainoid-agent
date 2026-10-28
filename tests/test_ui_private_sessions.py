"""UI10 device-local output stop: actual current API/snapshot/operation contracts."""

from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from brainoid_api.auth.runtime import authenticated_identity
from brainoid_api.errors import ERRORS
from brainoid_api.services.private_sessions import PrivateSession, PrivateSessionCreate
from brainoid_contracts import DisplayPolicy, ResourceId, Visibility
from brainoid_device.cloud_models import CloudError
from brainoid_device.cloud_wire import decode, encode
from brainoid_device.snapshot_sync import SnapshotSync
from brainoid_device.ui_bridge import GetConversation
from brainoid_runs.models import ConversationCreate, TurnCreate
from test_device_snapshot import SnapshotFixture


class PrivateOutputStopTests(SnapshotFixture):
    def start_private(self) -> tuple[ResourceId, ResourceId]:
        # Exercise the existing user API; no management role assumption or new scope.
        self.uid = "demo-a-member"
        with patch.dict(self.app.dependency_overrides, {authenticated_identity: self.identity}):
            response = self.client.post(
                f"/v1/tenants/{self.tenant}/devices/{self.device_id}/private-sessions",
                headers={"Idempotency-Key": str(uuid4())},
                json={"allowed_scopes": ["conversation"], "acknowledge_shared_space": True},
            )
        self.assertEqual(response.status_code, 201, response.text)
        session = ResourceId(response.json()["id"])
        conversation = self.conversations.create(
            self.identity(),
            self.tenant,
            ConversationCreate(
                agent_id=self.agent_id,
                visibility=Visibility.PRIVATE,
                display_policy=DisplayPolicy.AUTHENTICATED_SESSION,
            ),
            str(uuid4()),
        ).id
        self.conversations.turn(
            self.identity(),
            self.tenant,
            conversation,
            TurnCreate(
                text="SYNTHETIC PRIVATE", output_mode="text", output_device_id=self.device_id
            ),
            str(uuid4()),
        )
        return session, conversation

    async def test_stop_blocks_current_and_restarted_snapshot_and_operation_without_cloud_revoke(
        self,
    ) -> None:
        session, conversation = self.start_private()
        await self.sync.sync()
        before = self.cloud.output_generation
        self.assertEqual(
            (await self.sync.public())["snapshot"]["active_private_session"]["id"], session
        )
        self.sync.stop_output(session)
        self.assertGreater(self.cloud.output_generation, before)
        self.assertTrue(self.cloud.active)
        self.assertIsNotNone(self.cloud.journal.credentials)
        with self.assertRaises(CloudError):
            await self.bridge.operation(
                GetConversation(
                    operation="get_conversation",
                    conversation_id=conversation,
                    private_session_id=session,
                )
            )
        self.assertEqual(self.sessions.get(self.identity(), self.tenant, session).status, "active")
        resumed = SnapshotSync(self.cloud)
        value = await resumed.public()
        self.assertIsNone(value["snapshot"]["active_private_session"])
        self.assertEqual(value["snapshot"]["assigned_runs"], [])
        self.assertFalse(value["play_speech"])
        raw = self.private_store.read("private-output-stops-v1")
        self.assertNotIn(b"SYNTHETIC PRIVATE", raw)
        self.assertNotIn(b"access_token", raw)
        self.assertEqual(len(decode(raw)["sessions"]), 1)
        resumed.stop_output(session)  # Idempotent lost-local-response reconciliation.

    async def test_stopped_session_same_version_idle_update_does_not_undo_stop(self) -> None:
        session, _ = self.start_private()
        await self.sync.sync()
        self.sync.stop_output(session)
        path = f"tenants/{self.tenant}/private_sessions/{session}"
        self.store.data[path]["idle_expires_at"] = self.clock.now() + timedelta(seconds=90)
        value = await self.sync.public()
        self.assertIsNone(value["snapshot"]["active_private_session"])
        self.assertTrue(self.sync.stopped(session))
        self.clock.advance(300)
        self.assertFalse(self.sync.stopped(session))
        self.assertEqual(decode(self.private_store.read("private-output-stops-v1"))["sessions"], [])

    async def test_storage_failure_clears_current_output_and_does_not_claim_success(self) -> None:
        session, _ = self.start_private()
        await self.sync.sync()
        with patch.object(self.private_store, "write", side_effect=OSError("synthetic")):
            with self.assertRaises(CloudError):
                self.sync.stop_output(session)
        self.assertIsNone(self.sync.state)
        self.assertTrue(self.sync.stopped(session))
        self.sync.stop_output(session)
        resumed = SnapshotSync(self.cloud)
        self.assertTrue(resumed.stopped(session))

    async def test_wrong_device_binding_corrupt_or_rollback_stop_journal_fails_closed(self) -> None:
        session, _ = self.start_private()
        await self.sync.sync()
        self.sync.stop_output(session)
        raw = decode(self.private_store.read("private-output-stops-v1"))
        for change in (
            {"device_id": str(uuid4())},
            {"tenant_id": str(uuid4())},
            {"epoch": "other"},
            {"observed_at": self.clock.now().timestamp() + 1},
        ):
            self.private_store.write("private-output-stops-v1", encode({**raw, **change}))
            with self.assertRaises(CloudError):
                await SnapshotSync(self.cloud).public()
        self.private_store.write("private-output-stops-v1", b"invalid")
        with self.assertRaises(CloudError):
            await SnapshotSync(self.cloud).public()

    async def test_wrong_session_stop_cannot_hide_the_current_session(self) -> None:
        session, _ = self.start_private()
        await self.sync.sync()
        with self.assertRaises(CloudError):
            self.sync.stop_output(ResourceId(str(uuid4())))
        self.assertEqual(self.sync.session().id, session)  # type: ignore[union-attr]

    async def test_new_explicit_session_works_after_old_cloud_session_ended(self) -> None:
        session, _ = self.start_private()
        await self.sync.sync()
        self.sync.stop_output(session)
        self.sessions.end(self.identity(), self.tenant, session, str(uuid4()), '"1"', "ui10-end")
        next_session = self.sessions.start(
            self.identity(),
            self.tenant,
            self.device_id,
            PrivateSessionCreate(allowed_scopes=["conversation"], acknowledge_shared_space=True),
            str(uuid4()),
            "ui10-new",
        )
        await self.sync.sync()
        self.assertEqual(self.sync.session().id, next_session.id)  # type: ignore[union-attr]
        self.assertTrue(self.sync.stopped(session))


class OutputSessionPreconditionTests(SnapshotFixture):
    def prepare_output(self) -> tuple[PrivateSession, ResourceId]:
        self.uid = "demo-a-member"
        first = self.sessions.start(
            self.identity(),
            self.tenant,
            self.device_id,
            PrivateSessionCreate(allowed_scopes=["conversation"], acknowledge_shared_space=True),
            str(uuid4()),
            "ui10-consent",
        )
        conversation = self.conversations.create(
            self.identity(),
            self.tenant,
            ConversationCreate(
                agent_id=self.agent_id,
                visibility=Visibility.PRIVATE,
                display_policy=DisplayPolicy.AUTHENTICATED_SESSION,
            ),
            str(uuid4()),
        ).id
        return first, conversation

    def replace_session(self, first: PrivateSession) -> PrivateSession:
        self.sessions.end(
            self.identity(), self.tenant, first.id, str(uuid4()), f'"{first.version}"', "ui10-end"
        )
        return self.sessions.start(
            self.identity(),
            self.tenant,
            self.device_id,
            PrivateSessionCreate(allowed_scopes=["conversation"], acknowledge_shared_space=True),
            str(uuid4()),
            "ui10-reconsent",
        )

    async def test_delayed_request_without_precondition_uses_current_slot_for_legacy_compatibility(
        self,
    ) -> None:
        first, conversation = self.prepare_output()
        incoming = TurnCreate(
            text="LEGACY DELAYED", output_mode="text", output_device_id=self.device_id
        )
        replacement = self.replace_session(first)
        accepted = self.conversations.turn(
            self.identity(), self.tenant, conversation, incoming, str(uuid4())
        )
        snapshot = self.snapshots.page(self.device_identity).release()
        assigned = next(run for run in snapshot.assigned_runs if run.run_id == accepted.run_id)
        self.assertEqual(assigned.private_session_id, replacement.id)

    async def test_delayed_guarded_request_cannot_cross_to_new_consent_but_fresh_consent_can(
        self,
    ) -> None:
        from brainoid_api.errors import APIError

        first, conversation = self.prepare_output()
        incoming = TurnCreate(
            text="GUARDED DELAYED",
            output_mode="text",
            output_device_id=self.device_id,
            expected_output_private_session_id=first.id,
        )
        replacement = self.replace_session(first)
        with self.assertRaises(APIError) as rejected:
            self.conversations.turn(
                self.identity(), self.tenant, conversation, incoming, str(uuid4())
            )
        self.assertEqual(ERRORS[rejected.exception.code].status, 409)
        accepted = self.conversations.turn(
            self.identity(),
            self.tenant,
            conversation,
            incoming.model_copy(update={"expected_output_private_session_id": replacement.id}),
            str(uuid4()),
        )
        snapshot = self.snapshots.page(self.device_identity).release()
        self.assertEqual(
            next(
                run for run in snapshot.assigned_runs if run.run_id == accepted.run_id
            ).private_session_id,
            replacement.id,
        )

    async def test_exact_replay_is_retired_and_changing_only_precondition_conflicts_with_key(
        self,
    ) -> None:
        from brainoid_api.errors import APIError

        first, conversation = self.prepare_output()
        key = str(uuid4())
        body = TurnCreate(
            text="EXACT RETRY",
            output_mode="text",
            output_device_id=self.device_id,
            expected_output_private_session_id=first.id,
        )
        accepted = self.conversations.turn(self.identity(), self.tenant, conversation, body, key)
        self.assertEqual(
            self.conversations.turn(self.identity(), self.tenant, conversation, body, key).run_id,
            accepted.run_id,
        )
        replacement = self.replace_session(first)
        with self.assertRaises(APIError) as old:
            self.conversations.turn(self.identity(), self.tenant, conversation, body, key)
        self.assertEqual(ERRORS[old.exception.code].status, 409)
        with self.assertRaises(APIError) as changed:
            self.conversations.turn(
                self.identity(),
                self.tenant,
                conversation,
                body.model_copy(update={"expected_output_private_session_id": replacement.id}),
                key,
            )
        self.assertEqual(ERRORS[changed.exception.code].status, 403)

    async def test_device_principal_may_not_use_phone_precondition(self) -> None:
        from brainoid_api.errors import APIError

        first, conversation = self.prepare_output()
        with self.assertRaises(APIError) as rejected:
            self.conversations.turn(
                self.device_identity,
                self.tenant,
                conversation,
                TurnCreate(
                    text="DEVICE ATTEMPT",
                    output_mode="text",
                    output_device_id=self.device_id,
                    expected_output_private_session_id=first.id,
                ),
                str(uuid4()),
                first.id,
            )
        self.assertEqual(ERRORS[rejected.exception.code].status, 400)

    async def test_null_missing_output_and_unknown_session_are_not_consent(self) -> None:
        from brainoid_api.errors import APIError
        from pydantic import ValidationError

        first, conversation = self.prepare_output()
        for fields in (
            {"expected_output_private_session_id": first.id},
            {"output_device_id": self.device_id, "expected_output_private_session_id": None},
        ):
            with self.assertRaises(ValidationError):
                TurnCreate.model_validate({"text": "INVALID", "output_mode": "text", **fields})
        with self.assertRaises(APIError) as rejected:
            self.conversations.turn(
                self.identity(),
                self.tenant,
                conversation,
                TurnCreate(
                    text="UNKNOWN",
                    output_mode="text",
                    output_device_id=self.device_id,
                    expected_output_private_session_id=ResourceId(str(uuid4())),
                ),
                str(uuid4()),
            )
        self.assertEqual(ERRORS[rejected.exception.code].status, 409)

    async def test_same_authority_key_cannot_change_only_output_precondition(self) -> None:
        from brainoid_api.errors import APIError

        first, conversation = self.prepare_output()
        key = str(uuid4())
        body = TurnCreate(
            text="LEGACY RECEIPT", output_mode="text", output_device_id=self.device_id
        )
        accepted = self.conversations.turn(self.identity(), self.tenant, conversation, body, key)
        before = len(self.store.data)
        with self.assertRaises(APIError) as changed:
            self.conversations.turn(
                self.identity(),
                self.tenant,
                conversation,
                body.model_copy(update={"expected_output_private_session_id": first.id}),
                key,
            )
        self.assertEqual(ERRORS[changed.exception.code].status, 409)
        self.assertEqual(len(self.store.data), before)
        self.assertEqual(
            self.conversations.turn(self.identity(), self.tenant, conversation, body, key).run_id,
            accepted.run_id,
        )

    async def test_public_http_validates_precondition_and_rejects_delayed_session(self) -> None:
        from brainoid_api.routes.agents import reader_identity

        first, conversation = self.prepare_output()
        body = {
            "text": "HTTP DELAYED",
            "output_mode": "text",
            "output_device_id": self.device_id,
            "expected_output_private_session_id": first.id,
        }
        path = f"/v1/tenants/{self.tenant}/conversations/{conversation}/turns"
        replacement = self.replace_session(first)
        with patch.dict(self.app.dependency_overrides, {reader_identity: self.identity}):
            delayed = self.client.post(path, json=body, headers={"Idempotency-Key": str(uuid4())})
            self.assertEqual(delayed.status_code, 409)
            for invalid in (
                {**body, "expected_output_private_session_id": None},
                {key: value for key, value in body.items() if key != "output_device_id"},
            ):
                denied = self.client.post(
                    path, json=invalid, headers={"Idempotency-Key": str(uuid4())}
                )
                self.assertEqual(denied.status_code, 422)
            accepted = self.client.post(
                path,
                json={**body, "expected_output_private_session_id": replacement.id},
                headers={"Idempotency-Key": str(uuid4())},
            )
            self.assertEqual(accepted.status_code, 202)

    async def test_old_and_new_openapi_payload_sets_remain_compatible_and_guard_is_strict(
        self,
    ) -> None:
        import subprocess
        from pathlib import Path

        import yaml  # type: ignore[import-untyped]
        from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

        baseline = Path("specs/openapi-baseline.ref").read_text().strip()
        old = yaml.safe_load(
            subprocess.check_output(["git", "show", f"{baseline}:specs/openapi.yaml"])
        )
        new = yaml.safe_load(Path("specs/openapi.yaml").read_text())
        self.assertEqual(
            old["components"]["schemas"]["TurnCreate"]["oneOf"],
            new["components"]["schemas"]["TurnCreate"]["oneOf"][:2],
        )
        validators = [
            Draft202012Validator(
                {"$ref": "#/components/schemas/TurnCreate", "components": source["components"]}
            )
            for source in (old, new)
        ]
        for payload in (
            {"text": "LEGACY", "output_mode": "text"},
            {"text": "LEGACY", "output_mode": "text", "output_device_id": str(self.device_id)},
            {"audio_input_id": str(uuid4()), "output_mode": "speech"},
        ):
            for validator in validators:
                validator.validate(payload)
        guarded = {
            "text": "GUARDED",
            "output_mode": "text",
            "output_device_id": str(self.device_id),
            "expected_output_private_session_id": str(uuid4()),
        }
        self.assertFalse(validators[0].is_valid(guarded))
        validators[1].validate(guarded)
        self.assertFalse(
            validators[1].is_valid({**guarded, "expected_output_private_session_id": None})
        )
        self.assertFalse(
            validators[1].is_valid(
                {key: value for key, value in guarded.items() if key != "output_device_id"}
            )
        )
