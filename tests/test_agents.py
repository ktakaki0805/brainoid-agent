"""RUN-03 agent HTTP, optimistic updates, device/session and current authority."""

import copy
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import yaml  # type: ignore[import-untyped]
from brainoid_api.auth.runtime import AuthServices, authenticated_identity, get_auth_services
from brainoid_api.common.pagination import Cursors
from brainoid_api.errors import APIError, ErrorCode
from brainoid_api.repositories.agents import AgentPatch, Agents
from brainoid_api.repositories.tenants import TenantCreate
from brainoid_api.routes.agents import reader_identity
from brainoid_api.services.private_sessions import PrivateSessionCreate, PrivateSessions
from brainoid_contracts import ResourceId
from fastapi import FastAPI
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from test_device_pairing import DevicePairingFixture


class AgentTests(DevicePairingFixture):
    def setUp(self) -> None:
        super().setUp()
        self.agents = Agents(
            self.store,
            self.tenants,
            Cursors(b"a" * 32, now=self.clock.now),
            ledger=self.ledger,
            credentials=self.repo,
            now=self.clock.now,
        )
        self.app = cast(FastAPI, self.client.app)
        self.app.dependency_overrides[get_auth_services] = lambda: AuthServices(
            tenants=self.tenants, devices=self.repo, agents=self.agents
        )
        self.app.dependency_overrides[reader_identity] = lambda: self.identity()
        self.agent = ResourceId(
            next(
                d["id"]
                for p, d in self.store.data.items()
                if p.startswith(f"tenants/{self.tenant}/agents/")
            )
        )
        self.url = f"/v1/tenants/{self.tenant}/agents"
        self.path = f"tenants/{self.tenant}/agents/{self.agent}"

    def update(
        self,
        *,
        body: dict[str, Any] | None = None,
        key: str = "agent-test-key-001",
        version: str = '"1"',
    ) -> Any:
        return self.client.patch(
            self.url + "/" + self.agent,
            json=body or {"persona": "FIXTURE_PERSONA"},
            headers={"Idempotency-Key": key, "If-Match": version},
        )

    def test_initial_agent_contract_and_owner_admin_member_updates(self) -> None:
        tenant = self.tenants.create(
            self.identity(), TenantCreate(name="Fixture"), "new-agent-key-001"
        )
        initial = self.agents.list(self.identity(), tenant.id).items[0]
        self.assertEqual((initial.version, initial.persona, initial.allowed_tools), (1, "", ()))
        spec = yaml.safe_load(Path("specs/openapi.yaml").read_text())
        result = self.client.get(self.url + "/" + self.agent)
        Draft202012Validator(
            {"$ref": "#/components/schemas/Agent", "components": spec["components"]}
        ).validate(result.json())
        self.assertEqual(result.headers["etag"], '"1"')
        self.assertNotIn("prompt_version", result.json())
        self.store.retry_once = True
        first = self.update()
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.headers["etag"], '"2"')
        self.assertEqual(self.update().json(), first.json())
        self.assertEqual(self.update(body={"name": "Changed"}).status_code, 409)
        self.assertEqual(self.update(key="different-agent-key").status_code, 412)
        self.uid = "demo-a-admin"
        self.assertEqual(self.update(key="admin-agent-key-001", version='"2"').status_code, 200)
        self.uid = "demo-a-member"
        self.assertEqual(self.client.get(self.url).status_code, 200)
        self.assertEqual(self.update(key="member-agent-key-001", version='"3"').status_code, 403)

    def test_boundaries_replay_and_current_account(self) -> None:
        self.assertEqual(self.update().status_code, 200)
        self.store.data[f"tenants/{self.tenant}/memberships/{self.uid}"]["role"] = "member"
        self.assertEqual(self.update().status_code, 403)
        self.store.data[f"tenants/{self.tenant}/memberships/{self.uid}"]["status"] = "revoked"
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.uid = "demo-b-owner"
        self.assertEqual(self.client.get(self.url + "/" + self.agent).status_code, 404)
        self.assertEqual(self.update().status_code, 404)

    def test_input_rejects_authority_fields_nulls_and_duplicate_headers(self) -> None:
        for body in (
            {"persona": None},
            {"name": None},
            {"allowed_tools": ["admin"]},
            {"version": 9},
        ):
            self.assertEqual(self.update(body=body).status_code, 422)
        self.assertEqual(
            self.client.patch(
                self.url + "/" + self.agent, json={}, headers={"If-Match": '"1"'}
            ).status_code,
            422,
        )
        self.assertEqual(
            self.client.patch(
                self.url + "/" + self.agent,
                json={"persona": "x"},
                headers={"Idempotency-Key": "agent-test-key-001"},
            ).status_code,
            428,
        )
        self.assertEqual(
            self.client.get(self.url, headers={"X-Private-Session": str(uuid4())}).status_code, 400
        )
        self.assertEqual(
            self.client.get(
                self.url,
                headers=[("X-Private-Session", str(uuid4())), ("X-Private-Session", str(uuid4()))],
            ).status_code,
            400,
        )

    def test_cursor_binds_current_authority_and_agent_versions(self) -> None:
        other = copy.deepcopy(self.store.data[self.path])
        other["id"] = str(uuid4())
        self.store.data[f"tenants/{self.tenant}/agents/{other['id']}"] = other
        first = self.client.get(self.url, params={"limit": 1}).json()
        self.assertIsNotNone(first["next_cursor"])
        second = self.client.get(self.url, params={"limit": 1, "cursor": first["next_cursor"]})
        self.assertEqual(second.status_code, 200)
        self.assertNotEqual(first["items"][0]["id"], second.json()["items"][0]["id"])
        self.update()
        self.assertEqual(
            self.client.get(
                self.url, params={"limit": 1, "cursor": first["next_cursor"]}
            ).status_code,
            400,
        )

    def test_device_bearer_rechecked_per_transaction_and_cannot_manage(self) -> None:
        credentials = self.credentials()
        identity = self.repo.authenticate(credentials.access_token)
        del self.app.dependency_overrides[reader_identity]
        headers = {"Authorization": "Bearer " + credentials.access_token}
        read = self.client.get(self.url + "/" + self.agent, headers=headers)
        self.assertEqual(read.status_code, 200)
        self.assertEqual(
            read.json(),
            self.agents.get(self.identity(), self.tenant, self.agent).model_dump(mode="json"),
        )
        with self.assertRaises(APIError) as denied:
            self.agents.get(identity, self.other, self.agent)
        self.assertEqual(denied.exception.code, ErrorCode.RESOURCE_NOT_FOUND)
        del self.app.dependency_overrides[authenticated_identity]
        self.assertEqual(
            self.client.patch(
                self.url + "/" + self.agent,
                json={"persona": "Cannot manage"},
                headers={**headers, "Idempotency-Key": "device-agent-test-key", "If-Match": '"1"'},
            ).status_code,
            401,
        )
        # A saved verified identity is not enough after the family is invalidated.
        family_path = "device_credential_families/" + identity.family_id
        self.store.data[family_path]["status"] = "revoked"
        with self.assertRaises(APIError):
            self.agents.get(identity, self.tenant, self.agent)
        self.assertEqual(self.client.get(self.url, headers=headers).status_code, 401)

    def test_tracked_missing_ledger_and_opaque_uid(self) -> None:
        member = self.store.data[f"tenants/{self.tenant}/memberships/{self.uid}"]
        member["authorization_operation_id"] = str(uuid4())
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.uid = "Opaque / Agent User %"
        tenant = self.tenants.create(
            self.identity(), TenantCreate(name="Opaque"), "opaque-agent-key-001"
        )
        agent = self.agents.list(self.identity(), tenant.id).items[0]
        updated = self.agents.update(
            self.identity(), tenant.id, agent.id, AgentPatch(persona=""), "opaque-update-key", '"1"'
        )
        self.assertEqual(updated.version, 2)

    def test_device_session_header_is_current_and_bound_to_device(self) -> None:
        credentials = self.credentials()
        identity = self.repo.authenticate(credentials.access_token)
        sessions = PrivateSessions(
            self.store, self.tenants, self.ledger, self.repo, now=self.clock.now
        )
        session = sessions.start(
            self.identity(),
            self.tenant,
            identity.sub,
            PrivateSessionCreate(allowed_scopes=["conversation"], acknowledge_shared_space=True),
            "agent-private-session-key",
            "fixture-request",
        )
        del self.app.dependency_overrides[reader_identity]
        headers = {
            "Authorization": "Bearer " + credentials.access_token,
            "X-Private-Session": session.id,
        }
        self.assertEqual(self.client.get(self.url, headers=headers).status_code, 200)
        self.clock.advance(60)
        self.assertEqual(self.client.get(self.url, headers=headers).status_code, 404)
        self.assertEqual(
            self.client.get(self.url, headers={"X-Private-Session": session.id}).status_code, 401
        )
