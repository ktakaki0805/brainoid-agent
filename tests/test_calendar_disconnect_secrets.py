"""Exact remote secret cleanup remains pending on failures and cannot widen scope."""

import unittest
from unittest.mock import patch
from uuid import uuid4

from brainoid_api.adapters.google_calendar_secrets import GoogleCalendarSecrets
from brainoid_contracts import ResourceId
from brainoid_contracts.deletion import subject_id
from brainoid_memory.calendar_oauth import SecretUnavailable
from brainoid_memory.deletion_ledger import DeletionIntent, LifecycleScope
from test_calendar_secrets import TENANT, USER, CalendarSecretsTests


class CalendarDisconnectSecretTests(CalendarSecretsTests):
    def test_disconnect_remote_failure_exact_slots_restart_and_new_grant(self) -> None:
        reference = self.put()
        other_slot = ResourceId(str(uuid4()))
        other = self.put(other_slot)
        absent = ResourceId(str(uuid4()))
        intent = DeletionIntent(
            kind="disconnect_calendar",
            operation_id=ResourceId(str(uuid4())),
            tenant_id=TENANT,
            request_hash="c" * 64,
            source_policy="delete_source",
            created_at=self.clock.now(),
            targets=(),
            scopes=(
                LifecycleScope(
                    kind="integration",
                    resource_id=ResourceId(str(uuid4())),
                    tenant_id=TENANT,
                    version=1,
                    export_epoch=1,
                    authorization_id=ResourceId(str(uuid4())),
                    owner_subject_id=subject_id(USER),
                    secret_slots=(self.slot, absent),
                ),
            ),
        )
        with patch.object(
            self.transport, "destroy", side_effect=RuntimeError("SYNTHETIC_PROVIDER_FAILURE")
        ):
            with self.assertRaises(SecretUnavailable):
                self.secret.invalidate(intent, key="disconnect-secret-test")
        self.assertIn(reference, self.transport.values)
        self.secret = GoogleCalendarSecrets(
            self.registry, self.transport, project="synthetic-project"
        )
        for slot in (self.slot, absent):
            with self.assertRaises(SecretUnavailable):
                self.put(slot)
        self.secret.invalidate(intent, key="disconnect-secret-test")
        self.assertNotIn(reference, self.transport.values)
        self.assertEqual(self.secret.get(other), b"SYNTHETIC_REMOTE_SECRET")


def load_tests(loader, tests, pattern):  # type: ignore[no-untyped-def]
    return unittest.TestSuite(
        CalendarDisconnectSecretTests(name)
        for name in loader.getTestCaseNames(CalendarDisconnectSecretTests)
        if name.startswith("test_disconnect_")
    )
