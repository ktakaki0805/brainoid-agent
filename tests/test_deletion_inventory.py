"""R1 regressions through supported settings, entity and credential producers."""

import copy
from uuid import uuid4

from brainoid_api.app import create_app
from brainoid_api.auth.device_tokens import DeviceTokens
from brainoid_api.auth.runtime import AuthServices, get_auth_services
from brainoid_api.common.pagination import Cursors
from brainoid_api.errors import APIError, ErrorCode
from brainoid_api.repositories.device_credentials import (
    DeviceCredentialRepository,
    DeviceRefresh,
    PairingConfirm,
    PairingCreate,
    PairingExchange,
)
from brainoid_api.repositories.entities import Entities
from brainoid_api.repositories.tenants import TenantPatch, Tenants
from brainoid_api.services.deletion_requests import DeleteAccount, DeleteTenant, LeaveTenant
from brainoid_api.settings import load_settings
from brainoid_contracts import DisplayPolicy, FirebaseUid, ResourceId, TenantContext, Visibility
from brainoid_contracts.deletion import subject_id
from brainoid_contracts.transactions import Transaction
from brainoid_jobs.models import job_id
from brainoid_memory import EntityInput
from brainoid_memory.entity_resolver import external_key
from brainoid_memory.forget import operation_path
from brainoid_memory.lifecycle import KIND
from brainoid_memory.models import ExternalRef
from brainoid_policy.control_ledger import LedgerError
from brainoid_worker.entry import Execution
from brainoid_worker.lifecycle_handlers import DEVICE_GLOBALS
from starlette.testclient import TestClient
from test_api_foundation import LOCAL_ENV
from test_deletion_requests import KEY, OWNER, USER, DeletionFixture
from test_device_pairing import EPOCH, KEYRING


class Inventory:
    def __init__(self, private: bool) -> None:
        self.private = private

    def resolve(
        self, tx: Transaction, context: TenantContext, reference: ExternalRef
    ) -> EntityInput:
        return EntityInput(
            kind="appliance",
            label="Synthetic appliance",
            visibility=Visibility.PRIVATE if self.private else Visibility.SHARED,
            display_policy=DisplayPolicy.NEVER,
            external_refs=(reference,),
        )


