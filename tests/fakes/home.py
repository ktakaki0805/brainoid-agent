"""Synthetic canonical HA inventory; never connects to a provider or device."""

from collections.abc import AsyncGenerator
from datetime import datetime
from uuid import uuid4

from brainoid_agent_core.ports import CallControl
from brainoid_agent_core.types import (
    Completed,
    ModelEvent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextDelta,
    TextPart,
    ToolCall,
    ToolRequested,
)
from brainoid_contracts import FirebaseUid, ResourceId
from brainoid_contracts.transactions import Document
from brainoid_memory.commands import CommandCreate, HomeTarget
from brainoid_memory.entity_resolver import external_key
from brainoid_memory.models import Entity, ExternalRef


def home_fixture(
    tenant: ResourceId,
    user: FirebaseUid,
    now: datetime,
    *,
    private: bool = False,
    display: str | None = None,
    device: ResourceId | None = None,
) -> tuple[CommandCreate, dict[str, Document]]:
    entity, integration = ResourceId(str(uuid4())), ResourceId(str(uuid4()))
    device = device or ResourceId(str(uuid4()))
    reference = ExternalRef(
        provider="home_assistant", integration_id=integration, external_id="light.fixture"
    )
    record = Entity.model_validate(
        {
            "id": entity,
            "tenant_id": tenant,
            "owner_user_id": user if private else None,
            "created_by": user,
            "version": 1,
            "deletion_epoch": 0,
            "schema_version": 1,
            "created_at": now,
            "updated_at": now,
            "deleted": False,
            "suppress_reuse": False,
            "kind": "appliance",
            "label": "照明",
            "aliases": [],
            "external_refs": [reference.model_dump()],
            "visibility": "private" if private else "shared",
            "display_policy": display or ("never" if private else "shared_display"),
        }
    )
    key = external_key(tenant, reference)
    target = HomeTarget(
        tenant_id=tenant,
        device_id=device,
        entity_id=entity,
        reference=reference,
        version=1,
        status="active",
        allowed_actions=("light.turn_on", "light.turn_off"),
    )
    documents = {
        f"tenants/{tenant}/entities/{entity}": record.model_dump(mode="json"),
        f"tenants/{tenant}/entity_external_ids/{key}": {
            "entity_id": entity,
            "deletion_epoch": 0,
            "reference": reference.model_dump(mode="json"),
        },
        f"tenants/{tenant}/integrations/{integration}": {
            "id": integration,
            "tenant_id": tenant,
            "provider": "home_assistant",
            "status": "active",
            "device_id": device,
            "owner_user_id": user if private else None,
            "visibility": "private" if private else "shared",
        },
        f"tenants/{tenant}/home_targets/{key}": target.model_dump(mode="json"),
        f"tenants/{tenant}/devices/{device}": {
            "id": device,
            "tenant_id": tenant,
            "status": "active",
            "authorization_version": 1,
            "connection_generation": 1,
            "allowed_scopes": ["shared", "home.control"],
            "allowed_entity_ids": [entity],
            "allowed_room_ids": [],
            "capabilities": ["display", "audio_output"],
        },
    }
    body = CommandCreate.model_validate(
        {
            "device_id": device,
            "target_entity_id": entity,
            "operation": {"action": "light.turn_on", "parameters": {"brightness_pct": 40}},
        }
    )
    return body, documents


class HomePreambleModel:
    """Multiple real model steps with untrusted, deliberately false appliance prose."""

    def __init__(self, calls: tuple[ToolCall, ...]) -> None:
        self.calls = calls
        self.requests: list[ModelRequest] = []
        self.preamble = "UNOBSERVED_PHYSICAL_SUCCESS " * 80
        self.final_text = "普通の会話です。"

    async def stream(
        self, request: ModelRequest, *, control: CallControl
    ) -> AsyncGenerator[ModelEvent]:
        self.requests.append(request)
        step = sum(m.role == "tool" for m in request.messages)
        calls = () if step >= len(self.calls) else (self.calls[step],)
        text = self.preamble if calls else self.final_text
        yield TextDelta(text=text)
        for call in calls:
            yield ToolRequested(call=call)
        yield ModelResponse(
            message=ModelMessage(role="assistant", parts=(TextPart(text=text), *calls))
        )
        yield Completed(finish_reason="tool_calls" if calls else "stop")
