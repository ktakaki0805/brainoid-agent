"""T-020/T-021 real Firestore serialization, durable receipts and current authority."""

from datetime import timedelta
from functools import partial
from typing import Any
from unittest.mock import patch
from uuid import uuid4

from brainoid_api.auth.authorization import transaction_policy
from brainoid_api.errors import APIError, ErrorCode
from brainoid_api.services.private_sessions import (
    PrivateSession,
    PrivateSessionCreate,
    PrivateSessions,
)
from brainoid_contracts import FirebaseUid, ResourceId
from brainoid_policy.models import DeviceSubject, ResourceKind, ResourceRef
from brainoid_policy.scopes import PolicyDenied, ResourceGrant
from google.api_core.exceptions import Aborted
from test_device_pairing_integration import KEY, DevicePairingIntegrationFixture


class PrivateSessionsIntegrationTests(DevicePairingIntegrationFixture):
    def setUp(self) -> None:
        super().setUp()
        pairing = self.pairing()
        self.device = self.confirm(pairing)
        credentials = self.exchange(pairing)
        self.device_identity = self.repo.authenticate(credentials.access_token)
        self.sessions = PrivateSessions(
            self.store, self.tenants, self.ledger, self.repo, now=lambda: self.now
        )
        self.session_body = PrivateSessionCreate(
            allowed_scopes=["notes"], acknowledge_shared_space=True
        )

    def start(self, key: str = KEY, uid: str = "demo-a-owner") -> PrivateSession:
        identity = self.identity().model_copy(update={"user_id": FirebaseUid(uid)})
        return self.sessions.start(
            identity, self.tenant, self.device.id, self.session_body, key, "start"
        )

    def end(self, session: PrivateSession, key: str = KEY) -> None:
        self.sessions.end(self.identity(), self.tenant, session.id, key, '"1"', "end")

    def snapshot(self) -> PrivateSession | None:
        return self.store.run(lambda tx: self.sessions.snapshot(tx, self.device_identity))

    def grant(self, session: PrivateSession) -> ResourceGrant:
        note = ResourceId(str(uuid4()))
        self.db.document(f"tenants/{self.tenant}/notes/{note}").set(
            {
                "id": note,
                "tenant_id": self.tenant,
                "visibility": "private",
                "display_policy": "authenticated_session",
                "owner_user_id": "demo-a-owner",
                "created_by": "demo-a-owner",
                "version": 1,
                "deletion_epoch": 0,
                "deleted": False,
            }
        )
        return self.store.run(
            lambda tx: transaction_policy(
                tx,
                ledger=self.ledger,
                allow_untracked_bootstrap=True,
                now=lambda: self.now,
            ).resource(
                self.tenant,
                DeviceSubject(
                    device_id=self.device.id, connection_generation=1, private_session_id=session.id
                ),
                ResourceRef(tenant_id=self.tenant, kind=ResourceKind.NOTE, id=note),
            )
        )

    def activity(
        self, session: PrivateSession, grant: ResourceGrant, operation: ResourceId
    ) -> PrivateSession:
        return self.store.run(
            lambda tx: self.sessions.accept_activity(
                tx,
                self.device_identity,
                session.id,
                operation,
                grant,
            )
        )

    def test_concurrent_identical_start_is_one_session_and_one_receipt(self) -> None:
        results = self.concurrent(self.start, self.start)
        self.assertEqual(results[0], results[1])
        self.assertIsInstance(results[0], PrivateSession)
        self.assertEqual(
            len(list(self.db.collection(f"tenants/{self.tenant}/private_sessions").stream())), 1
        )
        self.assertEqual(
            len(
                list(self.db.collection(f"tenants/{self.tenant}/private_session_requests").stream())
            ),
            1,
        )

    def test_distinct_users_compete_for_one_slot_without_overwrite(self) -> None:
        results = self.concurrent(self.start, partial(self.start, KEY + "other", "demo-a-member"))
        self.assertEqual(sum(isinstance(r, PrivateSession) for r in results), 1, results)
        self.assertIn(ErrorCode.CONFLICT, results)
        active = self.snapshot()
        assert active is not None
        loser = "demo-a-member" if active.user_id == "demo-a-owner" else "demo-a-owner"
        self.fails(
            lambda: self.sessions.end(
                self.identity().model_copy(update={"user_id": FirebaseUid(loser)}),
                self.tenant,
                active.id,
                KEY,
                '"1"',
                "wrong-owner",
            ),
            ErrorCode.RESOURCE_NOT_FOUND,
        )
        self.assertEqual(self.snapshot(), active)

    def test_end_and_activity_serialize_and_no_late_output_survives(self) -> None:
        session = self.start()
        grant = self.grant(session)
        self.now += timedelta(seconds=50)
        results = self.concurrent(
            partial(self.end, session),
            partial(self.activity, session, grant, ResourceId(str(uuid4()))),
        )
        self.assertIn(None, results)
        self.assertTrue(
            all(
                r is None or isinstance(r, PrivateSession) or r == ErrorCode.RESOURCE_NOT_FOUND
                for r in results
            ),
            results,
        )
        self.assertIsNone(self.snapshot())
        self.assertEqual(self.start().status, "ended")
        with self.assertRaises(PolicyDenied):
            self.store.run(
                lambda tx: transaction_policy(
                    tx,
                    ledger=self.ledger,
                    allow_untracked_bootstrap=True,
                    now=lambda: self.now,
                ).worker(grant)
            )
        self.fails(
            lambda: self.activity(session, grant, ResourceId(str(uuid4()))),
            ErrorCode.RESOURCE_NOT_FOUND,
        )

    def test_commit_response_loss_replays_terminal_current_state(self) -> None:
        original = self.store.run
        calls = 0

        def lose(work: Any) -> Any:
            nonlocal calls
            result = original(work)
            calls += 1
            if calls == 1:
                raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)
            return result

        with patch.object(self.store, "run", side_effect=lose):
            self.fails(self.start, ErrorCode.TEMPORARILY_UNAVAILABLE)
        session = self.start()
        calls = 0
        with patch.object(self.store, "run", side_effect=lose):
            self.fails(partial(self.end, session), ErrorCode.TEMPORARILY_UNAVAILABLE)
        self.end(session)
        self.assertEqual(self.start().status, "ended")
        replacement = self.start(KEY + "new")
        self.assertNotEqual(replacement.id, session.id)
        self.end(session)
        self.assertEqual(self.snapshot(), replacement)

    def test_expiry_cleanup_start_race_and_replayed_activity_do_not_extend(self) -> None:
        session = self.start()
        grant = self.grant(session)
        activity = ResourceId(str(uuid4()))
        self.now += timedelta(seconds=50)
        results = self.concurrent(
            partial(self.activity, session, grant, activity),
            partial(self.activity, session, grant, activity),
        )
        self.assertEqual(results[0], results[1])
        self.now += timedelta(seconds=10)
        self.assertEqual(
            self.activity(session, grant, activity).idle_expires_at,
            session.created_at + timedelta(seconds=110),
        )
        self.now += timedelta(seconds=50)
        results = self.concurrent(self.snapshot, partial(self.start, KEY + "new"))
        replacement = self.start(KEY + "new")
        self.assertNotEqual(replacement.id, session.id)
        self.assertTrue(all(r is None or isinstance(r, PrivateSession) for r in results), results)
        self.assertEqual(self.start().status, "expired")
        self.assertEqual(self.snapshot(), replacement)

    def test_read_abort_restarts_activity_without_extending_its_replay(self) -> None:
        session = self.start()
        grant = self.grant(session)
        activity = ResourceId(str(uuid4()))
        self.now += timedelta(seconds=50)
        read = self.db._firestore_api.batch_get_documents
        attempts = 0

        def abort_once(*args: Any, **kwargs: Any) -> Any:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise Aborted("synthetic pre-commit read conflict")  # type: ignore[no-untyped-call]
            return read(*args, **kwargs)

        with patch.object(self.db._firestore_api, "batch_get_documents", abort_once):
            updated = self.activity(session, grant, activity)
        self.assertGreater(attempts, 1)
        self.assertEqual(updated.idle_expires_at, session.created_at + timedelta(seconds=110))
        self.now += timedelta(seconds=10)
        self.assertEqual(self.activity(session, grant, activity), updated)
        self.assertEqual(
            len(
                list(
                    self.db.collection(f"tenants/{self.tenant}/private_session_activities").stream()
                )
            ),
            1,
        )

    def test_membership_regrant_and_device_generation_reject_old_session(self) -> None:
        session = self.start()
        path = f"tenants/{self.tenant}/memberships/demo-a-owner"
        self.db.document(path).update({"authorization_version": 2, "role": "admin"})
        self.fails(
            lambda: self.sessions.context(self.device_identity, session.id),
            ErrorCode.RESOURCE_NOT_FOUND,
        )
        self.db.document(path).update({"authorization_version": 3, "role": "owner"})
        self.fails(
            lambda: self.sessions.context(self.device_identity, session.id),
            ErrorCode.RESOURCE_NOT_FOUND,
        )
        self.assertIsNone(self.snapshot())
        replacement = self.start(KEY + "new")
        self.db.document(f"tenants/{self.tenant}/devices/{self.device.id}").update(
            {"connection_generation": 2}
        )
        with self.assertRaises(APIError):
            self.sessions.context(self.device_identity, replacement.id)
