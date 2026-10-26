"""Typed memory tools; all persistence is delegated to the common mutation service."""

from typing import Annotated, Protocol

from brainoid_agent_core.ports import CallControl
from brainoid_contracts import DisplayPolicy, ResourceId, Version, Visibility
from brainoid_memory.dto import MemoryCreate
from brainoid_memory.models import SourceRef
from brainoid_memory.retrieval import SearchInput
from pydantic import Field

from brainoid_tools.registry import Arguments, Invocation, Registration, Registry
from brainoid_tools.results import HandlerResult


class SearchArguments(SearchInput, Arguments):
    pass


class RememberArguments(MemoryCreate, Arguments):
    # JSON strings/arrays are the canonical wire representations of enums/tuples.
    # All scalar fields still enforce strict types and the registry checks JSON Schema.
    visibility: Visibility = Field(strict=False)
    display_policy: DisplayPolicy = Field(strict=False)
    source_refs: Annotated[tuple[SourceRef, ...], Field(strict=False, max_length=20)] = ()


class ReviseArguments(Arguments):
    memory_id: ResourceId
    expected_version: Version
    replacement: MemoryCreate


type MemoryArguments = SearchArguments | RememberArguments | ReviseArguments


class MemoryBackend(Protocol):
    async def execute_memory(
        self,
        name: str,
        invocation: Invocation[MemoryArguments],
        *,
        control: CallControl,
        reconcile: bool = False,
    ) -> HandlerResult: ...


class MemoryHandler:
    def __init__(self, name: str, backend: MemoryBackend) -> None:
        self.name, self.backend = name, backend

    async def execute(
        self, invocation: Invocation[MemoryArguments], *, control: CallControl
    ) -> HandlerResult:
        return await self.backend.execute_memory(self.name, invocation, control=control)

    async def reconcile(
        self, invocation: Invocation[MemoryArguments], *, control: CallControl
    ) -> HandlerResult:
        return await self.backend.execute_memory(
            self.name, invocation, control=control, reconcile=True
        )


def register_memory(registry: Registry, backend: MemoryBackend) -> None:
    registry.register(
        Registration("memory.search", SearchArguments, MemoryHandler("memory.search", backend))
    )
    registry.register(
        Registration(
            "memory.remember", RememberArguments, MemoryHandler("memory.remember", backend)
        )
    )
    registry.register(
        Registration("memory.revise", ReviseArguments, MemoryHandler("memory.revise", backend))
    )
