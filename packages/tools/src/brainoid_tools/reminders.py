"""Typed reminder arguments and registration; persistence belongs to the shared service."""

from typing import Annotated, Protocol

from brainoid_agent_core.ports import CallControl
from brainoid_contracts import (
    DisplayPolicy,
    IanaTimezone,
    ResourceId,
    UtcTimestamp,
    Version,
    Visibility,
)
from brainoid_memory.reminders import ReminderCreate, ReminderPatch, ReminderReplan, ReminderStatus
from pydantic import Field

from brainoid_tools.registry import Arguments, Invocation, Registration, Registry
from brainoid_tools.results import HandlerResult


class CreateReminderArguments(ReminderCreate, Arguments):
    """Requires: Create reminder {complete create JSON, sorted keys, Unicode text}."""

    visibility: Visibility = Field(strict=False)
    display_policy: DisplayPolicy = Field(strict=False)


class ListRemindersArguments(Arguments):
    status: ReminderStatus | None = None
    limit: Annotated[int, Field(ge=1, le=20)] = 10


class UpdateReminderArguments(Arguments):
    """Requires: Update reminder {reminder_id}: {changes JSON, sorted keys, Unicode text}."""

    reminder_id: ResourceId
    expected_version: Version
    changes: ReminderPatch

    @property
    def resource_id(self) -> ResourceId:
        return self.reminder_id


class SnoozeReminderArguments(Arguments):
    """Requires: Snooze reminder {reminder_id}: {due_at/timezone JSON, sorted keys}."""

    reminder_id: ResourceId
    expected_version: Version
    due_at: UtcTimestamp
    timezone: IanaTimezone

    @property
    def resource_id(self) -> ResourceId:
        return self.reminder_id

    @property
    def changes(self) -> ReminderReplan:
        return ReminderReplan(due_at=self.due_at, timezone=self.timezone, snooze=True)


type ReminderArguments = (
    CreateReminderArguments
    | ListRemindersArguments
    | UpdateReminderArguments
    | SnoozeReminderArguments
)


class RemindersBackend(Protocol):
    async def execute_reminders(
        self,
        name: str,
        invocation: Invocation[ReminderArguments],
        *,
        control: CallControl,
        reconcile: bool = False,
    ) -> HandlerResult: ...


class RemindersHandler:
    def __init__(self, name: str, backend: RemindersBackend) -> None:
        self.name, self.backend = name, backend

    async def execute(
        self, invocation: Invocation[ReminderArguments], *, control: CallControl
    ) -> HandlerResult:
        return await self.backend.execute_reminders(self.name, invocation, control=control)

    async def reconcile(
        self, invocation: Invocation[ReminderArguments], *, control: CallControl
    ) -> HandlerResult:
        return await self.backend.execute_reminders(
            self.name, invocation, control=control, reconcile=True
        )


def register_reminders(registry: Registry, backend: RemindersBackend) -> None:
    # Explicitly injected business adapters retain ownership (e.g. confirmation
    # integration fixtures). Default production composition uses these handlers.
    for name, arguments in (
        ("reminders.create", CreateReminderArguments),
        ("reminders.list", ListRemindersArguments),
        ("reminders.update", UpdateReminderArguments),
        ("reminders.snooze", SnoozeReminderArguments),
    ):
        if not registry.definitions((name,)):
            registry.register(Registration(name, arguments, RemindersHandler(name, backend)))
