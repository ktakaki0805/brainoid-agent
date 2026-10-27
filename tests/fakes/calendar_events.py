"""Synthetic event reader fixtures, always composed through the OAuth producer."""

import copy
from collections.abc import Callable
from datetime import timedelta
from typing import Any, cast
from unittest.mock import patch

from brainoid_api.auth.runtime import AuthServices, get_auth_services
from brainoid_api.routes.agents import reader_identity
from brainoid_api.services.calendar_events import CalendarEvents
from brainoid_contracts import FirebaseUid, ResourceId
from brainoid_contracts.errors import APIError, ErrorCode
from brainoid_memory.calendar_reader import CalendarQuery, CalendarReader, CalendarSelection
from fastapi import FastAPI
from pydantic import SecretStr

from fakes.calendar_scenarios import CalendarFixture


def event(**changes: Any) -> dict[str, Any]:
    return {
        "id": "opaque-event",
        "summary": "SYNTHETIC_PRIVATE_APPOINTMENT",
        "etag": '"provider-v3"',
        "start": {"dateTime": "2026-03-08T01:30:00-05:00", "timeZone": "America/New_York"},
        "end": {"dateTime": "2026-03-08T03:30:00-04:00", "timeZone": "America/New_York"},
        **changes,
    }


class EventsProvider:
    def __init__(self) -> None:
        self.pages: dict[str | None, dict[str, Any]] = {
            None: {"timeZone": "America/New_York", "accessRole": "owner", "items": [event()]}
        }
        self.calls: list[tuple[CalendarQuery, str | None]] = []
        self.after: Callable[[], None] = lambda: None

    def events(
        self, token: SecretStr, query: CalendarQuery, page_token: str | None
    ) -> dict[str, Any]:
        self.calls.append((query, page_token))
        value = copy.deepcopy(self.pages[page_token])
        self.after()
        return value


