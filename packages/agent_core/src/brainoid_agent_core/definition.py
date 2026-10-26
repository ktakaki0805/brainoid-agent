"""Versioned personality data, never an execution permission grant."""

from typing import Annotated

from brainoid_contracts import ResourceId, UtcTimestamp, Version
from pydantic import Field

from brainoid_agent_core.types.common import CoreValue


class AgentDefinition(CoreValue):
    id: ResourceId
    tenant_id: ResourceId
    version: Version
    created_at: UtcTimestamp
    updated_at: UtcTimestamp
    name: Annotated[str, Field(strict=True, min_length=1, max_length=200)]
    persona: Annotated[str, Field(strict=True, max_length=4000, repr=False)]
    allowed_tools: Annotated[
        tuple[Annotated[str, Field(strict=True, max_length=80)], ...], Field(max_length=30)
    ]
