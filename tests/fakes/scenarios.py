"""Named synthetic failures; out-of-scope records deliberately emulate a faulty store."""

from datetime import UTC, datetime

from brainoid_agent_core.types import (
    AudioFormat,
    MemoryContent,
    MemoryMatches,
    MemoryRecord,
    ModelMessage,
    ModelRequest,
    SourceRef,
    TextPart,
    ToolCall,
    ToolExecution,
    ToolResult,
)
from brainoid_contracts import (
    DisplayPolicy,
    FirebaseUid,
    ResourceId,
    Scope,
    TenantContext,
    UserActorContext,
    Visibility,
)

TENANT_A = ResourceId("51cea16f-85c3-5b21-a3bf-5fe62e091124")
TENANT_B = ResourceId("22222222-2222-4222-8222-222222222222")
MEMORY_ID = ResourceId("33333333-3333-4333-8333-333333333333")
SUBJECT_ID = ResourceId("44444444-4444-4444-8444-444444444444")
SOURCE_ID = ResourceId("55555555-5555-4555-8555-555555555555")
TOOL_CALL_ID = ResourceId("66666666-6666-4666-8666-666666666666")
OPERATION_ID = ResourceId("77777777-7777-4777-8777-777777777777")
FIXED_TIME = datetime(2026, 10, 26, tzinfo=UTC)
SHARED_SCOPE = Scope(visibility=Visibility.SHARED, display_policy=DisplayPolicy.SHARED_DISPLAY)
PRIVATE_SCOPE = Scope(
    visibility=Visibility.PRIVATE, display_policy=DisplayPolicy.AUTHENTICATED_SESSION
)
CONTEXT = TenantContext(
    tenant_id=TENANT_A,
    actor=UserActorContext(
        actor_id=FirebaseUid("demo-a-member"),
        allowed_scopes=frozenset({"memory.read", "memory.write", "conversation"}),
        authorization_version=1,
    ),
)
MODEL_REQUEST = ModelRequest(
    messages=(ModelMessage(role="user", parts=(TextPart(text="FIXTURE_GREETING"),)),)
)
TOOL_CALL = ToolCall(
    call_key="fixture-call-1", name="memory.search", arguments_json='{"query":"FIXTURE"}'
)
MALFORMED_TOOL_CALL = ToolCall(
    call_key="fixture-malformed", name="memory.search", arguments_json='{"query":'
)
FORBIDDEN_TOOL_CALL = ToolCall(
    call_key="fixture-forbidden", name="calendar.confirm", arguments_json='{"approved":true}'
)
TOOL_EXECUTION = ToolExecution(
    context=CONTEXT,
    tool_call_id=TOOL_CALL_ID,
    idempotency_key="fixture-idempotency-1",
    call=TOOL_CALL,
)
OUTCOME_UNKNOWN = ToolResult(
    tool_call_id=TOOL_CALL_ID,
    status="outcome_unknown",
    result={"reason": "FIXTURE_RESPONSE_LOST_AFTER_EFFECT"},
    operation_id=OPERATION_ID,
)
MEMORY_CONTENT = MemoryContent(
    subject_entity_id=SUBJECT_ID,
    predicate="prefers",
    value="FIXTURE_SHARED_FACT",
    scope=SHARED_SCOPE,
    source_refs=(SourceRef(kind="message", resource_id=SOURCE_ID, version=1),),
    valid_from=FIXED_TIME,
)
MEMORY_RECORD = MemoryRecord(
    memory_id=MEMORY_ID,
    tenant_id=TENANT_A,
    content=MEMORY_CONTENT,
    version=1,
    recorded_at=FIXED_TIME,
    status="confirmed",
)
# Explicit fault injection: this is not the expected behavior of a real MemoryStore.
OUT_OF_SCOPE_CANDIDATES = MemoryMatches(
    records=(
        MEMORY_RECORD,
        MemoryRecord(
            memory_id=ResourceId("88888888-8888-4888-8888-888888888888"),
            tenant_id=TENANT_B,
            owner_user_id=FirebaseUid("demo-b-owner"),
            content=MEMORY_CONTENT.model_copy(
                update={"value": "FIXTURE_FORBIDDEN_TENANT_B_PRIVATE", "scope": PRIVATE_SCOPE}
            ),
            version=2,
            recorded_at=FIXED_TIME,
            status="confirmed",
        ),
    )
)
AUDIO_FORMAT = AudioFormat(encoding="pcm_s16le", sample_rate_hz=16000)
