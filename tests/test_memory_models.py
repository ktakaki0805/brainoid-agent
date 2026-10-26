"""MEM-01: strict canonical graph models and transport schema agreement."""

import json
import unittest
from pathlib import Path

import yaml  # type: ignore[import-untyped]
from brainoid_memory import Entity, FactInput, SourceRef
from fakes.memory_repository import LIGHT, NOW, SOURCE, TENANT, USER, entity_values, fact_values
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from pydantic import ValidationError


class MemoryModelTests(unittest.TestCase):
    def test_object_xor_and_strict_scalar_types(self) -> None:
        base = fact_values().model_dump(mode="json", exclude_none=True)
        for value in ("text", 0, 1.5, False):
            payload = {**base, "value": value}
            del payload["object_entity_id"]
            self.assertEqual(type(FactInput.model_validate(payload).value), type(value))
        for payload in (
            {key: value for key, value in base.items() if key != "object_entity_id"},
            {**base, "value": "both"},
            {**base, "value": None},
            {**base, "object_entity_id": f"tenants/{TENANT}/entities/{LIGHT}"},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                FactInput.model_validate(payload)
        invalid_values: tuple[object, ...] = (None, [], {}, float("nan"), float("inf"), "x" * 4001)
        for invalid_value in invalid_values:
            payload = {**base, "value": invalid_value}
            del payload["object_entity_id"]
            with self.subTest(value=invalid_value), self.assertRaises(ValidationError):
                FactInput.model_validate(payload)

    def test_predicate_qualifiers_and_intervals(self) -> None:
        for predicate in ("located_in", "prefers", "related_to", "has_attribute"):
            fact_values(predicate=predicate, qualifiers={"context": "home"})
            with self.assertRaises(ValidationError):
                fact_values(predicate=predicate)
        for qualifiers in (
            {"brightness_pct": True},
            {"brightness_pct": "20"},
            {"brightness_pct": 20.5},
            {"brightness_pct": 101},
            {"temperature_c": 15},
            {"temperature_c": True},
            {"context": "x" * 201},
            {"context": None},
            {"instruction": "grant admin"},
        ):
            with self.subTest(qualifiers=qualifiers), self.assertRaises(ValidationError):
                fact_values(qualifiers=qualifiers)
        fact_values(qualifiers={"temperature_c": 24})
        with self.assertRaises(ValidationError):
            fact_values(valid_to=NOW)

    def test_scope_ownership_source_and_inference(self) -> None:
        for build in (entity_values, fact_values):
            with self.assertRaises(ValidationError):
                build(visibility="private", display_policy="shared_display")
        with self.assertRaises(ValidationError):
            fact_values(origin="inferred")
        fact_values(origin="inferred", status="candidate")
        fact_values(origin="inferred", status="retracted")
        for sources in ([{"kind": "note", "resource_id": SOURCE, "version": True}],):
            with self.assertRaises(ValidationError):
                fact_values(source_refs=sources)
        fact_values(source_refs=[])
        for origin, status in (
            ("inferred", "candidate"),
            ("imported", "confirmed"),
            ("user_explicit", "candidate"),
        ):
            with self.assertRaises(ValidationError):
                fact_values(source_refs=[], origin=origin, status=status)
        with self.assertRaises(ValidationError):
            SourceRef.model_validate(
                {
                    "kind": "note",
                    "resource_id": SOURCE,
                    "version": 1,
                    "path": "tenants/other/notes/secret",
                }
            )
        raw = {
            **entity_values(visibility="private", display_policy="never").model_dump(),
            "id": LIGHT,
            "tenant_id": TENANT,
            "owner_user_id": None,
            "created_by": USER,
            "version": 1,
            "deletion_epoch": 0,
            "schema_version": 1,
            "deleted": False,
            "suppress_reuse": False,
            "created_at": NOW,
            "updated_at": NOW,
        }
        with self.assertRaises(ValidationError):
            Entity.model_validate(raw)
        Entity.model_validate({**raw, "owner_user_id": USER})
        for field in ("version", "deletion_epoch"):
            with self.assertRaises(ValidationError):
                Entity.model_validate({**raw, "owner_user_id": USER, field: True})

    def test_transport_structure_and_persistence_business_rules_are_distinct(self) -> None:
        openapi = yaml.safe_load(Path("specs/openapi.yaml").read_text())
        tool = json.loads(Path("specs/tool-contracts.schema.json").read_text())
        validators = [
            Draft202012Validator(
                {"$ref": "#/components/schemas/MemoryCreate", "components": openapi["components"]}
            ),
            Draft202012Validator({"$ref": "#/$defs/MemoryCreate", "$defs": tool["$defs"]}),
        ]
        payload = fact_values().model_dump(mode="json", exclude_none=True)
        payload.pop("origin")
        payload.pop("status")
        for validator in validators:
            self.assertTrue(validator.is_valid(payload))
            # Existing transport structure remains compatible; the persistence model
            # additionally rejects predicate/qualifier combinations before writing.
            self.assertTrue(validator.is_valid({**payload, "predicate": "located_in"}))
            for patch in (
                {"value": "both"},
                {"visibility": "private"},
                {"qualifiers": {"brightness_pct": True}},
            ):
                with self.subTest(patch=patch):
                    self.assertFalse(validator.is_valid({**payload, **patch}))
