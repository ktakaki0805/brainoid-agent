"""Numeric Secret Manager versions, immutable slots and durable erasure fences."""

import tempfile
import unittest
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from uuid import uuid4

from brainoid_api.adapters.calendar_secrets import LocalCalendarSecrets
from brainoid_api.adapters.google_calendar_secrets import GoogleCalendarSecrets
from brainoid_contracts import FirebaseUid, ResourceId
from brainoid_contracts.deletion import subject_id
from brainoid_memory.calendar_oauth import SecretUnavailable
from brainoid_memory.deletion_ledger import DeletionIntent
from fakes.runtime import FakeClock

TENANT = ResourceId("00000000-0000-0000-0000-000000000001")
USER = FirebaseUid("secret-owner")


class FakeSecretManager:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.destroyed: set[str] = set()
        self.adds = 0
        self.accesses: list[str] = []
        self.destroys: list[str] = []
        self.fail_after = False
        self.fail_before = False
        self.before: Callable[[], None] = lambda: None

    def create(self, name: str) -> None:
        pass

    def add(self, name: str, payload: bytes) -> str:
        self.adds += 1
        self.before()
        if self.fail_before:
            raise RuntimeError("SYNTHETIC_REMOTE_SECRET")
        reference = name + "/versions/1"
        if reference in self.values or reference in self.destroyed:
            raise AssertionError("Blind duplicate add")
        self.values[reference] = payload
        if self.fail_after:
            raise RuntimeError("SYNTHETIC_REMOTE_SECRET")
        return reference

    def access(self, reference: str) -> bytes:
        self.accesses.append(reference)
        if reference not in self.values:
            raise SecretUnavailable()
        return self.values[reference]

    def destroy(self, reference: str) -> None:
        self.destroys.append(reference)
        if reference not in self.values and reference not in self.destroyed:
            raise SecretUnavailable()
        self.values.pop(reference, None)
        self.destroyed.add(reference)


class CalendarSecretsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.registry = LocalCalendarSecrets(self.directory, b"s" * 32, now=self.clock.now)
        self.transport = FakeSecretManager()
        self.secret = GoogleCalendarSecrets(
            self.registry, self.transport, project="synthetic-project"
        )
        self.slot = ResourceId(str(uuid4()))

    def put(
        self, slot: ResourceId | None = None, content: bytes = b"SYNTHETIC_REMOTE_SECRET"
    ) -> str:
        return self.secret.put(
            slot or self.slot,
            tenant=TENANT,
            user=USER,
            binding="b" * 64,
            content=content,
            expires_at=None,
        )

    def intent(
        self, kind: str, *, tenant: ResourceId = TENANT, user: FirebaseUid = USER
    ) -> DeletionIntent:
        subject = subject_id(user)
        return DeletionIntent.model_validate(
            {
                "kind": kind,
                "operation_id": str(uuid4()),
                "tenant_id": subject if kind == "delete_account" else tenant,
                "request_hash": "a" * 64,
                "source_policy": "delete_source",
                "created_at": self.clock.now(),
                "targets": [],
                "scopes": [
                    {
                        "kind": {
                            "delete_account": "account",
                            "delete_tenant": "tenant",
                            "leave_tenant": "member",
                        }[kind],
                        "resource_id": tenant if kind == "delete_tenant" else subject,
                        "tenant_id": None if kind == "delete_account" else tenant,
                        "version": 1,
                    }
                ],
            }
        )

    def test_exact_version_and_first_value_survive_restart_without_local_plaintext(self) -> None:
        reference = self.put()
        self.assertTrue(reference.endswith("/versions/1"))
        self.assertEqual(self.put(content=b"other candidate"), reference)
        self.assertEqual(self.secret.get(reference), b"SYNTHETIC_REMOTE_SECRET")
        self.assertEqual(self.transport.adds, 1)
        self.secret = GoogleCalendarSecrets(
            self.registry, self.transport, project="synthetic-project"
        )
        self.assertEqual(self.secret.get(reference), b"SYNTHETIC_REMOTE_SECRET")
        self.assertNotIn(b"SYNTHETIC_REMOTE_SECRET", self.registry.path.read_bytes())
        with self.assertRaises(SecretUnavailable):
            self.secret.get(reference.replace("/1", "/latest"))

    def test_response_loss_reconciles_without_add_and_unknown_never_retries(self) -> None:
        self.transport.fail_after = True
        with self.assertRaises(SecretUnavailable) as error:
            self.put()
        self.assertNotIn("SYNTHETIC_REMOTE_SECRET", str(error.exception))
        self.transport.fail_after = False
        self.assertEqual(self.secret.get(self.put()), b"SYNTHETIC_REMOTE_SECRET")
        self.assertEqual(self.transport.adds, 1)
        self.slot = ResourceId(str(uuid4()))
        self.transport.fail_before = True
        with self.assertRaises(SecretUnavailable):
            self.put()
        self.transport.fail_before = False
        with self.assertRaises(SecretUnavailable):
            self.put()
        self.assertEqual(self.transport.adds, 2)
        with self.assertRaises(SecretUnavailable):
            self.secret.invalidate(self.intent("leave_tenant"), key="synthetic-purge")

    def test_all_lifecycle_scopes_destroy_and_fence_future_slot_writes(self) -> None:
        for kind in ("leave_tenant", "delete_account", "delete_tenant"):
            with self.subTest(kind=kind):
                self.setUp()
                reference = self.put()
                self.secret.invalidate(self.intent(kind), key="synthetic-purge")
                self.assertNotIn(reference, self.transport.values)
                with self.assertRaises(SecretUnavailable):
                    self.secret.get(reference)
                self.secret = GoogleCalendarSecrets(
                    self.registry, self.transport, project="synthetic-project"
                )
                with self.assertRaises(SecretUnavailable):
                    self.put(ResourceId(str(uuid4())))

    def test_late_add_after_revocation_is_destroyed_and_purge_remains_pending_until_known(
        self,
    ) -> None:
        entered, release = Event(), Event()

        def wait() -> None:
            entered.set()
            if not release.wait(10):
                raise AssertionError("Secret fixture timed out")

        self.transport.before = wait
        intent = self.intent("leave_tenant")
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.put)
            self.assertTrue(entered.wait(10))
            try:
                with self.assertRaises(SecretUnavailable):
                    self.secret.invalidate(intent, key="synthetic-purge")
            finally:
                release.set()
            with self.assertRaises(SecretUnavailable):
                future.result(10)
        self.secret.invalidate(intent, key="synthetic-purge")
        self.assertEqual(self.transport.values, {})
        with self.assertRaises(SecretUnavailable):
            self.put()

    def test_local_adapter_encrypts_and_persistently_revokes_missing_slots(self) -> None:
        args = {
            "tenant": TENANT,
            "user": USER,
            "binding": "a" * 64,
            "content": b"SYNTHETIC_LOCAL_SECRET",
            "expires_at": None,
        }
        reference = self.registry.put(self.slot, **args)  # type: ignore[arg-type]
        self.assertEqual(self.registry.get(reference), args["content"])
        self.assertNotIn(args["content"], self.registry.path.read_bytes())
        self.registry.invalidate(self.intent("leave_tenant"), key="synthetic-local-purge")
        self.registry = LocalCalendarSecrets(self.directory, b"s" * 32, now=self.clock.now)
        with self.assertRaises(SecretUnavailable):
            self.registry.put(ResourceId(str(uuid4())), **args)  # type: ignore[arg-type]
        with self.assertRaises(SecretUnavailable):
            self.registry.get(reference)

    def test_rejoin_generation_can_connect_but_old_scope_replay_cannot_erase_it(self) -> None:
        reference = self.put()
        intent = self.intent("leave_tenant")
        self.secret.invalidate(intent, key="member-purge")
        newer = self.secret.put(
            ResourceId(str(uuid4())),
            tenant=TENANT,
            user=USER,
            binding="new-generation",
            content=b"new secret",
            expires_at=None,
            generation=3,
        )
        self.secret.invalidate(intent, key="member-purge")
        self.assertEqual(self.secret.get(newer), b"new secret")
        with self.assertRaises(SecretUnavailable):
            self.secret.get(reference)

    def test_lost_destroy_receipt_reconciles_destroyed_version(self) -> None:
        from unittest.mock import patch

        reference = self.put()
        destroy = self.transport.destroy

        def lost(reference: str) -> None:
            destroy(reference)
            raise RuntimeError("lost destroy response")

        with patch.object(self.transport, "destroy", side_effect=lost):
            with self.assertRaises(SecretUnavailable):
                self.secret.revoke(self.slot)
        self.secret.revoke(self.slot)
        self.assertNotIn(reference, self.transport.values)

    def test_uncertain_other_scope_cannot_block_tenant_account_or_member_erasure(self) -> None:
        tenant_b = ResourceId("00000000-0000-0000-0000-000000000002")
        other = FirebaseUid("other-secret-owner")
        for kind, tenant, user in (
            ("delete_tenant", tenant_b, USER),
            ("delete_account", TENANT, other),
            ("leave_tenant", TENANT, other),
        ):
            with self.subTest(kind=kind):
                self.setUp()
                self.transport.fail_before = True
                with self.assertRaises(SecretUnavailable):
                    self.put()
                self.transport.fail_before = False
                old_intent = self.intent("leave_tenant")
                with self.assertRaises(SecretUnavailable):
                    self.secret.invalidate(old_intent, key="uncertain-old-scope")
                current = self.secret.put(
                    ResourceId(str(uuid4())),
                    tenant=tenant,
                    user=user,
                    binding="current-scope",
                    content=b"CURRENT_SCOPE_TOKEN",
                    expires_at=None,
                )
                target = self.intent(kind, tenant=tenant, user=user)
                for _ in range(2):
                    self.secret = GoogleCalendarSecrets(
                        self.registry, self.transport, project="synthetic-project"
                    )
                    self.transport.accesses.clear()
                    self.transport.destroys.clear()
                    self.assertEqual(
                        self.secret.invalidate(target, key="current-scope-erasure"),
                        "google-calendar-secrets-revoked-v1",
                    )
                    self.assertEqual(self.transport.destroys, [current])
                    self.assertEqual(self.transport.accesses, [])
                    self.assertNotIn(current, self.transport.values)
                self.transport.accesses.clear()
                self.transport.destroys.clear()
                with self.assertRaises(SecretUnavailable):
                    self.secret.invalidate(old_intent, key="uncertain-old-scope")
                self.assertEqual(self.transport.destroys, [self.secret.reference(self.slot)])
                self.assertEqual(self.transport.accesses, [])
                with self.registry.db() as db:
                    self.assertEqual(
                        db.execute(
                            "SELECT state,uncertain FROM remote_versions WHERE slot=?", (self.slot,)
                        ).fetchone(),
                        ("revoked", 1),
                    )

    def test_old_uncertain_generation_recovery_never_touches_rejoined_generation(self) -> None:
        self.transport.fail_before = True
        with self.assertRaises(SecretUnavailable):
            self.put()
        self.transport.fail_before = False
        intent = self.intent("leave_tenant")
        with self.assertRaises(SecretUnavailable):
            self.secret.invalidate(intent, key="old-generation-erasure")
        newer = self.secret.put(
            ResourceId(str(uuid4())),
            tenant=TENANT,
            user=USER,
            binding="rejoined",
            content=b"REJOINED_TOKEN",
            expires_at=None,
            generation=3,
        )
        self.transport.destroys.clear()
        self.transport.accesses.clear()
        with self.assertRaises(SecretUnavailable):
            self.secret.invalidate(intent, key="old-generation-erasure")
        old_ref = self.secret.reference(self.slot)
        self.assertEqual(self.transport.destroys, [old_ref])
        self.assertEqual(self.transport.accesses, [])
        self.assertEqual(self.secret.get(newer), b"REJOINED_TOKEN")
        # A delayed remote add becomes visible after the first cleanup failed.
        self.transport.values[old_ref] = b"LATE_OLD_GENERATION_TOKEN"
        self.secret = GoogleCalendarSecrets(
            self.registry, self.transport, project="synthetic-project"
        )
        self.transport.destroys.clear()
        self.transport.accesses.clear()
        self.secret.invalidate(intent, key="old-generation-erasure")
        self.assertEqual(self.transport.destroys, [old_ref])
        self.assertEqual(self.transport.accesses, [])
        self.assertNotIn(old_ref, self.transport.values)
        self.assertEqual(self.secret.get(newer), b"REJOINED_TOKEN")
