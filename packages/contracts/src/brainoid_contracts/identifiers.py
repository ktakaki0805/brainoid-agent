"""Nominal ID types with the same validation in constructors and Pydantic models."""

import re
from typing import ClassVar, Self
from uuid import UUID

from pydantic import GetCoreSchemaHandler, GetJsonSchemaHandler
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema, core_schema

UUID_PATTERN = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"


class _StringValue(str):
    """Strict strings; never trim, decode or coerce opaque identifiers."""

    min_length: ClassVar[int] = 1
    max_length: ClassVar[int | None] = None
    json_format: ClassVar[str | None] = None
    pattern: ClassVar[str | None] = None

    def __new__(cls, value: str) -> Self:
        if not isinstance(value, str):
            raise ValueError(f"{cls.__name__} requires a string")
        if len(value) < cls.min_length or (
            cls.max_length is not None and len(value) > cls.max_length
        ):
            raise ValueError(f"{cls.__name__} length is invalid")
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: object, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls, core_schema.str_schema(strict=True)
        )

    @classmethod
    def __get_pydantic_json_schema__(
        cls, schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        result = handler(schema)
        result["minLength"] = cls.min_length
        if cls.max_length is not None:
            result["maxLength"] = cls.max_length
        if cls.json_format is not None:
            result["format"] = cls.json_format
        if cls.pattern is not None:
            result["pattern"] = cls.pattern
        return result


class ResourceId(_StringValue):
    """Server UUID in hyphenated form, normalized to lowercase; no UUID version restriction."""

    min_length = 36
    max_length = 36
    json_format = "uuid"
    pattern = UUID_PATTERN

    def __new__(cls, value: str) -> Self:
        checked = super().__new__(cls, value)
        if re.fullmatch(UUID_PATTERN, checked) is None:
            raise ValueError("ResourceId requires a hyphenated UUID")
        return super().__new__(cls, str(UUID(checked)))


class FirebaseUid(_StringValue):
    """Firebase UID: 1–128 characters, preserved verbatim, with no UUID requirement."""

    max_length = 128


class ProviderId(_StringValue):
    """Opaque provider identifier, e.g. a Calendar event or calendar ID."""

    max_length = 1024
