"""Reminder specialization of the shared run-bound direct-resource boundary."""

from typing import TYPE_CHECKING

from brainoid_agent_core.ports import CallControl
from brainoid_memory.reminders import Reminder, ReminderCreate, ReminderPatch, Reminders, view
from brainoid_tools.registry import Invocation
from brainoid_tools.reminders import ReminderArguments
from brainoid_tools.results import HandlerResult

from brainoid_worker.direct_resources import DirectRuntime

if TYPE_CHECKING:
    from brainoid_worker.memory import MemoryRuntime


class RemindersRuntime(DirectRuntime[ReminderCreate, ReminderPatch, Reminder]):
    UPDATE_ACK = "リマインダーを更新しました。"

    def __init__(self, memory: "MemoryRuntime") -> None:
        super().__init__(
            memory,
            kind="reminder",
            service=Reminders(memory.handler.jobs.store, now=memory.handler.jobs.now),
            view=view,
        )

    async def execute_reminders(
        self,
        name: str,
        invocation: Invocation[ReminderArguments],
        *,
        control: CallControl,
        reconcile: bool = False,
    ) -> HandlerResult:
        return await self.execute_direct(name, invocation, control=control, reconcile=reconcile)
