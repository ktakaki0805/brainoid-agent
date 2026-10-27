"""Actual OAuth producers participate in accepted lifecycle purge and export."""

import json
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from brainoid_api.adapters.calendar_secrets import LocalCalendarSecrets
from brainoid_api.adapters.google_oauth import GoogleOAuth
from brainoid_api.common.pagination import Cursors
from brainoid_api.repositories.tenants import Tenants
from brainoid_api.services.calendar_authorization import CalendarAuthorization, path
from brainoid_api.services.deletion_requests import DeleteTenant
from brainoid_contracts import ResourceId
from brainoid_contracts.deletion import subject_id
from brainoid_memory.calendar_oauth import OAuthStart, SecretUnavailable
from brainoid_worker.lifecycle_handlers import LifecycleHandlers
from brainoid_worker.purge_resource import PurgeResource
from fakes.calendar_scenarios import OAuthTransport
from fakes.memory_repository import TENANT
from fakes.purge import FakePurgePages
from pydantic import SecretStr
from test_deletion_requests import OWNER, USER, DeletionFixture
from test_exports import ExportFixture


def calendar(tenants: Tenants, directory: Path) -> CalendarAuthorization:
    return CalendarAuthorization(
        tenants,
        GoogleOAuth(
            client_id="synthetic-client",
            client_secret=SecretStr("synthetic-secret"),
            redirect_uri="https://api.example.test/oauth",
            post=OAuthTransport().post,
        ),
        LocalCalendarSecrets(directory, b"s" * 32, now=tenants._now),
        returns={
            "mobile": "https://app.example.test/return",
            "web": "https://web.example.test/return",
        },
    )


class CalendarLifecycleTests(DeletionFixture):
    def test_actual_pending_and_connected_producers_erased_for_all_lifecycle_scopes(self) -> None:
        for kind in ("member", "account", "tenant"):
            with self.subTest(kind=kind):
                self.setUp()
                tenants = Tenants(
                    self.store,
                    Cursors(b"k" * 32, now=lambda: self.now),
                    ledger=self.authz,
                    now=lambda: self.now,
                )
                service = calendar(tenants, Path(self.enterContext(tempfile.TemporaryDirectory())))
                ids = []
                for user, completed in ((USER, True), (USER, False), (OWNER, True)):
                    response = service.start(
                        self.identity(user),
                        self.tenant,
                        OAuthStart(scope_profile="read_write", return_target="web"),
                        "synthetic-purge-" + user + str(completed),
                    )
                    state = parse_qs(urlsplit(response.authorization_url).query)["state"][0]
                    auth = ResourceId(state.split(".")[1])
                    if completed:
                        service.callback(state, "synthetic-code", None)
                    ids.append(auth)
                refs = [
                    self.store.data[path(self.tenant, auth)].get(
                        "secret_ref", self.store.data[path(self.tenant, auth)].get("transient_ref")
                    )
                    for auth in ids
                ]
                self.handler = PurgeResource(
                    self.jobs,
                    FakePurgePages(self.store),
                    handlers=LifecycleHandlers(),
                    hooks={"cache": self.cache, "export": self.export, "calendar": service.secrets},
                    account_finalizer=self.finalizer,
                )
                operation = (
                    self.repo.create(
                        self.identity(OWNER),
                        DeleteTenant(
                            kind="delete_tenant",
                            confirmation_name=self.store.data[f"tenants/{self.tenant}"]["name"],
                        ),
                        "synthetic-delete-tenant",
                        self.tenant,
                    )
                    if kind == "tenant"
                    else self.accept(account=kind == "account")
                )
                self.drain(operation, subject_id(USER) if kind == "account" else self.tenant)
                for index, auth in enumerate(ids):
                    erased = index < 2 or kind == "tenant"
                    self.assertEqual(path(self.tenant, auth) not in self.store.data, erased)
                    if erased:
                        with self.assertRaises(SecretUnavailable):
                            service.secrets.get(str(refs[index]))
                    else:
                        service.secrets.get(str(refs[index]))

    def test_missing_secret_hook_cannot_certify_erasure(self) -> None:
        tenants = Tenants(
            self.store,
            Cursors(b"k" * 32, now=lambda: self.now),
            ledger=self.authz,
            now=lambda: self.now,
        )
        service = calendar(tenants, Path(self.enterContext(tempfile.TemporaryDirectory())))
        service.start(
            self.identity(USER),
            self.tenant,
            OAuthStart(scope_profile="read", return_target="web"),
            "synthetic-missing-hook",
        )
        operation = self.accept()
        from brainoid_jobs import RetryableError
        from brainoid_jobs.models import job_id
        from brainoid_memory.lifecycle import KIND
        from brainoid_worker.entry import Execution

        identity = job_id(self.tenant, KIND, operation.operation_id)
        self.jobs.activate(self.tenant, identity)
        delivery = self.jobs.prepare_dispatch(self.tenant, identity, queue_name=KIND)
        assert delivery is not None
        lease = self.jobs.acquire(delivery, known_handler=lambda kind: kind == KIND)
        assert lease is not None
        execution = Execution(self.jobs, lease)
        with self.assertRaises(RetryableError):
            for _ in range(5000):
                self.assertFalse(self.handler.step(execution))
        self.assertTrue(any("/calendar_authorizations/" in p for p in self.store.data))


class CalendarExportTests(ExportFixture):
    def test_archive_has_only_current_owned_public_metadata_and_manifest_rechecks(self) -> None:
        service = calendar(self.tenants, Path(self.enterContext(tempfile.TemporaryDirectory())))
        response = service.start(
            self.identity(),
            TENANT,
            OAuthStart(scope_profile="read", return_target="web"),
            "synthetic-export-calendar",
        )
        state = parse_qs(urlsplit(response.authorization_url).query)["state"][0]
        service.callback(state, "synthetic-code", None)
        operation = self.ready()
        archive = self.archive(operation)
        public = [r["data"] for r in archive["records"] if r["kind"] == "integrations"]
        self.assertEqual(len(public), 1)
        raw = json.dumps(archive)
        for forbidden in (
            "secret_ref",
            "transient_ref",
            "calendar_authorizations",
            "granted_scopes",
            "SYNTHETIC_ACCESS",
            "SYNTHETIC_REFRESH",
            state,
        ):
            self.assertNotIn(forbidden, raw)
        location = f"tenants/{TENANT}/integrations/{public[0]['id']}"
        self.store.data[location]["secret_ref"] = "replacement-reference"
        self.assertNotEqual(self.download(operation).status_code, 200)