class DeletionInventoryTests(DeletionFixture):
    def tenants(self) -> Tenants:
        return Tenants(
            self.store,
            Cursors(b"k" * 32, now=lambda: self.now),
            ledger=self.authz,
            now=lambda: self.now,
        )

    def test_settings_reservation_all_kinds_and_failure_replay(self) -> None:
        for kind in ("leave", "account", "tenant"):
            for failure in ("none", "ledger_before", "ledger_after", "db_after_ledger"):
                with self.subTest(kind=kind, failure=failure):
                    self.setUp()
                    root = f"tenants/{self.tenant}"
                    body = (
                        DeleteAccount(kind="delete_account", confirm=True)
                        if kind == "account"
                        else DeleteTenant(
                            kind="delete_tenant", confirmation_name=self.store.data[root]["name"]
                        )
                        if kind == "tenant"
                        else LeaveTenant(kind="leave_tenant")
                    )
                    actor = OWNER if kind == "tenant" else USER
                    endpoint = None if kind == "account" else self.tenant

                    def patch() -> None:
                        with self.assertRaises(APIError) as error:
                            self.tenants().update(
                                self.identity(OWNER),
                                self.tenant,
                                TenantPatch(name="Legitimate renamed tenant"),
                                KEY + "settings",
                                '"1"',
                            )
                        self.assertEqual(error.exception.code, ErrorCode.CONFLICT)

                    def ledger_hook(name: str, failure: str = failure) -> None:
                        patch()
                        if failure.startswith("ledger_"):
                            raise LedgerError()
                        if failure == "db_after_ledger":
                            self.store.fail_commit = True

                    if failure == "ledger_before":
                        self.objects.before = ledger_hook
                    else:
                        self.objects.after = ledger_hook
                    if failure == "none":
                        operation = self.repo.create(self.identity(actor), body, KEY, endpoint)
                    else:
                        with self.assertRaises((APIError, RuntimeError)):
                            self.repo.create(self.identity(actor), body, KEY, endpoint)
                        self.store.fail_commit = False
                        patch()  # A later legitimate PATCH must also respect the durable reservation.
                        self.objects.before = self.objects.after = lambda name: None
                        operation = self.repo.create(self.identity(actor), body, KEY, endpoint)
                    self.assertEqual(
                        self.repo.create(self.identity(actor), body, KEY, endpoint), operation
                    )
                    self.assertEqual(len(self.objects.data), 1)
                    self.assertEqual(self.store.data[root]["version"], 1)
                    self.assertIsNone(self.store.data[root].get("membership_mutation"))

    def test_private_external_indexes_follow_canonical_entity_and_replay(self) -> None:
        for account in (False, True):
            with self.subTest(account=account):
                self.setUp()
                user = FirebaseUid("demo-a-admin")
                preserved = {}
                own = None
                for tenant, actor, private in (
                    (self.tenant, user, True),
                    (self.tenant, OWNER, True),
                    (self.tenant, user, False),
                    (self.other, FirebaseUid("demo-b-owner"), True),
                ):
                    ref = ExternalRef(
                        provider="home_assistant",
                        integration_id=ResourceId(str(uuid4())),
                        external_id="private-bedroom-device-only",
                    )
                    integration = f"tenants/{tenant}/integrations/{ref.integration_id}"
                    self.store.data[integration] = {
                        "id": ref.integration_id,
                        "tenant_id": tenant,
                        "provider": ref.provider,
                        "owner_user_id": actor,
                        "visibility": "private" if private else "shared",
                        "status": "active",
                    }
                    repository = Entities(
                        self.store,
                        self.tenants(),
                        Cursors(b"k" * 32, now=lambda: self.now),
                        ledger=self.authz,
                        inventory=Inventory(private),
                        now=lambda: self.now,
                        allow_untracked_bootstrap=True,
                    )
                    entity = repository.register(
                        self.identity(actor), tenant, ref, KEY + "register" + ref.integration_id
                    )
                    index = f"tenants/{tenant}/entity_external_ids/{external_key(tenant, ref)}"
                    if tenant == self.tenant and actor == user and private:
                        own = (f"tenants/{tenant}/entities/{entity.id}", index, integration, ref)
                    else:
                        preserved[index] = copy.deepcopy(self.store.data[index])
                assert own is not None
                entity_path, index, integration, ref = own
                operation = self.accept(user=user, account=account)
                anchor = subject_id(user) if account else self.tenant
                jid = job_id(anchor, KIND, operation.operation_id)
                self.jobs.activate(anchor, jid)
                delivery = self.jobs.prepare_dispatch(anchor, jid, queue_name=KIND)
                assert delivery is not None
                lease = self.jobs.acquire(delivery, known_handler=lambda kind: kind == KIND)
                assert lease is not None
                execution = Execution(self.jobs, lease)
                # Default official ordering erases entities before entity_external_ids.
                # Lose the committed response at that boundary, then resume the same job.
                interrupted = False

                def lost_response(entity_path: str = entity_path, index: str = index) -> None:
                    nonlocal interrupted
                    if (
                        not interrupted
                        and entity_path not in self.store.data
                        and "reference" in self.store.data[index]
                    ):
                        interrupted = True
                        raise RuntimeError("synthetic entity-erasure response loss")

                self.store.after_commit = lost_response
                with self.assertRaisesRegex(RuntimeError, "entity-erasure"):
                    for _ in range(5000):
                        self.handler.step(execution)
                self.store.after_commit = lambda: None
                for _ in range(5000):
                    if self.handler.step(execution):
                        self.jobs.complete(
                            lease, result={"purged": True}, commit=self.handler.commit
                        )
                        break
                self.assertTrue(interrupted)
                self.assertEqual(
                    self.store.data[operation_path(anchor, operation.operation_id)]["operation"][
                        "status"
                    ],
                    "succeeded",
                )
                self.assertNotIn(entity_path, self.store.data)
                self.assertNotIn(integration, self.store.data)
                self.assertEqual(
                    set(self.store.data[index]), {"entity_id", "deletion_epoch", "status"}
                )
                self.assertEqual(self.store.data[index]["status"], "deleted")
                for path, value in preserved.items():
                    self.assertEqual(self.store.data[path], value)
                # A different authorized owner cannot use the reduced index to recreate it.
                repository = Entities(
                    self.store,
                    self.tenants(),
                    Cursors(b"k" * 32, now=lambda: self.now),
                    ledger=self.authz,
                    inventory=Inventory(True),
                    now=lambda: self.now,
                    allow_untracked_bootstrap=True,
                )
                with self.assertRaises(APIError):
                    repository.register(self.identity(OWNER), self.tenant, ref, KEY + "reregister")

    def test_reduced_external_index_denies_registration_through_live_shared_integration(
        self,
    ) -> None:
        user = FirebaseUid("demo-a-admin")
        ref = ExternalRef(
            provider="home_assistant",
            integration_id=ResourceId(str(uuid4())),
            external_id="synthetic-private-object",
        )
        path = f"tenants/{self.tenant}/integrations/{ref.integration_id}"
        self.store.data[path] = {
            "id": ref.integration_id,
            "tenant_id": self.tenant,
            "provider": ref.provider,
            "owner_user_id": None,
            "visibility": "shared",
            "status": "active",
        }
        repository = Entities(
            self.store,
            self.tenants(),
            Cursors(b"k" * 32, now=lambda: self.now),
            ledger=self.authz,
            inventory=Inventory(True),
            now=lambda: self.now,
            allow_untracked_bootstrap=True,
        )
        entity = repository.register(self.identity(user), self.tenant, ref, KEY + "register")
        operation = self.accept(user=user)
        self.drain(operation, self.tenant)
        self.assertIn(path, self.store.data)
        self.assertNotIn(f"tenants/{self.tenant}/entities/{entity.id}", self.store.data)
        with self.assertRaises(APIError) as caught:
            repository.register(self.identity(OWNER), self.tenant, ref, KEY + "reregister")
        self.assertEqual(caught.exception.code, ErrorCode.RESOURCE_NOT_FOUND)

    def test_tenant_credentials_are_redacted_before_completion_and_replays_deny(self) -> None:
        tokens = DeviceTokens(
            KEYRING,
            project="demo-brainoid",
            credential_epoch=EPOCH,
            test_only=True,
            now=lambda: self.now,
        )
        credentials = DeviceCredentialRepository(
            self.store, self.tenants(), self.authz, tokens, now=lambda: self.now
        )
        pairs = []
        for tenant, owner, nonce in (
            (self.tenant, OWNER, "x"),
            (self.other, FirebaseUid("demo-b-owner"), "y"),
        ):
            body = PairingCreate(device_name="Synthetic Pi", installation_nonce=nonce * 32)
            pair = credentials.create(body, KEY + "pair", "127.0.0.1")
            confirm = PairingConfirm(
                user_code=pair.user_code, device_name="Synthetic Pi", allowed_entity_ids=[]
            )
            credentials.confirm(self.identity(owner), tenant, confirm, KEY + "confirm", "127.0.0.1")
            exchange = PairingExchange(poll_secret=pair.poll_secret)
            issued = credentials.exchange(pair.pairing_id, exchange, KEY + "exchange", "127.0.0.1")
            refresh = DeviceRefresh(
                device_id=issued.device_id,
                refresh_token=issued.refresh_token,
                refresh_request_id=ResourceId(str(uuid4())),
            )
            refreshed = credentials.refresh(refresh, KEY + "refresh", "127.0.0.1")
            pairs.append((body, pair, confirm, exchange, issued, refresh, refreshed))
        body, pair, confirm, exchange, issued, refresh, refreshed = pairs[0]
        pending = credentials.create(body, KEY + "pending-repair", "127.0.0.1")
        other_family = pairs[1][4].refresh_token.split(".")[0]
        other_pair = pairs[1][1].pairing_id
        preserved = {
            p: copy.deepcopy(d)
            for p, d in self.store.data.items()
            if p.split("/")[0] in DEVICE_GLOBALS
            and (
                d.get("tenant_id") == self.other
                or d.get("family_id") == other_family
                or d.get("pairing_id") == other_pair
                or p.startswith(f"device_credential_families/{other_family}")
            )
        }
        family = issued.refresh_token.split(".")[0]
        extra = f"device_pairings/{pair.pairing_id}/unknown/deep/children/payload"
        self.store.data[extra] = {"encrypted_receipt": "synthetic-private-payload"}
        operation = self.repo.create(
            self.identity(OWNER),
            DeleteTenant(
                kind="delete_tenant",
                confirmation_name=self.store.data[f"tenants/{self.tenant}"]["name"],
            ),
            KEY,
            self.tenant,
        )
        jid = job_id(self.tenant, KIND, operation.operation_id)
        self.jobs.activate(self.tenant, jid)
        delivery = self.jobs.prepare_dispatch(self.tenant, jid, queue_name=KIND)
        assert delivery is not None
        lease = self.jobs.acquire(delivery, known_handler=lambda kind: kind == KIND)
        assert lease is not None
        execution = Execution(self.jobs, lease)

        def lost_response() -> None:
            if self.store.data[f"device_credential_families/{family}"]["status"] == "revoked":
                self.store.after_commit = lambda: None
                raise RuntimeError("synthetic credential-redaction response loss")

        self.store.after_commit = lost_response
        with self.assertRaisesRegex(RuntimeError, "credential-redaction"):
            for _ in range(5000):
                self.handler.step(execution)
        self.assertEqual(
            self.store.data[operation_path(self.tenant, operation.operation_id)]["operation"][
                "status"
            ],
            "running",
        )
        self.jobs.fail(lease, retry=True)
        self.now += self.jobs.retry_delay
        delivery = self.jobs.prepare_dispatch(self.tenant, jid, queue_name=KIND)
        assert delivery is not None
        lease = self.jobs.acquire(delivery, known_handler=lambda kind: kind == KIND)
        assert lease is not None
        execution = Execution(self.jobs, lease)
        for _ in range(5000):
            if self.handler.step(execution):
                self.jobs.complete(lease, result={"purged": True}, commit=self.handler.commit)
                break
        self.assertEqual(
            self.store.data[operation_path(self.tenant, operation.operation_id)]["operation"][
                "status"
            ],
            "succeeded",
        )
        self.assertNotIn(extra, self.store.data)
        self.assertEqual(
            self.store.data[f"device_credential_families/{family}"],
            {
                "tenant_id": self.tenant,
                "device_id": issued.device_id,
                "status": "revoked",
            },
        )
        self.assertEqual(
            self.store.data[f"device_pairings/{pair.pairing_id}"],
            {
                "tenant_id": self.tenant,
                "device_id": issued.device_id,
                "status": "expired",
            },
        )
        self.assertEqual(
            self.store.data[f"device_pairings/{pending.pairing_id}"],
            {
                "tenant_id": self.tenant,
                "status": "expired",
            },
        )
        for path, document in self.store.data.items():
            if path.split("/")[0] not in DEVICE_GLOBALS or path in preserved:
                continue
            self.assertLessEqual(
                set(document),
                {
                    "tenant_id",
                    "device_id",
                    "original_device_id",
                    "status",
                    "family_id",
                    "pairing_id",
                },
            )
            self.assertEqual(len(path.split("/")), 2)
        for path, document in preserved.items():
            self.assertEqual(self.store.data[path], document)
        credentials.authenticate(pairs[1][6].access_token)
        # Actual purge above produced the minimal tombstone; old refresh replay
        # must also remain an authentication rejection at the public HTTP boundary.
        app = create_app(settings_loader=lambda: load_settings(LOCAL_ENV))
        app.dependency_overrides[get_auth_services] = lambda: AuthServices(devices=credentials)
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post(
                "/v1/device-tokens:refresh",
                json=refresh.model_dump(mode="json"),
                headers={"Idempotency-Key": KEY + "refresh"},
            )
        self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(response.json()["error"]["code"], "authentication_required")
        self.assertEqual(
            self.store.data[f"device_credential_families/{family}"],
            {
                "tenant_id": self.tenant,
                "device_id": issued.device_id,
                "status": "revoked",
            },
        )
        for replay in (
            lambda: credentials.create(body, KEY + "pair", "127.0.0.1"),
            lambda: credentials.create(body, KEY + "pending-repair", "127.0.0.1"),
            lambda: credentials.exchange(
                pending.pairing_id,
                PairingExchange(poll_secret=pending.poll_secret),
                KEY + "pending-exchange",
                "127.0.0.1",
            ),
            lambda: credentials.confirm(
                self.identity(OWNER), self.tenant, confirm, KEY + "confirm", "127.0.0.1"
            ),
            lambda: credentials.exchange(pair.pairing_id, exchange, KEY + "exchange", "127.0.0.1"),
            lambda: credentials.refresh(refresh, KEY + "refresh", "127.0.0.1"),
            lambda: credentials.authenticate(refreshed.access_token),
        ):
            with self.assertRaises(APIError):
                replay()
        # New keys cannot recreate receipt payloads for the erased installation.
        with self.assertRaises(APIError) as error:
            credentials.create(body, KEY + "new-pair", "127.0.0.1")
        self.assertEqual(error.exception.code, ErrorCode.RESOURCE_EXPIRED)
        # Other bound tenants and never-bound installations still admit requests.
        credentials.create(pairs[1][0], KEY + "other-new-pair", "127.0.0.1")
        credentials.create(
            PairingCreate(device_name="New Pi", installation_nonce="u" * 32),
            KEY + "unbound-pair",
            "127.0.0.1",
        )

    def test_acceptance_fences_new_pairing_and_existing_receipt_before_purge(self) -> None:
        tokens = DeviceTokens(
            KEYRING,
            project="demo-brainoid",
            credential_epoch=EPOCH,
            test_only=True,
            now=lambda: self.now,
        )
        credentials = DeviceCredentialRepository(
            self.store, self.tenants(), self.authz, tokens, now=lambda: self.now
        )
        body = PairingCreate(device_name="Synthetic Pi", installation_nonce="x" * 32)
        pair = credentials.create(body, KEY + "pair", "127.0.0.1")
        credentials.confirm(
            self.identity(OWNER),
            self.tenant,
            PairingConfirm(
                user_code=pair.user_code, device_name="Synthetic Pi", allowed_entity_ids=[]
            ),
            KEY + "confirm",
            "127.0.0.1",
        )
        # Before acceptance, the supported exact replay returns the original receipt.
        self.assertEqual(credentials.create(body, KEY + "pair", "127.0.0.1"), pair)
        self.repo.create(
            self.identity(OWNER),
            DeleteTenant(
                kind="delete_tenant",
                confirmation_name=self.store.data[f"tenants/{self.tenant}"]["name"],
            ),
            KEY,
            self.tenant,
        )
        before = {
            p: copy.deepcopy(d)
            for p, d in self.store.data.items()
            if p.split("/")[0] in DEVICE_GLOBALS
        }
        for key in (KEY + "pair", KEY + "new-pair"):
            with self.assertRaises(APIError) as error:
                credentials.create(body, key, "127.0.0.1")
            self.assertEqual(error.exception.code, ErrorCode.RESOURCE_EXPIRED)
        self.assertEqual(
            before, {p: d for p, d in self.store.data.items() if p.split("/")[0] in DEVICE_GLOBALS}
        )
