"""services/probe_reaction_service.py — React to boot-time probe failures.

Two in-memory concerns sharing a single ``@subscribe`` handler:
1. Preemptive watchdog quarantine — when a probe fails, every
   handler listed in ``PROBE_TO_HANDLERS`` for that probe is
   quarantined BEFORE it gets a chance to fail at runtime.
   Avoids the usual 10-consecutive-timeout quarantine cascade.
2. Bounded in-session history — last 50 probe reports kept in
   a deque for DiagnosticsPanel. No disk persistence — keeps
   the service stateless across reloads.
"""
from __future__ import annotations

import logging
from collections import deque
from typing import TYPE_CHECKING, Any

from unifideck.core.types.events import Events
from unifideck.event_bus.event_bus_devex import auto_wire, subscribe
from unifideck.utils.config_helpers import merge_str_list_mapping

if TYPE_CHECKING:
    from unifideck.event_bus.event_bus import EventBus

logger = logging.getLogger(__name__)

# Probe id → handlers to preemptively quarantine on failure.
# router_hook_patch + rpc_roundtrip are frontend-only — no
# backend handlers to quarantine for those.
PROBE_TO_HANDLERS: dict[str, list[str]] = {
    "steam_client_apps": [
        "ArtworkService._on_shortcut_created",
        "ShortcutService._on_download_complete",
        "ShortcutService._on_sync_complete",
    ],
    "steam_client_downloads": [
        "ShortcutService._on_download_complete",
    ],
}

HISTORY_MAX_ENTRIES = 50


class ProbeReactionService:
    """React to probe reports: quarantine handlers, keep history."""

    def __init__(
        self,
        bus: EventBus,
        watchdog: Any,
        config: object | None = None,
    ) -> None:
        """Store refs, init history deque, auto_wire."""
        self._bus = bus
        self._watchdog = watchdog
        self._mapping = merge_str_list_mapping(
            config, "probes.probe_to_handlers", PROBE_TO_HANDLERS,
        )
        self._history: deque[dict[str, Any]] = deque(maxlen=HISTORY_MAX_ENTRIES)

        # ``auto_wire(self, bus)`` walks ``self``'s methods
        # and registers every ``@subscribe(Events.X)``-marked
        # handler with the bus. Earlier this site called
        # ``self._bus.auto_wire(self)`` guarded by
        # ``hasattr`` — but ``auto_wire`` is module-level,
        # not a bus method, so the hasattr check returned
        # False and every subscription was silently dropped.
        auto_wire(self, self._bus)

    def get_history(self) -> list[dict[str, Any]]:
        """Return a snapshot of the in-session probe history."""
        return list(self._history)

    @subscribe(Events.RUNTIME_PROBES_REPORTED)
    async def _on_probes_reported(self, **kwargs: Any) -> None:
        """Record the report in history + quarantine affected handlers."""
        probes = kwargs.get("probes")
        if not isinstance(probes, list):
            return

        self._record_in_history(probes)
        self._quarantine_affected_handlers(probes)

    def _record_in_history(self, probes: list[dict[str, Any]]) -> None:
        import time
        self._history.append({
            "timestamp": time.time(),
            "probes": probes
        })

    def _quarantine_affected_handlers(self, probes: list[dict[str, Any]]) -> None:
        """Pre-emptively quarantine handlers tied to failing probes.

        Refactor history (2026-05-14): inline ``if not probe_id /
        if not verdict / if verdict in (fail, error) / for handler
        in handlers`` was at CC=11. Pulled the per-probe handler
        extraction out so the loop body is a flat "if affected,
        quarantine each".
        """
        # ``quarantine_preemptive`` is the real method name on
        # ``HandlerWatchdog``. This read ``force_quarantine`` — a method that
        # exists nowhere in the tree — and the ``hasattr`` guard swallowed the
        # mismatch silently, so nothing was ever quarantined even when the
        # handler ran. Audit register item 4h; same wrong-name class as the
        # ``rc``/``exit_code`` split in ``launch_history`` (correction C-2).
        if not self._watchdog or not hasattr(self._watchdog, "quarantine_preemptive"):
            return

        for probe in probes:
            handlers = self._extract_failed_handlers(probe)
            if handlers is None:
                continue
            probe_id = probe.get("id") or probe.get("name")
            for handler_name in handlers:
                logger.info(
                    "[ProbeReaction] Preemptively quarantining %s due to %s failure",
                    handler_name, probe_id,
                )
                self._watchdog.quarantine_preemptive(
                    handler_name, reason=f"{probe_id} probe failed",
                )

    def _extract_failed_handlers(
        self, probe: dict[str, Any],
    ) -> list[str] | None:
        """Return the handlers to quarantine for ``probe``, or ``None``.

        Returns ``None`` (caller should skip this probe) when :

            * Probe id is missing or unknown to the mapping.
            * Verdict is absent.
            * Verdict isn't a failure verdict (``fail`` / ``error``).

        Returns the configured handler list when the probe is a
        confirmed failure that maps to one or more handlers.
        """
        probe_id = probe.get("id") or probe.get("name")
        if not probe_id or probe_id not in self._mapping:
            return None
        verdict = probe.get("verdict") or probe.get("severity")
        if not verdict:
            return None
        if str(verdict).lower() not in ("fail", "error"):
            return None
        return self._mapping[probe_id]
