"""T-020/T-021: explicit consent, private delegation and live output/activity fences."""

from datetime import timedelta
from pathlib import Path
from typing import Annotated, cast
from uuid import uuid4

import yaml  # type: ignore[import-untyped]
from brainoid_api.auth.authorization import transaction_policy
from brainoid_api.auth.private_session_context import PrivateDeviceIdentity, private_session_context
from brainoid_api.auth.runtime import AuthServices, authenticated_identity, get_auth_services
from brainoid_api.common.transactions import Transaction
from brainoid_api.errors import ErrorCode
from brainoid_api.services.private_sessions import (
    PrivateSession,
    PrivateSessionCreate,
    PrivateSessions,
)
from brainoid_contracts import FirebaseUid, ResourceId
from brainoid_policy.evaluator import Evaluator
from brainoid_policy.models import (
    DeviceSubject,
    ManagementAction,
    ResourceKind,
    ResourceRef,
    UserSubject,
)
from brainoid_policy.scopes import PolicyDenied, ResourceGrant
from fastapi import Depends, FastAPI
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from test_device_pairing import KEY, DevicePairingFixture


class PrivateSessionTests(DevicePairingFixture):
    def setUp(self) -> None:
        super().setUp()
        pairing = self.create()
        self.device = self.confirm(pairing)
        self.store.data[f"tenants/{self.tenant}/devices/{self.device.id}"]["capabilities"] = [
            "display"
        ]
        self.credential = self.exchange(pairing)
        self.device_identity = self.repo.authenticate(self.credential.access_token)
        self.sessions = PrivateSessions(
            self.store, self.tenants, self.ledger, self.repo, now=self.clock.now
        )
        self.app = cast(FastAPI, self.client.app)
        self.app.dependency_overrides[get_auth_services] = lambda: AuthServices(
            tenants=self.tenants, devices=self.repo, private_sessions=self.sessions
        )
        self.uid = "demo-a-member"
        self.url = f"/v1/tenants/{self.tenant}/devices/{self.device.id}/private-sessions"
        self.body = PrivateSessionCreate(allowed_scopes=["notes"], acknowledge_shared_space=True)

        @self.app.get("/test-private-context")
        async def context(
            selected: Annotated[PrivateDeviceIdentity, Depends(private_session_context)],
        ) -> dict[str, str]:
            return {"session_id": selected.session_id, "device_id": selected.device.sub}

    def start(self, key: str = KEY, body: PrivateSessionCreate | None = None) -> PrivateSession:
        return self.sessions.start(
            self.identity(), self.tenant, self.device.id, body or self.body, key, "start-request"
        )

    def end(self, session: PrivateSession, key: str = KEY, version: int = 1) -> None:
        self.sessions.end(
            self.identity(), self.tenant, session.id, key, f'"{version}"', "end-request"
        )

    def snapshot(self) -> PrivateSession | None:
        return self.store.run(lambda tx: self.sessions.snapshot(tx, self.device_identity))

    def policy(self, tx: Transaction) -> Evaluator:
        return transaction_policy(
            tx, ledger=self.ledger, allow_untracked_bootstrap=True, now=self.clock.now
        )

    def note(self, owner: str | None = None) -> ResourceRef:
        note = ResourceId(str(uuid4()))
        self.store.data[f"tenants/{self.tenant}/notes/{note}"] = {
            "id": note,
            "tenant_id": self.tenant,
            "visibility": "private",
            "display_policy": "authenticated_session",
            "owner_user_id": owner or self.uid,
            "created_by": owner or self.uid,
            "version": 1,
            "deletion_epoch": 0,
            "deleted": False,
        }
        return ResourceRef(tenant_id=self.tenant, kind=ResourceKind.NOTE, id=note)

    def grant(self, session: PrivateSession, note: ResourceRef | None = None) -> ResourceGrant:
        ref = note or self.note()
        return self.store.run(
            lambda tx: self.policy(tx).resource(
                self.tenant,
                DeviceSubject(
                    device_id=self.device.id, connection_generation=1, private_session_id=session.id
                ),
                ref,
            )
        )

    def activity(
        self, session: PrivateSession, grant: ResourceGrant, activity: ResourceId | None = None
    ) -> PrivateSession:
        return self.store.run(
            lambda tx: self.sessions.accept_activity(
                tx, self.device_identity, session.id, activity or ResourceId(str(uuid4())), grant
            )
        )

    def test_http_contract_consent_etag_owner_and_replay_terminal_state(self) -> None:
        headers = {"Idempotency-Key": KEY}
        response = self.client.post(self.url, json=self.body.model_dump(), headers=headers)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.headers["etag"], '"1"')
        self.assertEqual(response.headers["cache-control"], "no-store")
        session = PrivateSession.model_validate(response.json())
        schema = yaml.safe_load(Path("specs/openapi.yaml").read_text())
        Draft202012Validator({"$ref": "#/components/schemas/PrivateSession", **schema}).validate(
            response.json()
        )
        self.assertEqual(session, self.start())
        self.fails(lambda: self.start(KEY + "new"), ErrorCode.CONFLICT)
        url = f"/v1/tenants/{self.tenant}/private-sessions/{session.id}"
        self.uid = "demo-a-owner"
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(
            self.client.delete(url, headers={**headers, "If-Match": '"1"'}).status_code, 404
        )
        self.fails(lambda: self.start(KEY + "other-user"), ErrorCode.CONFLICT)
        self.uid = "demo-a-member"
        self.assertEqual(self.client.get(url).json()["user_id"], self.uid)
        self.assertEqual(self.client.delete(url, headers=headers).status_code, 428)
        self.assertEqual(
            self.client.delete(url, headers={**headers, "If-Match": '"2"'}).status_code, 412
        )
        self.assertEqual(
            self.client.delete(url, headers={**headers, "If-Match": '"1"'}).status_code, 204
        )
        self.end(session)
        ended = self.start()
        self.assertEqual((ended.id, ended.status, ended.version), (session.id, "ended", 2))
        self.assertEqual(self.client.get(url).headers["etag"], '"2"')
        self.assertIsNone(self.snapshot())
        replacement = self.start(KEY + "new")
        self.assertNotEqual(replacement.id, session.id)
        self.end(session)  # old end receipt cannot terminate the replacement
        self.assertEqual(self.snapshot(), replacement)
        self.assertEqual(self.start().status, "ended")
        audits = [v for p, v in self.store.data.items() if "target_session_id" in v]
        self.assertEqual([v["result"] for v in audits], ["started", "ended", "started"])
        self.assertTrue(
            all(
                set(a) == {"actor_user_id", "target_session_id", "result", "request_id", "at"}
                for a in audits
            )
        )

    def test_inputs_forbid_delegating_management_and_require_explicit_consent(self) -> None:
        for scopes in (
            ["calendar.confirm"],
            ["memberships"],
            ["devices"],
            ["export"],
            ["delete"],
            [],
            ["notes", "notes"],
        ):
            result = self.client.post(
                self.url,
                json={"allowed_scopes": scopes, "acknowledge_shared_space": True},
                headers={"Idempotency-Key": KEY},
            )
            self.assertEqual(result.status_code, 422, result.text)
        for consent in (False, "true", 1, None):
            result = self.client.post(
                self.url,
                json={"allowed_scopes": ["notes"], "acknowledge_shared_space": consent},
                headers={"Idempotency-Key": KEY},
            )
            self.assertEqual(result.status_code, 422, result.text)
        self.assertEqual(self.client.post(self.url, json=self.body.model_dump()).status_code, 400)
        self.uid = "demo-b-owner"
        self.fails(self.start, ErrorCode.RESOURCE_NOT_FOUND)
        self.uid = "demo-a-member"
        self.fails(
            lambda: self.start(
                body=PrivateSessionCreate(
                    allowed_scopes=["calendar.read"], acknowledge_shared_space=True
                )
            ),
            ErrorCode.OPERATION_FORBIDDEN,
        )
        session = self.start()
        self.fails(
            lambda: self.start(
                body=PrivateSessionCreate(allowed_scopes=["tasks"], acknowledge_shared_space=True)
            ),
            ErrorCode.IDEMPOTENCY_CONFLICT,
        )
        subject = self.grant(session).principal.subject
        for action in ManagementAction:
            with self.assertRaises(PolicyDenied):
                self.store.run(
                    lambda tx: self.policy(tx).management(
                        self.tenant,
                        subject,
                        action,  # noqa: B023
                    )
                )
        for scope in ("calendar.confirm", "export", "delete", "tasks"):
            self.fails(
                lambda scope=scope: self.store.run(
                    lambda tx: self.sessions.current(
                        tx, self.device_identity, session.id, scope=scope
                    )
                ),
                ErrorCode.OPERATION_FORBIDDEN,
            )

    def test_header_requires_dedicated_current_bearer_and_correct_device_user_tenant(self) -> None:
        session = self.start()
        headers = {"X-Private-Session": session.id}
        self.assertEqual(self.client.get("/test-private-context", headers=headers).status_code, 401)
        headers["Authorization"] = "Bearer " + self.credential.access_token
        self.assertEqual(self.client.get("/test-private-context", headers=headers).status_code, 200)
        for value in ("garbage", str(uuid4())):
            result = self.client.get(
                "/test-private-context", headers={**headers, "X-Private-Session": value}
            )
            self.assertIn(result.status_code, (400, 404))
        self.assertEqual(
            self.client.get(
                "/test-private-context",
                headers=[*headers.items(), ("X-Private-Session", session.id)],
            ).status_code,
            400,
        )
        self.fails(
            lambda: self.store.run(
                lambda tx: self.sessions.current(
                    tx,
                    self.device_identity,
                    session.id,
                    expected_user_id=FirebaseUid("demo-a-owner"),
                )
            ),
            ErrorCode.OPERATION_FORBIDDEN,
        )
        self.uid = "demo-a-owner"
        other_pairing = self.create(key=KEY + "other-device")
        self.confirm(other_pairing, key=KEY + "other-device")
        other = self.exchange(other_pairing, KEY + "other-device")
        self.assertEqual(
            self.client.get(
                "/test-private-context",
                headers={**headers, "Authorization": "Bearer " + other.access_token},
            ).status_code,
            404,
        )
        self.app.dependency_overrides.pop(authenticated_identity)
        self.assertEqual(
            self.client.post(
                self.url, json=self.body.model_dump(), headers={**headers, "Idempotency-Key": KEY}
            ).status_code,
            401,
        )
        self.assertEqual(
            self.client.delete(
                f"/v1/tenants/{self.tenant}/private-sessions/{session.id}",
                headers={**headers, "Idempotency-Key": KEY, "If-Match": '"1"'},
            ).status_code,
            401,
        )

    def test_idle_boundary_heartbeat_snapshot_read_and_replays_do_not_refresh(self) -> None:
        session = self.start()
        original = self.clock.now()
        for seconds in (20, 20, 19.999):
            self.clock.advance(seconds)
            self.assertEqual(self.snapshot(), session)
            self.assertEqual(self.sessions.get(self.identity(), self.tenant, session.id), session)
            self.assertEqual(self.start(), session)
            self.repo.authenticate(self.credential.access_token)  # heartbeat credential checks
            self.sessions.context(self.device_identity, session.id)
        self.clock.advance(0.001)
        self.assertEqual(self.clock.now(), original + timedelta(seconds=60))
        self.fails(
            lambda: self.sessions.context(self.device_identity, session.id),
            ErrorCode.RESOURCE_NOT_FOUND,
        )
        self.assertIsNone(self.snapshot())
        expired = self.start()
        self.assertEqual((expired.id, expired.status), (session.id, "expired"))
        self.assertEqual(expired.expires_at, session.expires_at)
        replacement = self.start(KEY + "new")
        self.assertNotEqual(replacement.id, session.id)
        self.assertEqual(self.start().status, "expired")

    def test_activity_deduplication_absolute_limit_and_saved_output_receipts(self) -> None:
        session = self.start()
        grant = self.grant(session)
        activity = ResourceId(str(uuid4()))
        self.clock.advance(50)
        extended = self.activity(session, grant, activity)
        self.assertEqual(extended.idle_expires_at, session.created_at + timedelta(seconds=110))
        self.clock.advance(9)
        self.assertEqual(self.activity(session, grant, activity), extended)
        other = self.grant(session)
        self.fails(lambda: self.activity(session, other, activity), ErrorCode.IDEMPOTENCY_CONFLICT)
        for _ in range(4):
            self.clock.advance(50)
            extended = self.activity(session, grant)
        self.assertEqual(extended.idle_expires_at, session.expires_at)
        self.assertEqual(extended.version, 1)
        self.clock.advance(40.999)
        self.sessions.context(self.device_identity, session.id)
        self.clock.advance(0.001)
        self.assertEqual(self.clock.now(), session.expires_at)
        self.fails(lambda: self.activity(session, grant), ErrorCode.RESOURCE_NOT_FOUND)
        with self.assertRaises(PolicyDenied):
            self.store.run(lambda tx: self.policy(tx).worker(grant))
        self.assertIsNone(self.snapshot())

    def test_delayed_output_other_private_shared_mode_and_new_session_cannot_reuse(self) -> None:
        note = self.note()
        shared_device = DeviceSubject(device_id=self.device.id, connection_generation=1)
        with self.assertRaises(PolicyDenied):
            self.store.run(lambda tx: self.policy(tx).resource(self.tenant, shared_device, note))
        session = self.start()
        grant = self.grant(session, note)
        alien = self.note("demo-a-owner")
        with self.assertRaises(PolicyDenied):
            self.grant(session, alien)
        output = grant.principal.subject
        saved_output = self.store.run(
            lambda tx: self.policy(tx).resource(
                self.tenant,
                UserSubject(user_id=FirebaseUid(self.uid)),
                note,
                output=output,
            )
        )
        activity = ResourceId(str(uuid4()))
        self.activity(session, grant, activity)
        self.end(session)
        for receipt, destination in ((grant, None), (saved_output, output)):
            with self.assertRaises(PolicyDenied):
                self.store.run(
                    lambda tx: self.policy(tx).worker(receipt, output=destination)  # noqa: B023
                )
        self.fails(lambda: self.activity(session, grant, activity), ErrorCode.RESOURCE_NOT_FOUND)
        new = self.start(KEY + "new")
        self.fails(lambda: self.activity(new, grant), ErrorCode.OPERATION_FORBIDDEN)
        new_grant = self.grant(new, note)
        self.fails(lambda: self.activity(new, new_grant, activity), ErrorCode.IDEMPOTENCY_CONFLICT)

    def test_membership_account_device_and_slot_changes_permanently_fence_old_consent(self) -> None:
        session = self.start()
        member = next(
            v
            for p, v in self.store.data.items()
            if "/memberships/" in p and v["user_id"] == self.uid
        )
        member.update(role="admin", authorization_version=2)
        self.fails(
            lambda: self.sessions.context(self.device_identity, session.id),
            ErrorCode.RESOURCE_NOT_FOUND,
        )
        self.assertEqual(self.start().status, "expired")
        # A revoke/regrant to active does not upgrade a previously consented session.
        member = next(
            v
            for p, v in self.store.data.items()
            if "/memberships/" in p and v["user_id"] == self.uid
        )
        member.update(status="active", authorization_version=3)
        self.fails(
            lambda: self.sessions.context(self.device_identity, session.id),
            ErrorCode.RESOURCE_NOT_FOUND,
        )
        next_session = self.start(KEY + "next")
        profile = next(
            v
            for p, v in self.store.data.items()
            if p.startswith("users/") and v["user_id"] == self.uid
        )
        profile["version"] += 1
        self.fails(
            lambda: self.sessions.context(self.device_identity, next_session.id),
            ErrorCode.RESOURCE_NOT_FOUND,
        )
        self.assertIsNone(self.snapshot())
        current = self.start(KEY + "after-account")
        self.store.data[f"tenants/{self.tenant}/private_session_slots/{self.device.id}"] = {
            "session_id": str(uuid4())
        }
        self.fails(
            lambda: self.sessions.context(self.device_identity, current.id),
            ErrorCode.RESOURCE_NOT_FOUND,
        )

    def test_legacy_records_fail_closed_and_transaction_retry_is_atomic(self) -> None:
        self.store.retry_once = True
        session = self.start()
        self.assertEqual(len([p for p in self.store.data if "/private_sessions/" in p]), 1)
        path = f"tenants/{self.tenant}/private_sessions/{session.id}"
        del self.store.data[path]["membership_authorization_version"]
        self.fails(
            lambda: self.sessions.context(self.device_identity, session.id),
            ErrorCode.RESOURCE_NOT_FOUND,
        )
        self.fails(
            lambda: self.sessions.get(self.identity(), self.tenant, session.id),
            ErrorCode.OPERATION_FORBIDDEN,
        )

    def test_credential_revocation_between_admission_and_activity_is_rechecked(self) -> None:
        session = self.start()
        grant = self.grant(session)
        self.sessions.context(self.device_identity, session.id)
        self.store.data["device_credential_families/" + self.device_identity.family_id][
            "status"
        ] = "revoked"
        self.fails(lambda: self.activity(session, grant), ErrorCode.AUTHENTICATION_REQUIRED)
        self.fails(self.snapshot, ErrorCode.AUTHENTICATION_REQUIRED)
        self.assertEqual(
            self.sessions.get(self.identity(), self.tenant, session.id).idle_expires_at,
            session.idle_expires_at,
        )

    def test_opaque_unprovisioned_uid_is_bound_without_creating_a_profile(self) -> None:
        uid = "opaque/user?session#owner"
        member = next(
            v
            for p, v in self.store.data.items()
            if "/memberships/" in p and v["user_id"] == self.uid
        )
        member["user_id"] = uid
        self.uid = uid
        session = self.start()
        self.assertEqual(session.user_id, uid)
        self.assertEqual(self.sessions.context(self.device_identity, session.id).user_id, uid)
        self.assertFalse(
            any(
                p.startswith("users/") and v.get("user_id") == uid
                for p, v in self.store.data.items()
            )
        )
        self.end(session)
        self.assertIsNone(self.snapshot())
