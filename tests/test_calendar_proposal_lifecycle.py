"""Actual standalone HTTP producer has export and all lifecycle erasure coverage."""

import json
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from brainoid_api.auth.context import AuthenticatedIdentity
from brainoid_api.common.pagination import Cursors
from brainoid_api.repositories.tenants import Tenants
from brainoid_api.services.calendar_events import CalendarEvents
from brainoid_api.services.calendar_proposals import Proposals
from brainoid_api.services.deletion_requests import DeleteTenant
from brainoid_contracts import ProviderId, ResourceId
from brainoid_contracts.deletion import subject_id
from brainoid_memory.calendar_oauth import WRITE, OAuthStart
from brainoid_memory.calendar_proposals import CHANGE, ProposalView, proposal_path
from brainoid_memory.calendar_reader import CalendarReader, CalendarSelection
from brainoid_worker.lifecycle_handlers import LifecycleHandlers
from brainoid_worker.purge_resource import PurgeResource
from fakes.calendar_events import EventsProvider
from fakes.calendar_scenarios import OAuthTransport
from fakes.memory_repository import TENANT
from fakes.purge import FakePurgePages
from test_calendar_lifecycle import calendar
from test_deletion_requests import OWNER, USER, DeletionFixture
from test_exports import ExportFixture


def produce(
    tenants: Tenants, identity: AuthenticatedIdentity, tenant: ResourceId, directory: Path
) -> tuple[Proposals, ProposalView]:
    authorization = calendar(tenants, directory)
    transport = OAuthTransport()
    transport.scopes = WRITE
    authorization.provider.post = transport.post  # type: ignore[attr-defined]
    response = authorization.start(
        identity,
        tenant,
        OAuthStart(scope_profile="read_write", return_target="web"),
        "calendar-lifecycle-" + identity.user_id,
    )
    state = parse_qs(urlsplit(response.authorization_url).query)["state"][0]
    authorization.callback(state, "synthetic", None)
    integration = authorization.integrations(identity, tenant)[0].id
    reader = CalendarReader(
        tenants._store, EventsProvider(), authorization.secrets, tenants._cursors, now=tenants._now
    )
    events = CalendarEvents(authorization, reader)
    events.select(
        identity,
        tenant,
        integration,
        CalendarSelection(allowed_calendar_ids=[ProviderId("private-calendar")]),
        "calendar-lifecycle-select-" + identity.user_id,
        '"1"',
    )
    service = Proposals(events)
    body = CHANGE.validate_python(
        {
            "action": "create",
            "calendar_id": "private-calendar",
            "summary": "SYNTHETIC_STANDALONE_PROPOSAL",
            "start": {"date": "2026-03-08"},
            "end": {"date": "2026-03-09"},
        }
    )
    return service, service.create(
        identity, tenant, integration, body, "calendar-lifecycle-create-" + identity.user_id
    )


class ProposalExportTests(ExportFixture):
    def test_real_standalone_projection_and_current_grant_download_fence(self) -> None:
        service, proposal = produce(
            self.tenants,
            self.identity(),
            TENANT,
            Path(self.enterContext(tempfile.TemporaryDirectory())),
        )
        operation = self.ready()
        archive = self.archive(operation)
        public = [r["data"] for r in archive["records"] if r["kind"] == "calendar_proposals"]
        self.assertEqual(public, [proposal.model_dump(mode="json")])
        raw = json.dumps(archive)
        self.assertIn("SYNTHETIC_STANDALONE_PROPOSAL", raw)
        for field in ("secret_ref", "SYNTHETIC_ACCESS", "origin", "args_hash_private", "binding"):
            self.assertNotIn(field, raw)
        self.store.data[f"tenants/{TENANT}/integrations/{proposal.integration_id}"]["status"] = (
            "revoked"
        )
        self.assertNotEqual(self.download(operation).status_code, 200)


class ProposalLifecycleTests(DeletionFixture):
    def test_real_standalone_producer_erased_for_member_account_and_tenant(self) -> None:
        for kind in ("member", "account", "tenant"):
            with self.subTest(kind=kind):
                self.setUp()
                tenants = Tenants(
                    self.store,
                    Cursors(b"k" * 32, now=lambda: self.now),
                    ledger=self.authz,
                    now=lambda: self.now,
                )
                directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
                service, proposal = produce(tenants, self.identity(USER), self.tenant, directory)
                self.handler = PurgeResource(
                    self.jobs,
                    FakePurgePages(self.store),
                    handlers=LifecycleHandlers(),
                    hooks={
                        "cache": self.cache,
                        "export": self.export,
                        "calendar": service.events.authorization.secrets,
                    },
                    account_finalizer=self.finalizer,
                )
                operation = (
                    self.repo.create(
                        self.identity(OWNER),
                        DeleteTenant(
                            kind="delete_tenant",
                            confirmation_name=self.store.data[f"tenants/{self.tenant}"]["name"],
                        ),
                        "calendar-proposal-delete-tenant",
                        self.tenant,
                    )
                    if kind == "tenant"
                    else self.accept(account=kind == "account")
                )
                self.drain(operation, subject_id(USER) if kind == "account" else self.tenant)
                self.assertNotIn(proposal_path(self.tenant, proposal.proposal_id), self.store.data)
                self.assertNotIn(
                    "SYNTHETIC_STANDALONE_PROPOSAL", json.dumps(self.store.data, default=str)
                )
