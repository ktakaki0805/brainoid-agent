"""Task specialization of the shared run-bound direct-resource boundary."""

from typing import TYPE_CHECKING

from brainoid_agent_core.ports import CallControl
from brainoid_memory.tasks import Task, TaskCreate, TaskPatch, Tasks, view
from brainoid_tools.registry import Invocation
from brainoid_tools.results import HandlerResult
from brainoid_tools.tasks import TaskArguments

from brainoid_worker.direct_resources import DirectRuntime

if TYPE_CHECKING:
    from brainoid_worker.memory import MemoryRuntime


class TasksRuntime(DirectRuntime[TaskCreate, TaskPatch, Task]):
    UPDATE_ACK = "タスクを更新しました。"

    def __init__(self, memory: "MemoryRuntime") -> None:
        super().__init__(
            memory,
            kind="task",
            service=Tasks(memory.handler.jobs.store, now=memory.handler.jobs.now),
            view=view,
        )

    async def execute_tasks(
        self,
        name: str,
        invocation: Invocation[TaskArguments],
        *,
        control: CallControl,
        reconcile: bool = False,
    ) -> HandlerResult:
        return await self.execute_direct(name, invocation, control=control, reconcile=reconcile)
