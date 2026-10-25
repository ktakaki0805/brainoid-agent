"""Boundary cases for shared wire/storage contracts; no Firebase or provider calls."""

import itertools
import unittest
from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from brainoid_contracts import (
    DeviceActorContext,
    DisplayPolicy,
    FirebaseUid,
    IanaTimezone,
    ProviderId,
    ResourceId,
    Scope,
    TenantContext,
    UserActorContext,
    UtcTimestamp,
    Version,
    Visibility,
    WorkerActorContext,
    format_utc,
    normalize_utc,
)
from pydantic import BaseModel, TypeAdapter, ValidationError

RESOURCE_ID = "fcae923b-e348-4366-943c-f2634ad6c09b"
TENANT_ID = "5b817fe9-a7e4-4df9-bf78-0b5a4d0f8759"


class StoredResource(BaseModel):
    id: ResourceId
    user_id: FirebaseUid
    provider_id: ProviderId
    recorded_at: UtcTimestamp
    timezone: IanaTimezone
    version: Version
    scope: Scope


class IdentifierTests(unittest.TestCase):
    def test_resource_ids_require_hyphenated_uuid_and_normalize_case(self) -> None:
        self.assertEqual(ResourceId(RESOURCE_ID.upper()), RESOURCE_ID)
        self.assertEqual(
            ResourceId("00000000-0000-0000-0000-000000000000"),
            "0" * 8 + "-" + "0" * 4 + "-" + "0" * 4 + "-" + "0" * 4 + "-" + "0" * 12,
        )
        for value in [
            "",
            "firebase-user-example",
            RESOURCE_ID.replace("-", ""),
            "{" + RESOURCE_ID + "}",
            "urn:uuid:" + RESOURCE_ID,
            RESOURCE_ID[:-1] + "g",
            " " + RESOURCE_ID,
            1,
            b"uuid",
            None,
        ]:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                TypeAdapter(ResourceId).validate_python(value)

    def test_opaque_ids_preserve_content_and_have_distinct_types(self) -> None:
        for value in ["firebase-user-example", "a", "x" * 128, "ID/With:Case", " ユーザー "]:
            with self.subTest(value=value):
                self.assertIs(type(TypeAdapter(FirebaseUid).validate_python(value)), FirebaseUid)
                self.assertEqual(FirebaseUid(value), value)
        for value in ["calendar@example.test", "event/with%2Fopaque+data", "x" * 1024]:
            with self.subTest(value=value):
                self.assertIs(type(TypeAdapter(ProviderId).validate_python(value)), ProviderId)
                self.assertEqual(ProviderId(value), value)
        self.assertIsNot(FirebaseUid, ProviderId)
        self.assertIsNot(FirebaseUid, ResourceId)

    def test_opaque_id_boundaries(self) -> None:
        for kind, invalid in [
            (FirebaseUid, ["", "x" * 129, 1, b"uid", None]),
            (ProviderId, ["", "x" * 1025, 1, None]),
        ]:
            for value in invalid:
                with self.subTest(kind=kind, value=value), self.assertRaises(ValidationError):
                    TypeAdapter(kind).validate_python(value)
        with self.assertRaises(ValueError):
            FirebaseUid("")
        with self.assertRaises(ValueError):
            ResourceId("not-a-uuid")


