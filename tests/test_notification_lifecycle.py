"""Notification source, delivery child and job receipts follow accepted lifecycle purge."""

from datetime import timedelta
from uuid import uuid4

from brainoid_api.common.pagination import Cursors
from brainoid_api.repositories.memories import Memories
from brainoid_api.repositories.push_installations import PushInstallations
from brainoid_api.repositories.tenants import Tenants
from brainoid_api.services.deletion_requests import DeleteTenant
from brainoid_contracts import FirebaseUid
from brainoid_contracts.deletion import subject_id
from brainoid_contracts.transactions import Transaction
from brainoid_memory.notifications import KIND, notification_id, notification_path
from brainoid_memory.push_installations import PushCreate, claim_path, installation_path
from brainoid_memory.reminders import (
    ReminderCreate,
    ReminderPatch,
    Reminders,
    ReminderSnooze,
    ScheduleReference,
)
from brainoid_memory.repository import FactTransaction
from brainoid_worker.entry import Worker
from brainoid_worker.local import LOCAL_AUTHORIZATION, LocalCaller
from brainoid_worker.notifications import DispatchNotification, DueReminders
from brainoid_worker.push_notifications import PushNotifications
from fakes.notification_scenarios import FakeNotifications
from fakes.purge import FakePurgePages
from fakes.push_scenarios import FakePush
from test_deletion_requests import KEY, OWNER, USER, DeletionFixture


