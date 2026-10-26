"""Note specialization of the shared run-bound direct-resource boundary."""

from typing import TYPE_CHECKING

from brainoid_agent_core.ports import CallControl
from brainoid_memory.notes import Note, NoteCreate, NotePatch, Notes, view
from brainoid_tools.notes import NoteArguments
from brainoid_tools.registry import Invocation
from brainoid_tools.results import HandlerResult

from brainoid_worker.direct_resources import DirectRuntime

if TYPE_CHECKING:
    from brainoid_worker.memory import MemoryRuntime


class NotesRuntime(DirectRuntime[NoteCreate, NotePatch, Note]):
    UPDATE_ACK = "ノートを更新しました。"

    def __init__(self, memory: "MemoryRuntime") -> None:
        super().__init__(
            memory,
            kind="note",
            service=Notes(memory.handler.jobs.store, now=memory.handler.jobs.now),
            view=view,
        )

    async def execute_notes(
        self,
        name: str,
        invocation: Invocation[NoteArguments],
        *,
        control: CallControl,
        reconcile: bool = False,
    ) -> HandlerResult:
        return await self.execute_direct(name, invocation, control=control, reconcile=reconcile)
