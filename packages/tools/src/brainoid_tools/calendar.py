"""Calendar reference arguments cannot carry identity, scopes or credential references."""

from brainoid_contracts import ResourceId
from brainoid_memory.calendar_reader import CalendarQuery

from brainoid_tools.registry import Arguments, Handler, Registration, Registry


class ListCalendarArguments(CalendarQuery, Arguments):
    """Resolve a concrete UTC interval (at most 31 days) using the user's timezone.

    Ask for clarification when a local time is ambiguous. Follow next_cursor with
    identical query fields; a page is not a complete list while next_cursor exists.
    Calendar selection is an owner-only settings action, never a model argument.
    """

    integration_id: ResourceId


def register_calendar(registry: Registry, handler: Handler[ListCalendarArguments]) -> None:
    if not registry.definitions(("calendar.list",)):
        registry.register(Registration("calendar.list", ListCalendarArguments, handler))
