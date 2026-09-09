"""Event priority enum — ordering hints for the dispatcher.

``EventPriority`` is an ``IntEnum`` with three levels used by
``PriorityDispatcher`` to order event delivery:

* ``CRITICAL``   (0) — plugin lifecycle, game launches, security
  events. Must be delivered before anything else in the queue;
* ``NORMAL``     (1) — most events default here (auth flow, sync
  outcomes, download lifecycle);
* ``BACKGROUND`` (2) — high-frequency progress / telemetry that
  can be coalesced or dropped under back-pressure.

Two module-level helpers:

* ``get_priority(event)``     — look up the canonical priority
  for an event from the ``_DEFAULT_PRIORITY`` table.
* ``get_coalesce_key(event)`` — return the kwarg name to use as
  the coalesce discriminator (e.g. ``"download_id"`` for
  ``DOWNLOAD_PROGRESS``), or ``""`` if the event isn't
  coalescible.

Kept in its own module so the priority table can be reviewed
independently of the dispatcher mechanics.
"""

from __future__ import annotations

from enum import IntEnum

from unifideck.core.types import Events


class EventPriority(IntEnum):
    """Three-level event priority for dispatcher ordering.

    Lower numeric value = higher priority. ``IntEnum`` (not
    ``Enum``) so values are directly usable as sort keys without
    a ``.value`` accessor.
    """

    CRITICAL = 0
    NORMAL = 1
    BACKGROUND = 2

_DEFAULT_PRIORITY: dict[Events, EventPriority] = {
    Events.GAME_LAUNCHED: EventPriority.CRITICAL,
    Events.GAME_STOPPED: EventPriority.CRITICAL,
    Events.GAME_UNINSTALLED: EventPriority.NORMAL,
    Events.GAME_UPDATE_AVAILABLE: EventPriority.BACKGROUND,
    Events.SYNC_STARTED: EventPriority.NORMAL,
    Events.SYNC_COMPLETE: EventPriority.NORMAL,
    Events.SYNC_CANCELLED: EventPriority.NORMAL,
    Events.SYNC_FAILED: EventPriority.NORMAL,
    Events.SYNC_PROGRESS: EventPriority.BACKGROUND,
    Events.STORE_AUTH_STARTED: EventPriority.NORMAL,
    Events.STORE_AUTH_COMPLETE: EventPriority.NORMAL,
    Events.STORE_AUTH_FAILED: EventPriority.NORMAL,
    Events.STORE_LOGOUT: EventPriority.NORMAL,
    Events.DOWNLOAD_QUEUED: EventPriority.NORMAL,
    Events.DOWNLOAD_STARTED: EventPriority.NORMAL,
    Events.DOWNLOAD_COMPLETE: EventPriority.NORMAL,
    Events.DOWNLOAD_CANCELLED: EventPriority.NORMAL,
    Events.DOWNLOAD_FAILED: EventPriority.NORMAL,
    Events.DOWNLOAD_PROGRESS: EventPriority.BACKGROUND,
}
COALESCE_KEY: dict[Events, str] = {
    # SYNC_PROGRESS is intentionally NOT coalesced — when it is,
    # the dispatcher re-emits as ``sync_progress_batch`` and the
    # frontend (which only subscribes to ``sync_progress``) sees
    # no progress events at all. The volume is low enough
    # (one per store, ~4-10 emissions per run) that the original
    # coalescing benefit doesn't apply.
    Events.DOWNLOAD_PROGRESS: "download_id",
}

def get_priority(event: Events | str) -> EventPriority:
    """Return the canonical ``EventPriority`` for ``event``.

    Looks up ``event`` in the ``_DEFAULT_PRIORITY`` table.
    Accepts either a typed ``Events`` member or its string value
    (RPC payloads often carry the string form); strings that
    don't match any enum member fall back to ``NORMAL``.

    Args:
        event: an ``Events`` enum value or its string equivalent.

    Returns:
        The mapped priority, or ``EventPriority.NORMAL`` when
        the event has no entry in the table (defensive: a new
        event type defaults to NORMAL rather than crashing).
    """
    if isinstance(event, Events):
        return _DEFAULT_PRIORITY.get(event, EventPriority.NORMAL)
    try:
        resolved = Events(event)
        return _DEFAULT_PRIORITY.get(resolved, EventPriority.NORMAL)
    except ValueError:
        return EventPriority.NORMAL

def get_coalesce_key(event: Events | str) -> str:
    """Return the coalescing-key kwarg name for ``event``.

    Coalescing merges rapid-fire emissions of the same event so
    e.g. 100 ``DOWNLOAD_PROGRESS`` events for the same download
    collapse to a single delivery (the latest payload). The
    coalesce key tells the dispatcher **which** kwarg in the
    payload identifies "same emitter" — for download progress
    that's ``download_id``, for sync progress it's ``store``.

    An empty string return means the event is not coalescible —
    every emission is delivered as-is.

    Args:
        event: an ``Events`` enum value or its string equivalent.

    Returns:
        The kwarg name to use as the coalesce discriminator, or
        ``""`` when coalescing is disabled.
    """
    if isinstance(event, Events):
        return COALESCE_KEY.get(event, "")
    try:
        resolved = Events(event)
        return COALESCE_KEY.get(resolved, "")
    except ValueError:
        return ""