class TimeTests(unittest.TestCase):
    def test_offsets_cross_day_month_and_year_boundaries(self) -> None:
        cases = {
            "2026-01-01T00:30:00+09:00": "2025-12-31T15:30:00Z",
            "2024-03-01T00:15:00+01:00": "2024-02-29T23:15:00Z",
            "2026-12-31T23:30:00-03:30": "2027-01-01T03:00:00Z",
            "2026-10-26t12:00:00.123456z": "2026-10-26T12:00:00.123456Z",
            "2026-10-26T12:00:00+00:00": "2026-10-26T12:00:00Z",
            "0001-01-01T00:00:00Z": "0001-01-01T00:00:00Z",
            "9999-12-31T23:59:59.999999Z": "9999-12-31T23:59:59.999999Z",
        }
        adapter = TypeAdapter(UtcTimestamp)
        for source, expected in cases.items():
            with self.subTest(source=source):
                parsed = adapter.validate_json('"' + source + '"')
                self.assertIs(parsed.tzinfo, UTC)
                self.assertEqual(format_utc(parsed), expected)
                self.assertEqual(adapter.dump_json(parsed), ('"' + expected + '"').encode())
                self.assertEqual(normalize_utc(parsed), parsed)

    def test_rejects_missing_unknown_invalid_offsets_and_lossy_dates(self) -> None:
        for value in [
            "2026-10-26T12:00:00",
            "2026-10-26",
            "2026-10-26 12:00:00Z",
            "2026-10-26T12:00Z",
            "2026-10-26T12:00:00+0900",
            "2026-10-26T12:00:00-00:00",
            "2026-10-26T12:00:00+24:00",
            "2026-10-26T12:00:00+01:60",
            "2026-10-26T24:00:00Z",
            "2026-02-29T12:00:00Z",
            "2026-12-31T23:59:60Z",
            "2026-10-26T12:00:00.1234567Z",
            "2026-10-26T12:00:00,1Z",
            "0001-01-01T00:00:00+00:01",
            "9999-12-31T23:59:59-00:01",
            0,
            1792977600,
            True,
            None,
            datetime(2026, 10, 26),
        ]:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                TypeAdapter(UtcTimestamp).validate_python(value)

    def test_aware_datetimes_and_dst_folds_remain_distinct_instants(self) -> None:
        local = datetime(2026, 1, 1, 0, 30, tzinfo=timezone(timedelta(hours=9)))
        self.assertEqual(format_utc(local), "2025-12-31T15:30:00Z")
        earlier = datetime(2026, 11, 1, 1, 30, tzinfo=ZoneInfo("America/New_York"), fold=0)
        later = earlier.replace(fold=1)
        self.assertEqual(normalize_utc(later) - normalize_utc(earlier), timedelta(hours=1))

    def test_iana_names_are_checked_without_accepting_host_files(self) -> None:
        for valid in ["Asia/Tokyo", "America/New_York", "UTC", "Etc/GMT+9"]:
            with self.subTest(value=valid):
                self.assertEqual(TypeAdapter(IanaTimezone).validate_python(valid), valid)
        for value in [
            "",
            "Asia/Not_A_Zone",
            "asia/tokyo",
            "+09:00",
            "../UTC",
            "/etc/localtime",
            "localtime",
            "posix/Asia/Tokyo",
            "right/UTC",
            "x" * 81,
            9,
            None,
        ]:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                TypeAdapter(IanaTimezone).validate_python(value)


class ScopeAndVersionTests(unittest.TestCase):
    def test_version_requires_positive_integer_without_coercion(self) -> None:
        adapter = TypeAdapter(Version)
        for valid in [1, 2, 2**63]:
            self.assertEqual(adapter.validate_python(valid), valid)
        for value in [0, -1, True, False, "1", 1.0, None]:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                adapter.validate_python(value)

    def test_every_visibility_display_combination(self) -> None:
        for visibility, policy in itertools.product(Visibility, DisplayPolicy):
            value = {"visibility": visibility.value, "display_policy": policy.value}
            with self.subTest(value=value):
                if visibility == Visibility.PRIVATE and policy == DisplayPolicy.SHARED_DISPLAY:
                    with self.assertRaises(ValidationError):
                        Scope.model_validate(value)
                else:
                    scope = Scope.model_validate(value)
                    self.assertEqual(scope.model_dump(mode="json"), value)
                    with self.assertRaises(ValidationError):
                        scope.display_policy = DisplayPolicy.SHARED_DISPLAY

    def test_incomplete_or_unknown_scope_is_rejected(self) -> None:
        for value in [
            {},
            {"visibility": "private"},
            {"visibility": "public", "display_policy": "never"},
            {"visibility": "shared", "display_policy": "always"},
            {"visibility": "shared", "display_policy": "never", "admin": True},
        ]:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                Scope.model_validate(value)