class NotificationLifecycleTests(DeletionFixture):
    def test_admin_replan_has_new_intent_while_rejoin_and_title_edit_do_not_revive_old(
        self,
    ) -> None:
        cursors = Cursors(b"k" * 32, now=lambda: self.now)
        tenants = Tenants(self.store, cursors, ledger=self.authz, now=lambda: self.now)
        memories = Memories(
            self.store,
            tenants,
            cursors,
            ledger=self.authz,
            now=lambda: self.now,
            allow_untracked_bootstrap=True,
        )
        service = Reminders(self.store, now=lambda: self.now)

        def member(tx: Transaction) -> FactTransaction:
            return memories._open(tx, self.identity(USER), self.tenant, None)

        def admin(tx: Transaction) -> FactTransaction:
            return memories._open(tx, self.identity(OWNER), self.tenant, None)

        body = ReminderCreate.model_validate(
            {
                "title": "shared schedule",
                "due_at": self.now + timedelta(seconds=1),
                "timezone": "UTC",
                "delivery_target": {"kind": "user", "id": OWNER},
                "visibility": "shared",
                "display_policy": "never",
            }
        )
        record = service.mutate(member, body, "shared-admin-replan-1")
        untouched = service.mutate(member, body, "shared-old-rejoin-1")
        member_path = next(
            p
            for p, d in self.store.data.items()
            if p.startswith(f"tenants/{self.tenant}/memberships/") and d.get("user_id") == USER
        )
        self.store.data[member_path]["status"] = "revoked"
        self.now += timedelta(seconds=1)
        edited = service.mutate(
            admin,
            ReminderPatch(title="admin title only"),
            "admin-title-edit-key",
            identity=record.id,
            expected=1,
        )
        scanner = DueReminders(self.normal, FakePurgePages(self.store))
        self.assertEqual(scanner.scan(self.tenant).created, 0)
        replanned = service.mutate(
            admin,
            ReminderSnooze(
                due_at=self.now + timedelta(seconds=1),
                timezone=body.timezone,
                expected_version=edited.version,
            ).changes(),
            "admin-reschedule-key",
            identity=record.id,
            expected=edited.version,
        )
        self.now += timedelta(seconds=1)
        self.assertEqual(scanner.scan(self.tenant).created, 1)
        self.store.data[member_path].update(status="active", authorization_version=2)
        self.assertEqual(scanner.scan(self.tenant).created, 0)
        original_id = notification_id(
            ScheduleReference(tenant_id=self.tenant, reminder_id=untouched.id, schedule_version=1)
        )
        self.assertNotIn(notification_path(self.tenant, original_id), self.store.data)
        identity = notification_id(
            ScheduleReference(tenant_id=self.tenant, reminder_id=replanned.id, schedule_version=2)
        )
        provider = FakeNotifications()
        worker = Worker(
            self.normal, LocalCaller(), {KIND: DispatchNotification(self.normal, provider)}
        )
        from brainoid_jobs.models import job_id

        job = self.normal.get(self.tenant, job_id(self.tenant, KIND, identity))
        assert job is not None
        self.assertEqual(job.grant.principal.user_id, OWNER)
        self.assertEqual(job.grant.resource.created_by, USER)
        delivery = self.normal.prepare_dispatch(self.tenant, job.id)
        assert delivery is not None
        worker.handle(LOCAL_AUTHORIZATION, delivery.model_dump(mode="json"))
        self.assertEqual(len(provider.calls), 1)

    def test_account_purge_retains_token_transferred_to_another_current_account(self) -> None:
        cursors = Cursors(b"k" * 32, now=lambda: self.now)
        tenants = Tenants(self.store, cursors, ledger=self.authz, now=lambda: self.now)
        registrations = PushInstallations(tenants)
        body = PushCreate(fcm_token="SYNTHETIC_TRANSFERRED_TOKEN", platform="ios", app_version="1")
        old = registrations.mutate(self.identity(USER), body, "old-push-registration")
        new = registrations.mutate(self.identity(OWNER), body, "new-push-registration")
        assert old is not None and new is not None
        operation = self.accept(account=True)
        self.drain(operation, subject_id(USER))
        self.assertNotIn(installation_path(old.id), self.store.data)
        self.assertIn(installation_path(new.id), self.store.data)
        self.assertEqual(self.store.data[claim_path(body.fcm_token)]["user_id"], OWNER)

    def test_real_producers_are_purged_by_member_account_and_tenant_intents(self) -> None:
        for kind in ("member", "account", "tenant"):
            with self.subTest(kind=kind):
                self.setUp()
                cursors = Cursors(b"k" * 32, now=lambda: self.now)
                tenants = Tenants(self.store, cursors, ledger=self.authz, now=lambda: self.now)
                memories = Memories(
                    self.store,
                    tenants,
                    cursors,
                    ledger=self.authz,
                    now=lambda: self.now,
                    allow_untracked_bootstrap=True,
                )
                service = Reminders(self.store, now=lambda: self.now)
                registrations = []
                for user in (USER, OWNER):
                    installation = PushInstallations(tenants).mutate(
                        self.identity(user),
                        PushCreate(
                            fcm_token="synthetic-purge-" + user, platform="android", app_version="1"
                        ),
                        "push-purge-" + user,
                    )
                    assert installation is not None
                    registrations.append(installation)
                ids = []
                for actor, visibility in ((USER, "private"), (USER, "shared"), (OWNER, "private")):

                    def opened(
                        tx: Transaction, actor: FirebaseUid = actor, memories: Memories = memories
                    ) -> FactTransaction:
                        return memories._open(tx, self.identity(actor), self.tenant, None)

                    record = service.mutate(
                        opened,
                        ReminderCreate.model_validate(
                            {
                                "title": "NOTIFICATION_LIFECYCLE",
                                "due_at": self.now + timedelta(seconds=1),
                                "timezone": "UTC",
                                "delivery_target": {"kind": "user", "id": actor},
                                "visibility": visibility,
                                "display_policy": "never",
                            }
                        ),
                        "notification-lifecycle-" + str(uuid4()),
                    )
                    ids.append(
                        notification_id(
                            ScheduleReference(
                                tenant_id=self.tenant, reminder_id=record.id, schedule_version=1
                            )
                        )
                    )
                self.now += timedelta(seconds=1)
                scanner = DueReminders(self.normal, FakePurgePages(self.store))
                self.assertEqual(scanner.scan(self.tenant).created, 3)
                provider = FakePush()
                push = PushNotifications(self.normal, self.tenant, provider)
                worker = Worker(
                    self.normal, LocalCaller(), {KIND: DispatchNotification(self.normal, push)}
                )
                deliveries = []
                from brainoid_jobs.models import job_id

                for identity in ids:
                    delivery = self.normal.prepare_dispatch(
                        self.tenant, job_id(self.tenant, KIND, identity)
                    )
                    assert delivery is not None
                    deliveries.append(delivery)
                    worker.handle(LOCAL_AUTHORIZATION, delivery.model_dump(mode="json"))
                if kind == "tenant":
                    operation = self.repo.create(
                        self.identity(OWNER),
                        DeleteTenant(
                            kind="delete_tenant",
                            confirmation_name=self.store.data[f"tenants/{self.tenant}"]["name"],
                        ),
                        KEY,
                        self.tenant,
                    )
                else:
                    operation = self.accept(account=kind == "account")
                self.drain(operation, subject_id(USER) if kind == "account" else self.tenant)
                for identity in ids[:2]:
                    path = notification_path(self.tenant, identity)
                    self.assertFalse(
                        any(p == path or p.startswith(path + "/") for p in self.store.data)
                    )
                    self.assertIsNone(
                        self.normal.get(self.tenant, job_id(self.tenant, KIND, identity))
                    )
                self.assertEqual(
                    installation_path(registrations[0].id) in self.store.data, kind != "account"
                )
                self.assertEqual(
                    claim_path("synthetic-purge-" + USER) in self.store.data, kind != "account"
                )
                self.assertIn(installation_path(registrations[1].id), self.store.data)
                self.assertIn(claim_path("synthetic-purge-" + OWNER), self.store.data)
                other = notification_path(self.tenant, ids[2])
                self.assertEqual(other in self.store.data, kind != "tenant")
                count = len(provider.calls)
                for delivery in deliveries:
                    worker.handle(LOCAL_AUTHORIZATION, delivery.model_dump(mode="json"))
                self.assertEqual(len(provider.calls), count)
