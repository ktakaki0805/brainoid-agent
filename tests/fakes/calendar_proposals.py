"""CAL03 scenarios shared by strict fake and Firestore Emulator."""

from typing import Any, cast
from unittest.mock import patch

from brainoid_api.auth.runtime import AuthServices, get_auth_services
from brainoid_api.routes.agents import reader_identity
from brainoid_api.services.calendar_events import CalendarEvents
from brainoid_api.services.calendar_proposals import Proposals
from brainoid_contracts import FirebaseUid
from brainoid_contracts.errors import APIError, ErrorCode
from brainoid_contracts.idempotency import Idempotency
from brainoid_memory.calendar_oauth import WRITE
from brainoid_memory.calendar_proposals import CHANGE, ProposalView, proposal_path
from brainoid_memory.calendar_reader import CalendarQuery, CalendarReader, CalendarSelection
from fastapi import FastAPI

from fakes.calendar_events import EventsProvider
from fakes.calendar_scenarios import CalendarFixture


class ProposalScenarios(CalendarFixture):
    def setup_proposals(self) -> None:
        self.setup_calendar()
        state, _ = self.start()
        self.service.callback(state, "code", None)
        self.events_provider = EventsProvider()
        self.reader = CalendarReader(
            self.store,
            self.events_provider,
            self.secrets,
            self.tenants._cursors,
            now=self.clock.now,
        )
        self.events = CalendarEvents(self.service, self.reader)
        self.query = CalendarQuery.model_validate(
            {
                "calendar_id": "private/calendar@example.test",
                "time_min": "2026-03-01T00:00:00Z",
                "time_max": "2026-04-01T00:00:00Z",
            }
        )
        self.transport.scopes = WRITE
        state, _ = self.start(profile="read_write", key="calendar-proposal-oauth")
        self.service.callback(state, "code", None)
        self.integration = next(r["id"] for r in self.records() if WRITE in r["granted_scopes"])
        self.events.select(
            self.identity(),
            self.tenant,
            self.integration,
            CalendarSelection(allowed_calendar_ids=[self.query.calendar_id]),
            "calendar-proposal-selection",
            '"1"',
        )
        self.proposals = Proposals(self.events)
        self.body = {
            "action": "create",
            "calendar_id": self.query.calendar_id,
            "summary": "SYNTHETIC_PRIVATE_PROPOSAL",
            "start": {"date": "2026-03-08"},
            "end": {"date": "2026-03-09"},
        }

    def events_client(self) -> Any:
        client = self.client()
        cast(FastAPI, client.app).dependency_overrides[reader_identity] = lambda: self.identity()
        cast(FastAPI, client.app).dependency_overrides[get_auth_services] = lambda: AuthServices(
            tenants=self.tenants, calendar=self.service, calendar_events=self.events
        )
        return client

    def propose(
        self, body: dict[str, Any] | None = None, key: str = "calendar-proposal-create"
    ) -> ProposalView:
        return self.proposals.create(
            self.identity(),
            self.tenant,
            self.integration,
            CHANGE.validate_python(body or self.body),
            key,
        )

    def test_proposal_immutable_replay_deadline_and_owner(self) -> None:
        first = self.propose()
        self.assertEqual(first, self.propose())
        self.assertEqual(first.status, "pending")
        self.assertEqual((first.expires_at - self.clock.now()).total_seconds(), 300)
        with self.assertRaises(APIError) as conflict:
            self.propose({**self.body, "summary": "changed"})
        self.assertEqual(conflict.exception.code, ErrorCode.IDEMPOTENCY_CONFLICT)
        changed = self.propose({**self.body, "summary": "changed"}, key="calendar-another-proposal")
        self.assertNotEqual(first.proposal_id, changed.proposal_id)
        for uid in ("demo-a-owner", "demo-b-member"):
            with self.assertRaises(APIError):
                self.proposals.get(self.identity(FirebaseUid(uid)), self.tenant, first.proposal_id)
            with self.assertRaises(APIError):
                self.proposals.reject(
                    self.identity(FirebaseUid(uid)),
                    self.tenant,
                    first.proposal_id,
                    "calendar-wrong-reject",
                )
        self.clock.advance(299)
        self.assertEqual(
            self.proposals.get(self.identity(), self.tenant, first.proposal_id).status, "pending"
        )
        self.clock.advance(1)
        self.assertEqual(
            self.proposals.get(self.identity(), self.tenant, first.proposal_id).status, "expired"
        )
        self.assertEqual(self.propose().expires_at, first.expires_at)
        self.assertEqual(
            self.proposals.reject(
                self.identity(), self.tenant, first.proposal_id, "calendar-expiry-reject"
            ).status,
            "expired",
        )

    def test_replay_and_final_release_keep_original_purge_fence(self) -> None:
        first = self.propose()
        count = len(self.events_provider.calls)
        self.assertEqual(first, self.propose())
        self.assertEqual(len(self.events_provider.calls), count)
        path = f"tenants/{self.tenant}/purge_barriers/history"
        self.store.run(lambda tx: tx.set(path, {"epoch": 1}))
        with self.assertRaises(APIError):
            self.proposals.get(self.identity(), self.tenant, first.proposal_id)
        with self.assertRaises(APIError):
            self.propose()
        self.assertEqual(len(self.events_provider.calls), count)
        saved = self.store.run(lambda tx: tx.get(proposal_path(self.tenant, first.proposal_id)))
        assert saved is not None
        self.assertEqual(saved["origin"]["purge_epoch"], 0)

    def test_final_release_rechecks_origin_after_successful_commit(self) -> None:
        execute = Idempotency.execute
        calls = len(self.events_provider.calls)

        def changed(service: Idempotency, **kwargs: Any) -> Any:
            result = execute(service, **kwargs)
            if kwargs["operation"].startswith("calendar.propose:"):
                self.store.run(
                    lambda tx: tx.set(f"tenants/{self.tenant}/purge_barriers/history", {"epoch": 1})
                )
            return result

        with patch.object(Idempotency, "execute", changed), self.assertRaises(APIError):
            self.propose()
        saved = self.store.run(
            lambda tx: tx.collection(f"tenants/{self.tenant}/calendar_proposals")
        )
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0][1]["origin"]["purge_epoch"], 0)
        self.assertEqual(len(self.events_provider.calls), calls + 1)

    def test_purge_during_provider_does_not_restamp_or_save(self) -> None:
        def purge() -> None:
            self.events_provider.after = lambda: None
            self.store.run(
                lambda tx: tx.set(f"tenants/{self.tenant}/purge_barriers/history", {"epoch": 1})
            )

        self.events_provider.after = purge
        with self.assertRaises(APIError):
            self.propose()
        self.assertEqual(
            self.store.run(lambda tx: tx.collection(f"tenants/{self.tenant}/calendar_proposals")),
            [],
        )

    def test_target_etag_scope_invitation_and_series(self) -> None:
        update = {
            **self.body,
            "action": "update",
            "event_id": "opaque-event",
            "provider_etag": '"provider-v3"',
        }
        self.assertEqual(self.propose(update).action, "update")
        delete = {k: update[k] for k in ("calendar_id", "event_id", "provider_etag")}
        delete["action"] = "delete"
        self.assertEqual(self.propose(delete, "calendar-proposal-delete").action, "delete")
        for field, target_value in (
            ("event_id", "other-event"),
            ("provider_etag", "other-etag"),
            ("calendar_id", "other-calendar"),
        ):
            with self.assertRaises(APIError):
                self.propose({**update, field: target_value}, "calendar-other-target-" + field)
        page = self.events_provider.pages[None]
        for field, value in (
            ("attendees", [{"email": "synthetic@example.test"}]),
            ("recurrence", ["RRULE:FREQ=DAILY"]),
            ("recurringEventId", "series"),
        ):
            page["items"][0][field] = value
            with self.assertRaises(APIError):
                self.propose(update, "calendar-forbidden-target-" + field)
            del page["items"][0][field]
        read_integration = next(r["id"] for r in self.records() if WRITE not in r["granted_scopes"])
        with self.assertRaises(APIError):
            self.proposals.create(
                self.identity(),
                self.tenant,
                read_integration,
                CHANGE.validate_python(self.body),
                "calendar-readonly-scope",
            )

    def test_time_hash_and_transport_revocation(self) -> None:
        for end in (
            {"date": "2026-03-08"},
            {"date": "2026-03-07"},
            {"date_time": "2026-03-09T00:00:00Z", "timezone": "UTC"},
        ):
            with self.assertRaises(ValueError):
                CHANGE.validate_python({**self.body, "end": end})
        for field in ("attendees", "recurrence", "owner_user_id", "run_id", "args_hash"):
            with self.assertRaises(ValueError):
                CHANGE.validate_python({**self.body, field: []})
        first = self.propose()
        path = proposal_path(self.tenant, first.proposal_id)
        original = self.get(path)
        for field, value in (
            ("args_hash", "0" * 64),
            ("expires_at", "2030-01-01T00:00:00Z"),
            ("normalized_request", {**self.body, "summary": "changed"}),
        ):
            self.put(path, {**original, field: value})
            with self.assertRaises(APIError):
                self.proposals.get(self.identity(), self.tenant, first.proposal_id)
        self.put(path, original)
        update = {
            **self.body,
            "action": "update",
            "event_id": "opaque-event",
            "provider_etag": '"provider-v3"',
        }
        self.events_provider.after = self.revoke_member
        with self.assertRaises(APIError):
            self.propose(update, "calendar-target-revoked")

    def test_http_public_projection_reject_and_unconfigured_confirm(self) -> None:
        with self.events_client() as client:
            root = f"/v1/tenants/{self.tenant}"
            response = client.post(
                root + f"/integrations/{self.integration}/calendar-proposals",
                json=self.body,
                headers={"Idempotency-Key": "calendar-http-proposal"},
            )
            self.assertEqual(response.status_code, 201, response.text)
            path = root + "/calendar-proposals/" + response.json()["proposal_id"]
            for field in ("secret_ref", "binding", "owner_user_id", "origin", "SYNTHETIC_ACCESS"):
                self.assertNotIn(field, response.text)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(
                client.post(
                    path + ":confirm",
                    json={"confirmed": True, "args_hash": response.json()["args_hash"]},
                ).status_code,
                503,
            )
            self.assertEqual(client.post(path + ":reject", json={"result": {}}).status_code, 400)
            for _ in range(2):
                rejected = client.post(
                    path + ":reject", headers={"Idempotency-Key": "calendar-http-reject"}
                )
                self.assertEqual(rejected.status_code, 200, rejected.text)
                self.assertEqual(rejected.json()["status"], "declined")
            self.assertEqual(client.get(path).json()["status"], "declined")

    def test_proposal_same_key_lost_probe_response_recovers_without_duplicate(self) -> None:
        update = {
            **self.body,
            "action": "update",
            "event_id": "opaque-event",
            "provider_etag": '"provider-v3"',
        }
        original = self.events_provider.event
        fired = False

        def failed(*args: Any) -> dict[str, Any]:
            nonlocal fired
            if not fired:
                fired = True
                self.propose(update)
                raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)
            return original(*args)

        with patch.object(self.events_provider, "event", side_effect=failed):
            result = self.propose(update)
        self.assertEqual(self.propose(update), result)

    def test_writable_acl_and_locked_target_are_required(self) -> None:
        self.events_provider.pages[None]["accessRole"] = "reader"
        with self.assertRaises(APIError):
            self.propose()
        self.events_provider.pages[None]["accessRole"] = "owner"
        update = {
            **self.body,
            "action": "update",
            "event_id": "opaque-event",
            "provider_etag": '"provider-v3"',
        }
        for field, value in (
            ("locked", True),
            ("attendeesOmitted", True),
            ("eventType", "birthday"),
        ):
            self.events_provider.pages[None]["items"][0][field] = value
            with self.assertRaises(APIError):
                self.propose(update, "calendar-unsupported-" + field)
            del self.events_provider.pages[None]["items"][0][field]

    def test_committed_create_response_loss_replays_without_provider_io(self) -> None:
        from brainoid_persistence.firestore import PersistenceUnavailable

        original = self.store.run
        lost = False

        def run(work: Any) -> Any:
            nonlocal lost
            result = original(work)
            records = original(
                lambda tx: tx.collection(f"tenants/{self.tenant}/calendar_proposals")
            )
            if records and not lost:
                lost = True
                raise PersistenceUnavailable()
            return result

        with patch.object(self.store, "run", side_effect=run):
            with self.assertRaises(PersistenceUnavailable):
                self.propose()
        before = len(self.events_provider.calls)
        result = self.propose()
        self.assertEqual(result.status, "pending")
        self.assertEqual(len(self.events_provider.calls), before)
        self.assertEqual(
            len(
                self.store.run(
                    lambda tx: tx.collection(f"tenants/{self.tenant}/calendar_proposals")
                )
            ),
            1,
        )

    def test_second_integration_uses_distinct_idempotency_namespace(self) -> None:
        first = self.propose()
        state, _ = self.start(key="calendar-second-write-oauth", profile="read_write")
        self.service.callback(state, "code", None)
        self.integration = next(
            r["id"]
            for r in self.records()
            if WRITE in r["granted_scopes"] and r["id"] != first.integration_id
        )
        self.events.select(
            self.identity(),
            self.tenant,
            self.integration,
            CalendarSelection(allowed_calendar_ids=[self.query.calendar_id]),
            "calendar-second-write-select",
            '"1"',
        )
        second = self.propose()
        self.assertNotEqual(first.proposal_id, second.proposal_id)
        self.assertNotEqual(first.integration_id, second.integration_id)
        self.assertEqual(self.propose(), second)

    def test_restricted_writer_create_only_and_utc_dst_normalization(self) -> None:
        self.events_provider.pages[None]["accessRole"] = "writerWithoutPrivateAccess"
        timed = {
            **self.body,
            "start": {"date_time": "2026-11-01T01:30:00-04:00", "timezone": "America/New_York"},
            "end": {"date_time": "2026-11-01T01:30:00-05:00", "timezone": "America/New_York"},
        }
        proposed = self.propose(timed)
        body = proposed.normalized_request.model_dump(mode="json")
        self.assertEqual(body["start"]["date_time"], "2026-11-01T05:30:00Z")
        self.assertEqual(body["end"]["date_time"], "2026-11-01T06:30:00Z")
        with self.assertRaises(ValueError):
            CHANGE.validate_python(
                {
                    **timed,
                    "start": {"date_time": "2026-11-01T01:30:00", "timezone": "America/New_York"},
                }
            )
        with self.assertRaises(APIError):
            self.propose(
                {
                    **self.body,
                    "action": "update",
                    "event_id": "opaque-event",
                    "provider_etag": '"provider-v3"',
                },
                "calendar-restricted-update",
            )