class CalendarEventsScenarios(CalendarFixture):
    def setup_events(self) -> None:
        self.setup_calendar()
        state, _ = self.start()
        self.service.callback(state, "code", None)
        self.integration = ResourceId(self.records()[0]["id"])
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
        self.select()

    def select(
        self,
        calendars: list[str] | None = None,
        version: int = 1,
        key: str = "calendar-select-key-001",
    ) -> Any:
        return self.events.select(
            self.identity(),
            self.tenant,
            self.integration,
            CalendarSelection.model_validate(
                {
                    "allowed_calendar_ids": calendars
                    if calendars is not None
                    else [self.query.calendar_id]
                }
            ),
            key,
            f'"{version}"',
        )

    def events_client(self) -> Any:
        client = self.client()
        cast(FastAPI, client.app).dependency_overrides[reader_identity] = lambda: self.identity()
        cast(FastAPI, client.app).dependency_overrides[get_auth_services] = lambda: AuthServices(
            tenants=self.tenants, calendar=self.service, calendar_events=self.events
        )
        return client

    def read(self, query: CalendarQuery | None = None) -> Any:
        return self.events.list(self.identity(), self.tenant, self.integration, query or self.query)

    def test_selected_calendar_real_producer_and_http_contract(self) -> None:
        with self.events_client() as client:
            url = f"/v1/tenants/{self.tenant}/integrations/{self.integration}"
            page = client.get(
                url + "/calendar-events",
                params=self.query.model_dump(mode="json", exclude_none=True),
            )
            self.assertEqual(page.status_code, 200, page.text)
            self.assertIsNone(page.json()["next_cursor"])
            item = page.json()["items"][0]
            self.assertEqual(item["start"]["date_time"], "2026-03-08T06:30:00Z")
            self.assertEqual(item["end"]["date_time"], "2026-03-08T07:30:00Z")
            self.assertEqual(item["start"]["timezone"], "America/New_York")
            self.assertEqual(item["etag"], '"provider-v3"')
            self.assertIn("fetched_at", item)
            self.assertNotIn("description", item)
            updated = client.patch(
                url,
                json={"allowed_calendar_ids": []},
                headers={"Idempotency-Key": "calendar-empty-selection", "If-Match": '"2"'},
            )
            self.assertEqual(updated.status_code, 200, updated.text)
            self.assertEqual(updated.headers["etag"], '"3"')
            self.assertNotEqual(
                client.get(
                    url + "/calendar-events",
                    params=self.query.model_dump(mode="json", exclude_none=True),
                ).status_code,
                200,
            )

    def test_empty_page_with_next_cursor_and_query_generation_binding(self) -> None:
        self.events_provider.pages[None]["items"] = []
        self.events_provider.pages[None]["nextPageToken"] = "next"
        self.events_provider.pages["next"] = {"timeZone": "America/New_York", "items": [event()]}
        first = self.read()
        self.assertEqual(first.items, [])
        self.assertIsNotNone(first.next_cursor)
        query = self.query.model_copy(update={"cursor": first.next_cursor})
        self.assertEqual(len(self.read(query).items), 1)
        for change in (
            {"calendar_id": "not-selected"},
            {"limit": 2},
            {"time_min": self.query.time_min + timedelta(hours=1)},
        ):
            with self.assertRaises(APIError):
                self.read(query.model_copy(update=change))
        self.select(version=2, key="calendar-reselection-02")
        with self.assertRaises(APIError):
            self.read(query)

    def test_opaque_cursor_ascii_escape_and_unicode_round_trip(self) -> None:
        for token in ("a" * 384, "\\" * 100, '"' * 100, "あ" * 50):
            with self.subTest(token_length=len(token)):
                self.events_provider.pages[None]["nextPageToken"] = token
                self.events_provider.pages[token] = {"timeZone": "UTC", "items": []}
                first = self.read()
                self.assertTrue(first.next_cursor)
                self.assertLessEqual(len(first.next_cursor), 2048)
                final = self.read(self.query.model_copy(update={"cursor": first.next_cursor}))
                self.assertEqual(final.items, [])
                self.assertIsNone(final.next_cursor)
                self.assertEqual(self.events_provider.calls[-1][1], token)

    def test_unrepresentable_cursor_fails_whole_page_with_sanitized_service_and_http_503(
        self,
    ) -> None:
        with self.events_client() as client, self.assertLogs("brainoid_api", level="INFO") as logs:
            for token in ("\\" * 300, '"' * 300, "あ" * 100, "a" * 385):
                with self.subTest(token_length=len(token)):
                    self.events_provider.pages[None]["nextPageToken"] = token
                    with self.assertRaises(APIError) as error:
                        self.read()
                    self.assertEqual(error.exception.code, ErrorCode.TEMPORARILY_UNAVAILABLE)
                    response = client.get(
                        f"/v1/tenants/{self.tenant}/integrations/{self.integration}/calendar-events",
                        params=self.query.model_dump(mode="json", exclude_none=True),
                    )
                    self.assertEqual(response.status_code, 503, response.text)
                    self.assertEqual(response.json()["error"]["code"], "temporarily_unavailable")
                    self.assertTrue(response.json()["error"]["retryable"])
                    self.assertNotIn("items", response.json())
                    self.assertNotIn("SYNTHETIC_PRIVATE_APPOINTMENT", response.text)
                    self.assertNotIn(token, response.text)
        self.assertNotIn("SYNTHETIC_PRIVATE_APPOINTMENT", "\n".join(logs.output))

    def test_cursor_issue_preserves_current_authorization_denial(self) -> None:
        self.events_provider.pages[None]["nextPageToken"] = "next"
        with patch.object(
            self.reader.cursors, "issue", side_effect=APIError(ErrorCode.OPERATION_FORBIDDEN)
        ):
            with self.assertRaises(APIError) as denied:
                self.read()
        self.assertEqual(denied.exception.code, ErrorCode.OPERATION_FORBIDDEN)

    def test_owner_scope_status_and_origin_are_current(self) -> None:
        location = f"tenants/{self.tenant}/integrations/{self.integration}"
        original = self.get(location)
        changes: list[dict[str, Any]] = [
            {"owner_user_id": "other"},
            {"status": "revoked"},
            {"deleted": True},
            {"allowed_calendar_ids": []},
            {"granted_scopes": []},
            {"secret_ref": "other"},
        ]
        for change in changes:
            self.put(location, {**original, **change})
            before = len(self.events_provider.calls)
            with self.assertRaises(APIError):
                self.read()
            self.assertEqual(len(self.events_provider.calls), before)
        self.put(location, original)
        self.revoke_member()
        with self.assertRaises(APIError):
            self.read()

    def test_provider_malformed_failure_and_late_revocation_never_empty_success(self) -> None:
        original = copy.deepcopy(self.events_provider.pages[None])
        for malformed in (
            {"error": "PRIVATE_PROVIDER_ERROR"},
            {"timeZone": "UTC", "items": [event(), {}]},
            {"timeZone": "invalid", "items": []},
            {"timeZone": "UTC", "items": "wrong"},
        ):
            self.events_provider.pages[None] = malformed
            with self.assertRaises(APIError):
                self.read()
        self.events_provider.pages[None] = original
        self.events_provider.after = self.revoke_member
        with self.assertRaises(APIError):
            self.read()

    def test_selection_if_match_idempotency_acl_and_empty(self) -> None:
        self.assertEqual(self.select().version, 2)
        with self.assertRaises(APIError):
            self.select(version=1, key="calendar-stale-selection")
        self.events_provider.pages[None]["accessRole"] = "freeBusyReader"
        with self.assertRaises(APIError):
            self.select(["forbidden-calendar"], version=2, key="calendar-denied-selection")
        self.select([], version=2, key="calendar-cleared-selection")
        with self.assertRaises(APIError):
            self.read()

    def test_readable_roles_preserve_redaction_and_reject_unknown_access(self) -> None:
        page = self.events_provider.pages[None]
        page["items"] = [event(summary="")]
        for version, role in enumerate(
            ("reader", "writer", "writerWithoutPrivateAccess", "owner"), start=2
        ):
            with self.subTest(role=role):
                page["accessRole"] = role
                selected = self.select(version=version, key="calendar-role-" + role)
                self.assertEqual(selected.version, version + 1)
                item = self.read().items[0].model_dump(mode="json")
                self.assertEqual(item["summary"], "予定")
                self.assertNotIn("description", item)
        for role in ("none", "freeBusyReader", "unknown"):
            with self.subTest(role=role):
                page["accessRole"] = role
                with self.assertRaises(APIError) as denied:
                    self.select(version=6, key="calendar-role-" + role)
                self.assertEqual(denied.exception.code, ErrorCode.OPERATION_FORBIDDEN)
                self.assertEqual(self.records()[0]["version"], 6)

    def test_committed_selection_replays_without_provider_or_body_conflict_probe(self) -> None:
        for code in (ErrorCode.TEMPORARILY_UNAVAILABLE, ErrorCode.AUTHENTICATION_REQUIRED):
            with (
                self.subTest(code=code),
                patch.object(self.events_provider, "events", side_effect=APIError(code)) as send,
            ):
                self.assertEqual(self.select().version, 2)
                with self.assertRaises(APIError) as conflict:
                    self.select(["different-calendar"])
                self.assertEqual(conflict.exception.code, ErrorCode.IDEMPOTENCY_CONFLICT)
                with self.assertRaises(APIError) as stale:
                    self.select(key="calendar-new-stale-key")
                self.assertEqual(stale.exception.code, ErrorCode.VERSION_CONFLICT)
                self.assertEqual(send.call_count, 0)

    def test_committed_selection_still_requires_current_owner_and_membership(self) -> None:
        with patch.object(self.events_provider, "events") as send:
            with self.assertRaises(APIError):
                self.events.select(
                    self.identity(FirebaseUid("demo-a-owner")),
                    self.tenant,
                    self.integration,
                    CalendarSelection(allowed_calendar_ids=[self.query.calendar_id]),
                    "calendar-select-key-001",
                    '"1"',
                )
            self.revoke_member()
            with self.assertRaises(APIError):
                self.select()
            self.assertEqual(send.call_count, 0)

    def test_same_key_commit_during_probe_converges_to_receipt(self) -> None:
        before = len(self.events_provider.calls)
        key = "calendar-parallel-same-key"

        def commit_other_request() -> None:
            self.events_provider.after = lambda: None
            self.assertEqual(self.select(version=2, key=key).version, 3)

        self.events_provider.after = commit_other_request
        self.assertEqual(self.select(version=2, key=key).version, 3)
        self.assertEqual(self.records()[0]["version"], 3)
        self.assertEqual(len(self.events_provider.calls), before + 2)

    def test_different_key_commit_during_probe_preserves_version_conflict(self) -> None:
        def commit_other_request() -> None:
            self.events_provider.after = lambda: None
            self.select([], version=2, key="calendar-parallel-other-key")

        self.events_provider.after = commit_other_request
        with self.assertRaises(APIError) as conflict:
            self.select(version=2, key="calendar-parallel-first-key")
        self.assertEqual(conflict.exception.code, ErrorCode.VERSION_CONFLICT)
        self.assertEqual(self.records()[0]["version"], 3)
        self.assertEqual(self.records()[0]["allowed_calendar_ids"], [])

    def test_same_key_commit_during_secret_read_avoids_duplicate_provider_send(self) -> None:
        original = self.secrets.get
        fired = False
        key = "calendar-secret-parallel-key"
        before = len(self.events_provider.calls)

        def delayed(reference: str) -> bytes:
            nonlocal fired
            value = original(reference)
            if not fired:
                fired = True
                self.assertEqual(self.select(version=2, key=key).version, 3)
            return value

        with patch.object(self.secrets, "get", side_effect=delayed):
            self.assertEqual(self.select(version=2, key=key).version, 3)
        self.assertEqual(len(self.events_provider.calls), before + 1)

    def test_same_key_commit_between_calendar_probes_stops_remaining_sends(self) -> None:
        calendars = [self.query.calendar_id, "second-calendar"]
        key = "calendar-multiple-parallel-key"
        before = len(self.events_provider.calls)

        def committed() -> None:
            self.events_provider.after = lambda: None
            self.assertEqual(self.select(calendars, version=2, key=key).version, 3)

        self.events_provider.after = committed
        selected = self.select(calendars, version=2, key=key)
        self.assertEqual(selected.version, 3)
        self.assertEqual(selected.allowed_calendar_ids, calendars)
        self.assertEqual(len(self.events_provider.calls), before + 3)

    def test_same_key_commit_then_provider_error_recovers_saved_receipt(self) -> None:
        original = self.events_provider.events
        for version, code in enumerate(
            (ErrorCode.TEMPORARILY_UNAVAILABLE, ErrorCode.AUTHENTICATION_REQUIRED), start=2
        ):
            key = "calendar-provider-failure-" + str(version)
            fired = False

            def failed(
                token: SecretStr,
                query: CalendarQuery,
                page: str | None,
                *,
                version: int = version,
                key: str = key,
                code: ErrorCode = code,
            ) -> dict[str, Any]:
                nonlocal fired
                if not fired:
                    fired = True
                    self.assertEqual(self.select(version=version, key=key).version, version + 1)
                    raise APIError(code)
                return original(token, query, page)

            with patch.object(self.events_provider, "events", side_effect=failed) as send:
                self.assertEqual(self.select(version=version, key=key).version, version + 1)
                self.assertEqual(send.call_count, 2)

    def test_same_key_commit_then_secret_error_recovers_without_new_provider_send(self) -> None:
        original = self.secrets.get
        fired = False
        key = "calendar-secret-failure-key"
        before = len(self.events_provider.calls)

        def failed(reference: str) -> bytes:
            nonlocal fired
            if not fired:
                fired = True
                self.assertEqual(self.select(version=2, key=key).version, 3)
                raise RuntimeError("SYNTHETIC_SECRET_ERROR")
            return original(reference)

        with patch.object(self.secrets, "get", side_effect=failed):
            self.assertEqual(self.select(version=2, key=key).version, 3)
        self.assertEqual(len(self.events_provider.calls), before + 1)

    def test_probe_failure_without_receipt_never_commits_or_retries(self) -> None:
        before = len(self.events_provider.calls)
        original = self.records()[0]
        key = "calendar-uncommitted-failure"
        with patch.object(self.secrets, "get", side_effect=RuntimeError("SYNTHETIC_SECRET")):
            with self.assertRaises(APIError) as failed:
                self.select(version=2, key=key)
            self.assertEqual(failed.exception.code, ErrorCode.TEMPORARILY_UNAVAILABLE)
        self.assertEqual(len(self.events_provider.calls), before)
        with patch.object(
            self.events_provider, "events", side_effect=APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)
        ) as send:
            with self.assertRaises(APIError) as failed:
                self.select(version=2, key=key)
            self.assertEqual(failed.exception.code, ErrorCode.TEMPORARILY_UNAVAILABLE)
            self.assertEqual(send.call_count, 1)
        self.assertEqual(self.records()[0], original)
        self.assertEqual(self.select(version=2, key=key).version, 3)

    def test_different_body_commit_during_secret_read_is_conflict_without_send(self) -> None:
        original = self.secrets.get
        key = "calendar-secret-body-conflict"
        before = len(self.events_provider.calls)

        def changed(reference: str) -> bytes:
            value = original(reference)
            self.assertEqual(self.select([], version=2, key=key).version, 3)
            return value

        with patch.object(self.secrets, "get", side_effect=changed):
            with self.assertRaises(APIError) as conflict:
                self.select(version=2, key=key)
            self.assertEqual(conflict.exception.code, ErrorCode.IDEMPOTENCY_CONFLICT)
        self.assertEqual(len(self.events_provider.calls), before)
        self.assertEqual(self.records()[0]["allowed_calendar_ids"], [])

    def test_saved_receipt_recovery_after_probe_failure_rechecks_revocation(self) -> None:
        key = "calendar-late-revoke-receipt"

        def revoked() -> None:
            self.events_provider.after = lambda: None
            self.select(version=2, key=key)
            self.revoke_member()
            raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)

        self.events_provider.after = revoked
        with self.assertRaises(APIError) as denied:
            self.select(version=2, key=key)
        self.assertEqual(denied.exception.code, ErrorCode.RESOURCE_NOT_FOUND)
        self.assertEqual(self.records()[0]["version"], 3)

    def test_saved_receipt_recovery_after_probe_failure_rechecks_owner(self) -> None:
        key = "calendar-late-owner-receipt"

        def changed() -> None:
            self.events_provider.after = lambda: None
            self.select(version=2, key=key)
            record = self.records()[0]
            self.put(
                f"tenants/{self.tenant}/integrations/{self.integration}",
                {**record, "owner_user_id": "demo-a-owner"},
            )
            raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)

        self.events_provider.after = changed
        with self.assertRaises(APIError) as denied:
            self.select(version=2, key=key)
        self.assertEqual(denied.exception.code, ErrorCode.RESOURCE_NOT_FOUND)
        self.assertEqual(self.records()[0]["version"], 3)

    def test_uncommitted_stamp_change_during_secret_read_blocks_provider(self) -> None:
        original = self.secrets.get
        before = len(self.events_provider.calls)

        def changed(reference: str) -> bytes:
            value = original(reference)
            record = self.records()[0]
            self.put(
                f"tenants/{self.tenant}/integrations/{self.integration}",
                {**record, "version": 3},
            )
            return value

        with patch.object(self.secrets, "get", side_effect=changed):
            with self.assertRaises(APIError) as conflict:
                self.select(version=2, key="calendar-uncommitted-stamp")
            self.assertEqual(conflict.exception.code, ErrorCode.VERSION_CONFLICT)
        self.assertEqual(len(self.events_provider.calls), before)

    def test_selection_receipt_expires_at_24_hours_without_provider_send(self) -> None:
        with patch.object(self.events_provider, "events") as send:
            self.clock.advance(24 * 60 * 60 - 1)
            self.assertEqual(self.select().version, 2)
            self.clock.advance(1)
            with self.assertRaises(APIError) as conflict:
                self.select()
            self.assertEqual(conflict.exception.code, ErrorCode.VERSION_CONFLICT)
            self.assertEqual(send.call_count, 0)

    def test_malformed_endpoints_fail_whole_page_and_selection_with_sanitized_http_503(
        self,
    ) -> None:
        page = self.events_provider.pages[None]
        shapes: list[object] = [[], "SYNTHETIC_PRIVATE_APPOINTMENT", None, 7, True]
        with self.events_client() as client, self.assertLogs("brainoid_api", level="INFO") as logs:
            url = f"/v1/tenants/{self.tenant}/integrations/{self.integration}"
            for field in ("start", "end"):
                for shape in shapes:
                    with self.subTest(field=field, shape=shape):
                        # A valid first event must not escape as a partial success.
                        page["items"] = [event(), event(**{field: shape})]
                        response = client.get(
                            url + "/calendar-events",
                            params=self.query.model_dump(mode="json", exclude_none=True),
                        )
                        self.assertEqual(response.status_code, 503, response.text)
                        self.assertEqual(
                            response.json()["error"]["code"], "temporarily_unavailable"
                        )
                        self.assertNotIn("SYNTHETIC_PRIVATE_APPOINTMENT", response.text)
                        self.assertNotIn("SYNTHETIC_ACCESS", response.text)
                        page["items"] = [event(**{field: shape})]
                        response = client.patch(
                            url,
                            json={"allowed_calendar_ids": [self.query.calendar_id]},
                            headers={
                                "Idempotency-Key": "calendar-malformed-selection",
                                "If-Match": '"2"',
                            },
                        )
                        self.assertEqual(response.status_code, 503, response.text)
                        self.assertEqual(
                            response.json()["error"]["code"], "temporarily_unavailable"
                        )
                        self.assertNotIn("SYNTHETIC_PRIVATE_APPOINTMENT", response.text)
                        self.assertEqual(self.records()[0]["version"], 2)
        self.assertNotIn("SYNTHETIC_PRIVATE_APPOINTMENT", "\n".join(logs.output))
        self.assertNotIn("SYNTHETIC_ACCESS", "\n".join(logs.output))

    def test_empty_and_hidden_selection_responses_are_valid(self) -> None:
        page = self.events_provider.pages[None]
        page["items"] = []
        self.assertEqual(self.select(version=2, key="calendar-empty-provider").version, 3)
        hidden = event()
        hidden.pop("summary")
        page["items"] = [hidden]
        self.assertEqual(self.select(version=3, key="calendar-hidden-provider").version, 4)
        self.assertEqual(self.read().items[0].summary, "予定")

    def test_secret_io_revocation_blocks_provider_and_multi_selection_stops(self) -> None:
        before = len(self.events_provider.calls)
        get = self.secrets.get

        def delayed(reference: str) -> bytes:
            value = get(reference)
            self.revoke_member()
            return value

        with patch.object(self.secrets, "get", side_effect=delayed), self.assertRaises(APIError):
            self.read()
        self.assertEqual(len(self.events_provider.calls), before)

    def test_page_expiry_budget_and_period_limits(self) -> None:
        self.events_provider.pages[None]["nextPageToken"] = "next"
        first = self.read()
        self.clock.advance(300)
        with self.assertRaises(APIError):
            self.read(self.query.model_copy(update={"cursor": first.next_cursor}))
        for max_time in (self.query.time_min, self.query.time_min + timedelta(days=31, seconds=1)):
            with self.assertRaises(ValueError):
                CalendarQuery(
                    calendar_id=self.query.calendar_id,
                    time_min=self.query.time_min,
                    time_max=max_time,
                )
        for number in range(1, 11):
            self.events_provider.pages[str(number)] = {
                "timeZone": "UTC",
                "items": [],
                "nextPageToken": str(number + 1),
            }
        self.events_provider.pages[None] = {"timeZone": "UTC", "items": [], "nextPageToken": "1"}
        query = self.query
        for _ in range(9):
            query = query.model_copy(update={"cursor": self.read(query).next_cursor})
        with self.assertRaises(APIError):
            self.read(query)

    def test_legacy_grant_requires_reauthorization_and_late_selection_revocation_stops(
        self,
    ) -> None:
        record = self.records()[0]
        location = f"tenants/{self.tenant}/calendar_authorizations/{record['authorization_id']}"
        authorization = self.get(location)
        legacy = {key: value for key, value in authorization.items() if key != "granted_scopes"}
        self.put(location, legacy)
        before = len(self.events_provider.calls)
        with self.assertRaises(APIError):
            self.read()
        self.assertEqual(len(self.events_provider.calls), before)
        self.put(location, authorization)
        self.events_provider.after = self.revoke_member
        with self.assertRaises(APIError):
            self.select(["first", "second"], version=2, key="calendar-multi-revocation")
        self.assertEqual(len(self.events_provider.calls), before + 1)
