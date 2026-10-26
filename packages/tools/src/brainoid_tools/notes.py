"""Typed note arguments and registration; persistence belongs to the shared service."""

from typing import Annotated, Protocol

from brainoid_agent_core.ports import CallControl
from brainoid_contracts import DisplayPolicy, ResourceId, Version, Visibility
from brainoid_memory.notes import NoteCreate, NotePatch
from pydantic import Field

from brainoid_tools.registry import Arguments, Invocation, Registration, Registry
from brainoid_tools.results import HandlerResult


class CreateNoteArguments(NoteCreate, Arguments):
    """Requires direct user text: Save {visibility} note {title}: {body}.

    Scope must equal the conversation scope. Copy title/body exactly; mismatches
    are denied. Ask the user to state the intended contents and scope explicitly.
    """

    visibility: Visibility = Field(strict=False)
    display_policy: DisplayPolicy = Field(strict=False)


class ListNotesArguments(Arguments):
    query: Annotated[str, Field(max_length=2000)] = ""
    limit: Annotated[int, Field(ge=1, le=20)] = 10


class UpdateNoteArguments(Arguments):
    """Requires: Update note {note_id}: {changes JSON, sorted keys, Unicode text}."""

    note_id: ResourceId
    expected_version: Version
    changes: NotePatch


class DeleteNoteArguments(Arguments):
    """Requires direct user text: Delete note {note_id}."""

    note_id: ResourceId
    expected_version: Version


type NoteArguments = (
    CreateNoteArguments | ListNotesArguments | UpdateNoteArguments | DeleteNoteArguments
)


class NotesBackend(Protocol):
    async def execute_notes(
        self,
        name: str,
        invocation: Invocation[NoteArguments],
        *,
        control: CallControl,
        reconcile: bool = False,
    ) -> HandlerResult: ...


class NotesHandler:
    def __init__(self, name: str, backend: NotesBackend) -> None:
        self.name, self.backend = name, backend

    async def execute(
        self, invocation: Invocation[NoteArguments], *, control: CallControl
    ) -> HandlerResult:
        return await self.backend.execute_notes(self.name, invocation, control=control)

    async def reconcile(
        self, invocation: Invocation[NoteArguments], *, control: CallControl
    ) -> HandlerResult:
        return await self.backend.execute_notes(
            self.name, invocation, control=control, reconcile=True
        )


def register_notes(registry: Registry, backend: NotesBackend) -> None:
    # Explicitly injected business adapters retain ownership (e.g. confirmation
    # integration fixtures). Default production composition uses these handlers.
    for name, arguments in (
        ("notes.create", CreateNoteArguments),
        ("notes.list", ListNotesArguments),
        ("notes.update", UpdateNoteArguments),
        ("notes.delete", DeleteNoteArguments),
    ):
        if not registry.definitions((name,)):
            registry.register(Registration(name, arguments, NotesHandler(name, backend)))
