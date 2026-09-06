"""launcher/frontend_bridge.py — launcher-subprocess → plugin toast bridge.

The game launcher runs as a separate process (spawned by Steam via the
shortcut), so its ``EventBus`` is isolated from the plugin's in-process
``EventReplayBuffer`` that the frontend polls via ``subscribe_replay``.
LAUNCHER_STAGE toast events emitted in the launcher therefore can't
reach the UI on their own.

This module is the bridge (the replacement for staging's
``launcher_toasts.json`` + ``get_launcher_toasts`` poll, adapted to the
event-replay pipeline):

* **Launcher side** appends each LAUNCHER_STAGE event as a JSONL line to
  a shared file — either automatically (a bus forwarder installed in
  ``bootstrap``) or directly via :func:`launcher_toast` from deep launch
  code that has no bus reference.
* **Plugin side** returns new lines via the ``get_launcher_toasts`` RPC
  (:class:`LauncherEventDrainer`), which a *persistent* frontend poll
  calls regardless of whether the QAM panel is open — so launch-time
  toasts appear in Gaming Mode (the QAM-bound ``ToastEventListener``
  alone never polls during a closed-panel launch).

Everything is best-effort: a launch must never fail because a toast
couldn't be written or read.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# When set (within a ``suppress_launcher_toasts`` scope), ``launcher_toast``
# no-ops. Used by the install-time prefix warmup, which reuses the launch
# path's prefix-init / compat / umu-runtime steps — those toast launch
# progress ("first-time setup", "downloading runtime", ...) that is just
# noise during a background install (the download row already shows a
# "Setting up game…" state). A ContextVar keeps it scoped to the awaiting
# task, so a concurrent real launch still toasts normally.
_SUPPRESSED: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "launcher_toast_suppressed", default=False,
)


@contextlib.contextmanager
def suppress_launcher_toasts() -> Iterator[None]:
    """Suppress ``launcher_toast`` emits within this (task-scoped) block."""
    token = _SUPPRESSED.set(True)
    try:
        yield
    finally:
        _SUPPRESSED.reset(token)


EVENTS_FILE = Path("~/.local/share/unifideck/launcher_events.jsonl").expanduser()
# The launcher only ever needs the most recent handful of stages; cap the
# file so it can't grow without bound across many launches.
_MAX_LINES = 100
# The event name the frontend's ToastEventListener subscribes to.
_LAUNCHER_STAGE = "launcher_stage"


def record_event(event: str, kwargs: dict[str, Any]) -> None:
    """Append one event (``{event, kwargs, ts}``) to the bridge file.

    Best-effort: read-modify-write keeping only the last ``_MAX_LINES``
    lines, via a temp file + atomic replace so the plugin never reads a
    torn line. Any failure is swallowed (a toast is never worth aborting
    a launch over).
    """
    try:
        EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {"event": event, "kwargs": kwargs, "ts": time.time()},
            default=str,
        )
        existing: list[str] = []
        if EVENTS_FILE.is_file():
            existing = EVENTS_FILE.read_text().splitlines()
        existing.append(line)
        if len(existing) > _MAX_LINES:
            existing = existing[-_MAX_LINES:]
        tmp = EVENTS_FILE.with_suffix(".jsonl.tmp")
        tmp.write_text("\n".join(existing) + "\n")
        tmp.replace(EVENTS_FILE)
    except (OSError, TypeError, ValueError):
        logger.debug("[frontend_bridge] record_event failed", exc_info=True)


def launcher_toast(
    i18n_key: str,
    *,
    i18n_title_key: str | None = None,
    i18n_params: dict[str, Any] | None = None,
    severity: str | None = None,
    game_title: str = "",
    duration_ms: int | None = None,
) -> None:
    """Emit a LAUNCHER_STAGE toast from launcher code that has no bus.

    Builds the same payload shape as ``launcher.rpc.emit_stage`` and
    writes it straight to the bridge file. Use this from deep launch
    helpers (umu retry, compat/prereq install, store handlers) where
    threading the ``EventBus`` through would be impractical.

    ``duration_ms`` overrides the frontend's per-severity default toast
    duration — same meaning as ``emit_stage``'s. The two builders must
    stay in step: a field one produces and the other doesn't is a
    payload that renders differently depending on which process emitted
    it.

    No-op inside a ``suppress_launcher_toasts`` scope (e.g. the install-time
    prefix warmup reusing these launch helpers).
    """
    if _SUPPRESSED.get():
        return
    payload: dict[str, Any] = {
        "i18n_key": i18n_key,
        "game_title": game_title,
        "priority": "normal",
    }
    if i18n_title_key is not None:
        payload["i18n_title_key"] = i18n_title_key
    if i18n_params is not None:
        payload["i18n_params"] = i18n_params
    if severity is not None:
        payload["severity"] = severity
    if duration_ms is not None:
        payload["duration_ms"] = duration_ms
    record_event(_LAUNCHER_STAGE, payload)


def install_bus_forwarder(bus: Any) -> None:
    """Mirror the launcher bus's LAUNCHER_STAGE emits into the file.

    Catches every bus-based emit (``emit_stage`` toasts, cloud-save
    conflict/error events, circuit-breaker stages) without each call
    site needing to know about the bridge.
    """
    from unifideck.core.types import Events

    async def _forward(**kwargs: Any) -> None:
        record_event(Events.LAUNCHER_STAGE.value, kwargs)

    bus.on(Events.LAUNCHER_STAGE, _forward)


class LauncherEventDrainer:
    """Plugin-side reader: returns launcher toast payloads since last poll.

    Dedup is by wall-clock ``ts``. The first ``poll_new`` only primes the
    high-water mark so a backlog from a previous session isn't returned
    as a burst of stale toasts; later calls return only events written
    since. A single instance is kept on the RPC mixin so the watermark
    persists across the frontend's polling.
    """

    def __init__(self) -> None:
        self._last_ts: float | None = None

    def poll_new(self) -> list[dict[str, Any]]:
        """Return the ``kwargs`` of launcher events newer than the watermark."""
        records = self._read_records()
        if self._last_ts is None:
            # Prime only — don't return the existing backlog as toasts.
            self._last_ts = max((ts for ts, _ in records), default=0.0)
            return []
        fresh: list[dict[str, Any]] = []
        for ts, rec in sorted(records, key=lambda item: item[0]):
            if ts > self._last_ts:
                self._last_ts = ts
                kwargs = rec.get("kwargs")
                if isinstance(kwargs, dict):
                    fresh.append(kwargs)
        return fresh

    @staticmethod
    def _read_records() -> list[tuple[float, dict[str, Any]]]:
        """Parse the bridge file into ``(ts, record)`` pairs (best effort)."""
        if not EVENTS_FILE.is_file():
            return []
        try:
            lines = EVENTS_FILE.read_text().splitlines()
        except OSError:
            return []
        out: list[tuple[float, dict[str, Any]]] = []
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            ts = rec.get("ts")
            if isinstance(ts, (int, float)):
                out.append((float(ts), rec))
        return out
