"""Actual paired device credentials and home.control-only sessions at the HTTP boundary."""

import copy
from datetime import timedelta
from typing import Any, cast
from unittest.mock import patch
from uuid import uuid4

from brainoid_agent_core.types import ToolCall
from brainoid_api.auth.runtime import AuthServices, get_auth_services
from brainoid_api.common.pagination import Cursors
from brainoid_api.errors import APIError
from brainoid_api.repositories.commands import CommandRequests
from brainoid_api.repositories.device_credentials import PairingConfirm
from brainoid_api.repositories.devices import DevicePatch, Devices
from brainoid_api.routes.agents import reader_identity
from brainoid_api.services.private_sessions import PrivateSessionCreate
from brainoid_contracts import ResourceId
from brainoid_memory.commands import command_path
from brainoid_memory.export_projection import project
from brainoid_persistence.firestore import PersistenceUnavailable
from brainoid_policy.control_ledger import OperationScope
from brainoid_policy.scopes import PolicyDenied
from brainoid_worker.home import HomeRuntime
from fakes.home import home_fixture
from fastapi import FastAPI
from test_commands import intent
from test_notes import NotesWorkerFixture


class CommandsDeviceTests(NotesWorkerFixture):
    def paired_home(
        self, *, conversation_scope: bool = False
    ) -> tuple[Any, Any, dict[str, str], str]:
        body, documents = home_fixture(self.tenant, self.identity().user_id, self.clock.now())
        self.store.data.update(documents)
        management = Devices(
            self.store,
            self.tenants,
            self.ledger,
            Cursors(b"d" * 32, now=self.clock.now),
            now=self.clock.now,
        )
        scopes: list[OperationScope] = ["shared", "home.control"]
        if conversation_scope:
            scopes.append("conversation")
        old_pair = self.create("h" * 32, "home-first-pairing")
        old = self.confirm(old_pair)
        old_credentials = self.exchange(old_pair)
        updated = management.update(
            self.identity(),
            self.tenant,
            old.id,
            DevicePatch(
                allowed_scopes=scopes,
                allowed_entity_ids=[body.target_entity_id],
            ),
            "home-permission-change",
            '"1"',
            "request",
        )
        management.revoke(
            self.identity(),
            self.tenant,
            old.id,
            "home-explicit-revoke",
            f'"{updated.version}"',
            "request",
        )
        pair = self.create("h" * 32, "home-second-pairing")
        new = self.repo.confirm(
            self.identity(),
            self.tenant,
            PairingConfirm(
                user_code=pair.user_code,
                device_name="Home Pi",
                allowed_entity_ids=[body.target_entity_id],
                replaces_device_id=old.id,
            ),
            "home-repair-confirm",
            "127.0.0.1",
        )
        credentials = self.exchange(pair, "home-repair-exchange")
        body = body.model_copy(update={"device_id": new.id})
        for path, doc in self.store.data.items():
            if path in documents and ("/home_targets/" in path or "/integrations/" in path):
                doc["device_id"] = new.id
        app = cast(FastAPI, self.client.app)
        app.dependency_overrides[get_auth_services] = lambda: AuthServices(
            tenants=self.tenants,
            devices=self.repo,
            memories=self.memories,
            conversations=self.conversations,
        )
        del app.dependency_overrides[reader_identity]
        url = f"/v1/tenants/{self.tenant}/commands"
        headers = {
            "Authorization": "Bearer " + credentials.access_token,
            "Idempotency-Key": "home-device-create",
        }
        response = self.client.post(url, json=body.model_dump(mode="json"), headers=headers)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(set(new.allowed_scopes), set(scopes))
        self.assertEqual(
            self.client.get(url + "/" + response.json()["id"], headers=headers).status_code, 200
        )
        self.assertNotEqual(
            self.client.post(
                url,
                json=body.model_dump(mode="json"),
                headers={**headers, "Authorization": "Bearer " + old_credentials.access_token},
            ).status_code,
            202,
        )

        return body, credentials, headers, response.json()["id"]

    def test_home_only_device_and_private_session_without_memory_scope_then_old_credential_denial(
        self,
    ) -> None:
        body, _, headers, _ = self.paired_home()
        url = f"/v1/tenants/{self.tenant}/commands"
        entity = self.store.data[f"tenants/{self.tenant}/entities/{body.target_entity_id}"]
        entity.update(
            visibility="private",
            display_policy="authenticated_session",
            owner_user_id=self.identity().user_id,
            version=2,
        )
        self.assertEqual(
            self.client.post(
                url,
                json=body.model_dump(mode="json"),
                headers={**headers, "Idempotency-Key": "home-device-private"},
            ).status_code,
            404,
        )
        session = self.sessions.start(
            self.identity(),
            self.tenant,
            body.device_id,
            PrivateSessionCreate(allowed_scopes=["home.control"], acknowledge_shared_space=True),
            "home-only-session-key",
            "request",
        )
        private_headers = {
            **headers,
            "Idempotency-Key": "home-private-session",
            "X-Private-Session": session.id,
        }
        private = self.client.post(url, json=body.model_dump(mode="json"), headers=private_headers)
        self.assertEqual(private.status_code, 202, private.text)
        command = ResourceId(private.json()["id"])
        self.assertEqual(
            self.client.get(url + "/" + command, headers=private_headers).status_code, 200
        )
        self.clock.advance(61)
        self.assertNotEqual(
            self.client.get(url + "/" + command, headers=private_headers).status_code, 200
        )

    def test_normal_refresh_keeps_binding_but_reuse_revokes_owner_read_and_export(self) -> None:
        body, credentials, headers, identity = self.paired_home()
        command = ResourceId(identity)
        commands = CommandRequests(self.memories)
        path = command_path(self.tenant, command)
        binding = copy.deepcopy(self.store.data[path]["actor_credential"])
        self.assertEqual(
            binding["family_id"], self.tokens.verify(credentials.access_token).family_id
        )
        refreshed = self.refresh(self.refresh_body(credentials), "home-refresh-normal")
        self.assertEqual(commands.get(self.identity(), self.tenant, command).status, "queued")
        replay = self.client.post(
            f"/v1/tenants/{self.tenant}/commands",
            json=body.model_dump(mode="json"),
            headers={**headers, "Authorization": "Bearer " + refreshed.access_token},
        )
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertEqual(replay.json()["id"], identity)
        self.assertEqual(self.store.data[path]["actor_credential"], binding)

        def exported() -> Any:
            return self.store.run(
                lambda tx: project(
                    tx,
                    self.memories._open(tx, self.identity(), self.tenant, None),
                    path,
                    scope="visible_tenant_data",
                )
            )

        self.assertIsNotNone(exported())
        with self.assertRaises(APIError):
            self.refresh(self.refresh_body(credentials), "home-refresh-reuse")
        self.assertEqual(
            self.store.data["device_credential_families/" + binding["family_id"]]["status"],
            "revoked",
        )
        self.assertEqual(
            self.client.get(
                f"/v1/tenants/{self.tenant}/commands/{identity}", headers=headers
            ).status_code,
            401,
        )
        with self.assertRaises(APIError):
            commands.get(self.identity(), self.tenant, command)
        self.assertIsNone(exported())
        self.assertEqual(self.store.data[path]["actor_credential"], binding)

    def test_original_family_expiry_epoch_replacement_and_missing_evidence_fail_closed(
        self,
    ) -> None:
        _, credentials, _, identity = self.paired_home()
        commands = CommandRequests(self.memories)
        command = ResourceId(identity)
        path = command_path(self.tenant, command)
        family_path = (
            "device_credential_families/" + self.tokens.verify(credentials.access_token).family_id
        )
        original = copy.deepcopy(self.store.data)
        for changes in (
            {"expires_at": self.clock.now()},
            {"expires_at": self.clock.now() + timedelta(days=31)},
            {"epoch": str(uuid4())},
            {"device_id": str(uuid4())},
            {"connection_generation": 2},
            {"authorization_version": 999},
        ):
            with self.subTest(changes=changes):
                self.store.data = copy.deepcopy(original)
                self.store.data[family_path].update(changes)
                with self.assertRaises(APIError):
                    commands.get(self.identity(), self.tenant, command)
        self.store.data = copy.deepcopy(original)
        del self.store.data[path]["actor_credential"]
        with self.assertRaises(APIError):
            commands.get(self.identity(), self.tenant, command)
        self.store.data = copy.deepcopy(original)
        # Restore an entirely valid old DB snapshot, but retain a newer external epoch.
        self.tokens.epoch = ResourceId(str(uuid4()))
        with self.assertRaises(APIError):
            commands.get(self.identity(), self.tenant, command)
        # The same common boundary is used by terminal ACK/reconcile and future claim.
        with self.assertRaises(PolicyDenied):
            self.store.run(
                lambda tx: commands.service.get(
                    self.memories._open(tx, self.identity(), self.tenant, None), command
                )
            )

    def test_device_default_home_terminal_resume_keeps_original_family_after_refresh(self) -> None:
        body, credentials, headers, _ = self.paired_home(conversation_scope=True)
        self.configure_calls(
            (
                ToolCall(
                    call_key="home", name="home.set_state", arguments_json=body.model_dump_json()
                ),
            )
        )
        self.store.data[f"tenants/{self.tenant}/devices/{body.device_id}"]["capabilities"] = [
            "display"
        ]
        self.app.dependency_overrides[reader_identity] = self.identity
        conversation = self.conversation()
        del self.app.dependency_overrides[reader_identity]
        response = self.client.post(
            self.url + f"/{conversation}/turns",
            json={"text": intent(body), "output_mode": "text"},
            headers={**headers, "Idempotency-Key": "device-home-run-turn"},
        )
        self.assertEqual(response.status_code, 202, response.text)
        run = ResourceId(response.json()["run_id"])
        with patch.object(self.jobs, "complete", side_effect=PersistenceUnavailable()):
            with self.assertRaises(PersistenceUnavailable):
                self.execute(run)
        saved = self.store.data[f"tenants/{self.tenant}/runs/{run}"]
        command = ResourceId(saved["home_command"]["effect_id"])
        original = self.store.data[command_path(self.tenant, command)]["actor_credential"]
        self.assertEqual(
            original["family_id"], self.tokens.verify(credentials.access_token).family_id
        )
        self.refresh(self.refresh_body(credentials), "home-resume-refresh")
        self.clock.advance(6)
        self.execute(run)
        self.assertEqual(
            self.conversations.get_run(self.identity(), self.tenant, run).status, "completed"
        )
        self.assertEqual(
            self.store.data[command_path(self.tenant, command)]["actor_credential"], original
        )
        self.assertEqual(len(self.model.requests), 1)
        events = [
            d
            for p, d in self.store.data.items()
            if f"/runs/{run}/events/" in p and d["type"] == "text.delta"
        ]
        self.assertEqual("".join(d["payload"]["text"] for d in events), HomeRuntime.UPDATE_ACK)

    def test_device_default_home_terminal_reuse_denies_revoked_origin_without_ack(self) -> None:
        body, credentials, headers, _ = self.paired_home(conversation_scope=True)
        self.configure_calls(
            (
                ToolCall(
                    call_key="home", name="home.set_state", arguments_json=body.model_dump_json()
                ),
            )
        )
        self.store.data[f"tenants/{self.tenant}/devices/{body.device_id}"]["capabilities"] = [
            "display"
        ]
        self.app.dependency_overrides[reader_identity] = self.identity
        conversation = self.conversation()
        del self.app.dependency_overrides[reader_identity]
        response = self.client.post(
            self.url + f"/{conversation}/turns",
            json={"text": intent(body), "output_mode": "text"},
            headers={**headers, "Idempotency-Key": "device-home-revoked-turn"},
        )
        self.assertEqual(response.status_code, 202, response.text)
        run = ResourceId(response.json()["run_id"])
        with patch.object(self.jobs, "complete", side_effect=PersistenceUnavailable()):
            with self.assertRaises(PersistenceUnavailable):
                self.execute(run)
        saved = self.store.data[f"tenants/{self.tenant}/runs/{run}"]
        command = ResourceId(saved["home_command"]["effect_id"])
        self.refresh(self.refresh_body(credentials), "home-terminal-refresh")
        with self.assertRaises(APIError):
            self.refresh(self.refresh_body(credentials), "home-terminal-reuse")
        with self.assertRaises(APIError):
            CommandRequests(self.memories).get(self.identity(), self.tenant, command)
        self.clock.advance(6)
        # prepare_dispatch may already obsolete the revoked job; either way no ACK is released.
        from brainoid_jobs.models import job_id
        from brainoid_worker.local import LOCAL_AUTHORIZATION

        delivery = self.jobs.prepare_dispatch(self.tenant, job_id(self.tenant, "run_turn", run))
        if delivery is not None:
            self.worker.handle(LOCAL_AUTHORIZATION, delivery.model_dump(mode="json"))
        events = [
            d
            for p, d in self.store.data.items()
            if f"/runs/{run}/events/" in p and d["type"] == "text.delta"
        ]
        self.assertEqual(events, [])
        self.assertEqual(self.store.data[command_path(self.tenant, command)]["status"], "queued")