class ContextAndSerializationTests(unittest.TestCase):
    def context_input(self, actor_type: str, actor_id: str) -> dict[str, object]:
        return {
            "tenant_id": TENANT_ID,
            "actor": {
                "actor_type": actor_type,
                "actor_id": actor_id,
                "authorization_version": 1,
                "allowed_scopes": ["memory.read", "conversation"],
            },
        }

    def test_actor_discriminator_selects_the_right_id_type(self) -> None:
        for actor_type, actor_id, expected in [
            ("user", "firebase-user-example", UserActorContext),
            ("device", RESOURCE_ID, DeviceActorContext),
            ("worker", "worker@example.iam.test", WorkerActorContext),
        ]:
            with self.subTest(actor_type=actor_type):
                context = TenantContext.model_validate(self.context_input(actor_type, actor_id))
                self.assertIsInstance(context.actor, expected)
                self.assertEqual(context.actor.actor_id, actor_id)
                self.assertIsInstance(context.tenant_id, ResourceId)
                self.assertIsInstance(context.actor.allowed_scopes, frozenset)
                with self.assertRaises(ValidationError):
                    context.tenant_id = ResourceId(RESOURCE_ID)
                with self.assertRaises(ValidationError):
                    context.actor.authorization_version = 2

    def test_context_rejects_malformed_bindings_and_inactive_status(self) -> None:
        invalid = [
            self.context_input("device", "firebase-user-example"),
            self.context_input("unknown", RESOURCE_ID),
        ]
        invalid.append({"tenant_id": "not-a-uuid", "actor": {"actor_type": "user"}})
        base = {
            "actor_type": "user",
            "actor_id": "firebase-user-example",
            "authorization_version": 1,
            "allowed_scopes": [],
        }
        for field, value in [
            ("membership_status", "revoked"),
            ("authorization_version", 0),
            ("allowed_scopes", [""]),
            ("private_session_id", RESOURCE_ID),
        ]:
            invalid.append({"tenant_id": TENANT_ID, "actor": {**base, field: value}})
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                TenantContext.model_validate(value)

    def test_device_session_keeps_resolved_user_and_expiry(self) -> None:
        context = TenantContext.model_validate(
            {
                "tenant_id": TENANT_ID,
                "actor": {
                    "actor_type": "device",
                    "actor_id": RESOURCE_ID,
                    "authorization_version": 1,
                    "allowed_scopes": ["memory.read"],
                    "private_session": {
                        "private_session_id": RESOURCE_ID,
                        "user_id": "firebase-user-example",
                        "expires_at": "2026-10-26T09:05:00+09:00",
                        "idle_expires_at": "2026-10-26T09:01:00+09:00",
                    },
                },
            }
        )
        self.assertIsInstance(context.actor, DeviceActorContext)
        assert isinstance(context.actor, DeviceActorContext)
        assert context.actor.private_session is not None
        self.assertEqual(context.actor.private_session.user_id, "firebase-user-example")
        self.assertEqual(
            format_utc(context.actor.private_session.expires_at), "2026-10-26T00:05:00Z"
        )

    def test_models_round_trip_normalized_wire_values_and_publish_schema(self) -> None:
        resource = StoredResource.model_validate(
            {
                "id": RESOURCE_ID.upper(),
                "user_id": "firebase-user-example",
                "provider_id": "calendar@example.test",
                "recorded_at": "2026-01-01T00:30:00+09:00",
                "timezone": "Asia/Tokyo",
                "version": 1,
                "scope": {"visibility": "private", "display_policy": "authenticated_session"},
            }
        )
        dumped = resource.model_dump(mode="json")
        self.assertEqual(dumped["id"], RESOURCE_ID)
        self.assertEqual(dumped["recorded_at"], "2025-12-31T15:30:00Z")
        self.assertEqual(StoredResource.model_validate_json(resource.model_dump_json()), resource)
        properties = StoredResource.model_json_schema()["properties"]
        self.assertEqual(properties["id"]["format"], "uuid")
        self.assertNotIn("format", properties["user_id"])
        self.assertEqual(properties["user_id"]["maxLength"], 128)
        self.assertEqual(properties["provider_id"]["maxLength"], 1024)
        self.assertEqual(properties["recorded_at"]["format"], "date-time")
        self.assertEqual(properties["timezone"]["format"], "iana-timezone")
        self.assertEqual(properties["version"]["minimum"], 1)
        self.assertIn("allOf", Scope.model_json_schema())


if __name__ == "__main__":
    unittest.main()
