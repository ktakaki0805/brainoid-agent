"""Typed task arguments and registration; persistence belongs to the shared service."""

from typing import Annotated, Protocol

from brainoid_agent_core.ports import CallControl
from brainoid_contracts import DisplayPolicy, ResourceId, Version, Visibility
from brainoid_memory.tasks import TaskCreate, TaskPatch, TaskStatus
from pydantic import Field

from brainoid_tools.registry import Arguments, Invocation, Registration, Registry
from brainoid_tools.results import HandlerResult


class CreateTaskArguments(TaskCreate, Arguments):
    """Requires: Create task {complete create JSON, sorted keys, Unicode text}."""

    visibility: Visibility = Field(strict=False)
    display_policy: DisplayPolicy = Field(strict=False)


class ListTasksArguments(Arguments):
    status: TaskStatus | None = None
    limit: Annotated[int, Field(ge=1, le=20)] = 10


class UpdateTaskArguments(Arguments):
    """Requires: Update task {task_id}: {changes JSON, sorted keys, Unicode text}."""

    task_id: ResourceId
    expected_version: Version
    changes: TaskPatch

    @property
    def resource_id(self) -> ResourceId:
        return self.task_id


class DeleteTaskArguments(Arguments):
    """Requires direct user text: Delete task {task_id}."""

    task_id: ResourceId
    expected_version: Version

    @property
    def resource_id(self) -> ResourceId:
        return self.task_id


type TaskArguments = (
    CreateTaskArguments | ListTasksArguments | UpdateTaskArguments | DeleteTaskArguments
)


class TasksBackend(Protocol):
    async def execute_tasks(
        self,
        name: str,
        invocation: Invocation[TaskArguments],
        *,
        control: CallControl,
        reconcile: bool = False,
    ) -> HandlerResult: ...


class TasksHandler:
    def __init__(self, name: str, backend: TasksBackend) -> None:
        self.name, self.backend = name, backend

    async def execute(
        self, invocation: Invocation[TaskArguments], *, control: CallControl
    ) -> HandlerResult:
        return await self.backend.execute_tasks(self.name, invocation, control=control)

    async def reconcile(
        self, invocation: Invocation[TaskArguments], *, control: CallControl
    ) -> HandlerResult:
        return await self.backend.execute_tasks(
            self.name, invocation, control=control, reconcile=True
        )


def register_tasks(registry: Registry, backend: TasksBackend) -> None:
    # Explicitly injected business adapters retain ownership (e.g. confirmation
    # integration fixtures). Default production composition uses these handlers.
    for name, arguments in (
        ("tasks.create", CreateTaskArguments),
        ("tasks.list", ListTasksArguments),
        ("tasks.update", UpdateTaskArguments),
        ("tasks.delete", DeleteTaskArguments),
    ):
        if not registry.definitions((name,)):
            registry.register(Registration(name, arguments, TasksHandler(name, backend)))
