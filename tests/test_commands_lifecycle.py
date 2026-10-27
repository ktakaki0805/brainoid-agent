"""Command inventory joins actual lifecycle erasure and current export manifests."""

import copy
from datetime import timedelta
from uuid import uuid4

from brainoid_api.common.pagination import Cursors
from brainoid_api.repositories.commands import CommandRequests
from brainoid_api.repositories.memories import Memories
from brainoid_api.repositories.tenants import Tenants
from brainoid_api.services.deletion_requests import DeleteTenant
from brainoid_contracts import ResourceId
from brainoid_contracts.deletion import subject_id
from brainoid_memory.commands import command_path
from fakes.home import home_fixture
from fakes.memory_repository import TENANT
from test_deletion_requests import KEY, OWNER, USER, DeletionFixture
from test_exports import ExportFixture


class CommandsLifecycleTests(DeletionFixture):
    def test_command_and_inventory_erasure_preserves_other_private_and_shared_contributions(
        self,
    ) -> None:
        for kind in ("member", "account", "tenant"):
            with self.subTest(kind=kind):
                self.setUp()
                tenants = Tenants(
                    self.store,
                    Cursors(b"h" * 32, now=lambda: self.now),
                    ledger=self.authz,
                    now=lambda: self.now,
                )
                memories = Memories(
                    self.store,
                    tenants,
                    Cursors(b"h" * 32, now=lambda: self.now),
                    ledger=self.authz,
                    now=lambda: self.now,
                    allow_untracked_bootstrap=True,
                )
                commands = CommandRequests(memories)
                preserved = {}
                deleted = {}
                for actor, private in ((USER, True), (OWNER, True), (USER, False)):
                    body, documents = home_fixture(self.tenant, actor, self.now, private=private)
                    self.store.data.update(documents)
                    record = commands.create(
                        self.identity(actor), self.tenant, body, "lifecycle-home-" + str(uuid4())
                    )
                    paths = [command_path(self.tenant, record.id)] + [
                        p for p in documents if "/devices/" not in p
                    ]
                    if actor == USER and private:
                        deleted = {p: copy.deepcopy(self.store.data[p]) for p in paths}
                    elif kind != "tenant":
                        preserved.update({p: copy.deepcopy(self.store.data[p]) for p in paths})
                operation = (
                    self.repo.create(
                        self.identity(OWNER),
                        DeleteTenant(
                            kind="delete_tenant",
                            confirmation_name=self.store.data[f"tenants/{self.tenant}"]["name"],
                        ),
                        KEY,
                        self.tenant,
                    )
                    if kind == "tenant"
                    else self.accept(account=kind == "account")
                )
                self.drain(operation, subject_id(USER) if kind == "account" else self.tenant)
                for path in deleted:
                    if "/entity_external_ids/" in path:
                        self.assertEqual(
                            self.store.data[path],
                            {
                                "entity_id": deleted[path]["entity_id"],
                                "deletion_epoch": 0,
                                "status": "deleted",
                            },
                        )
                    else:
                        self.assertFalse(path in self.store.data, (kind, path))
                for path, document in preserved.items():
                    self.assertEqual(self.store.data[path], document)


class CommandsExportTests(ExportFixture):
    def test_command_export_omits_internal_authority_and_current_status_invalidates_download(
        self,
    ) -> None:
        body, documents = home_fixture(TENANT, self.identity().user_id, self.now)
        self.store.data.update(documents)
        commands = CommandRequests(self.memories)
        record = commands.create(self.identity(), TENANT, body, "home-export-create")
        operation = self.ready()
        archive = self.archive(operation)
        exported = next(r["data"] for r in archive["records"] if r["kind"] == "commands")
        self.assertEqual(exported["status"], "queued")
        self.assertEqual(exported["id"], record.id)
        for key in ("actor_stamp", "account_stamp", "binding", "actor", "actor_credential"):
            self.assertNotIn(key, exported)
        commands.cancel(self.identity(), TENANT, record.id, "home-export-cancel", None)
        self.assertNotEqual(self.download(operation).status_code, 200)

    def test_device_origin_export_rechecks_family_and_external_epoch_at_download(self) -> None:
        # Synthetic inventory/family plus real signed device authentication and ZIP worker.
        body, documents = home_fixture(TENANT, self.identity().user_id, self.now)
        self.store.data.update(documents)
        family = ResourceId(str(uuid4()))
        family_path = "device_credential_families/" + family
        self.store.data[family_path] = {
            "tenant_id": TENANT,
            "device_id": body.device_id,
            "epoch": self.tokens.epoch,
            "authorization_version": 1,
            "connection_generation": 1,
            "status": "active",
            "expires_at": self.now + timedelta(days=30),
        }
        device = self.credentials.authenticate(
            self.tokens.issue(
                tenant_id=TENANT,
                device_id=body.device_id,
                family_id=family,
                authorization_version=1,
                connection_generation=1,
            )
        )
        record = CommandRequests(self.memories).create(device, TENANT, body, "home-device-export")
        self.exports.credential_epoch = self.tokens.epoch
        operation = self.ready()
        exported = next(
            r["data"] for r in self.archive(operation)["records"] if r["kind"] == "commands"
        )
        self.assertEqual(exported["id"], record.id)
        self.assertNotIn("actor_credential", exported)
        self.store.data[family_path]["status"] = "revoked"
        self.assertNotEqual(self.download(operation).status_code, 200)
        replacement = self.create_export("home-revoked-export-key")
        self.generate(replacement)
        self.assertFalse(any(r["kind"] == "commands" for r in self.archive(replacement)["records"]))

        # Restoring a former DB state cannot restore the externally changed epoch.
        self.store.data[family_path]["status"] = "active"
        restored = self.create_export("home-restored-export-key")
        self.generate(restored)
        self.assertTrue(any(r["kind"] == "commands" for r in self.archive(restored)["records"]))
        self.exports.credential_epoch = ResourceId(str(uuid4()))
        self.assertNotEqual(self.download(restored).status_code, 200)
