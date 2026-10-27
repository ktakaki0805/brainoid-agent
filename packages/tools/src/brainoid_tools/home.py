"""Narrow typed home command registration; no arbitrary services or URLs."""

from typing import Protocol

from brainoid_agent_core.ports import CallControl
from brainoid_memory.commands import CommandCreate

from brainoid_tools.registry import Arguments, Invocation, Registration, Registry
from brainoid_tools.results import HandlerResult


class SetStateArguments(CommandCreate, Arguments):
    """Requires: Set home state {complete arguments JSON, sorted keys, Unicode text}."""


class HomeBackend(Protocol):
    async def execute_home(
        self,
        invocation: Invocation[SetStateArguments],
        *,
        control: CallControl,
        reconcile: bool = False,
    ) -> HandlerResult: ...


class HomeHandler:
    def __init__(self, backend: HomeBackend) -> None:
        self.backend = backend

    async def execute(
        self, invocation: Invocation[SetStateArguments], *, control: CallControl
    ) -> HandlerResult:
        return await self.backend.execute_home(invocation, control=control)

    async def reconcile(
        self, invocation: Invocation[SetStateArguments], *, control: CallControl
    ) -> HandlerResult:
        return await self.backend.execute_home(invocation, control=control, reconcile=True)


def register_home(registry: Registry, backend: HomeBackend) -> None:
    if not registry.definitions(("home.set_state",)):
        registry.register(
            Registration(
                "home.set_state", SetStateArguments, HomeHandler(backend), effect_kind="command"
            )
        )
