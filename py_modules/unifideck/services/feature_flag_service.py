"""services/feature_flag_service.py — Probe-driven feature flags.

Listens for ``RUNTIME_PROBES_REPORTED`` after the frontend's
boot-time CEF probe suite completes. Translates failing probes
into disabled features via ``PROBE_TO_FEATURES``, exposes flags
via RPC so hooks consult them before using a capability.
State is in-memory only — resets at every plugin reload. Probes
re-run at each boot so flags stay fresh with the current Steam
client state.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from unifideck.core.types.events import Events
from unifideck.event_bus.event_bus_devex import auto_wire, subscribe
from unifideck.utils.config_helpers import merge_str_list_mapping

if TYPE_CHECKING:
    from unifideck.event_bus.event_bus import EventBus

logger = logging.getLogger(__name__)

# Probe id → features it gates. One failing probe disables every
# feature in its list. Kept as the module-level default; user
# config at ``probes.probe_to_features`` can override per probe id.
PROBE_TO_FEATURES: dict[str, list[str]] = {
    "steam_client_apps": [
        "shortcut_creation",
        "artwork_injection",
        "play_button_override",
    ],
    "steam_client_downloads": [
        "download_progress_polling",
        "download_queue_ui",
    ],
    "steam_client_input": [
        "controller_hints",
    ],
    "router_hook_patch": [
        "library_view_patch",
    ],
    "rpc_roundtrip": [
        "diagnostics_panel_polling",
    ],
}

# Every feature we know about. Starts enabled; probes may disable.
ALL_FEATURES: list[str] = sorted({
    f for features in PROBE_TO_FEATURES.values() for f in features
})


class FeatureFlagService:
    """Reactive feature flag store driven by runtime probes."""

    def __init__(
        self, bus: EventBus, config: object | None = None,
    ) -> None:
        """Merge config-supplied mapping, init flags."""
        self._bus = bus
        self._mapping = merge_str_list_mapping(
            config, "probes.probe_to_features", PROBE_TO_FEATURES,
        )

        self._flags = dict.fromkeys(ALL_FEATURES, True)

        # ``auto_wire(self, bus)`` walks ``self``'s methods
        # and registers every ``@subscribe(Events.X)``-marked
        # handler with the bus. Earlier this site called
        # ``self._bus.auto_wire(self)`` guarded by
        # ``hasattr`` — but ``auto_wire`` is module-level,
        # not a bus method, so the hasattr check returned
        # False and every subscription was silently dropped.
        auto_wire(self, self._bus)

    def get_flags(self) -> dict[str, bool]:
        """Return a copy of the current feature flag state."""
        return self._flags.copy()

    def is_enabled(self, feature: str) -> bool:
        """Check one flag. Unknown features return True."""
        return self._flags.get(feature, True)

    @subscribe(Events.RUNTIME_PROBES_REPORTED)
    async def _on_probes_reported(self, **kwargs: Any) -> None:
        """Update flags from the report."""
        probes = kwargs.get("probes")
        if not isinstance(probes, list):
            return

        changed = self._apply_probes_to_flags(probes)
        if changed:
            logger.info("[FeatureFlagService] Flags changed: %s", changed)

    def _apply_probes_to_flags(self, probes: list[dict[str, Any]]) -> list[str]:
        """Walk probes and update affected features.

        Refactor history (2026-05-14): was a single method at
        CC=20 — the loop body inlined probe-id validation,
        verdict normalisation, the 3-way state mapping
        (fail/error → False, ok/info → True, warn → skip), and
        the per-feature mutation loop with its dynamic-add
        branch. Pulled the per-probe resolution into
        ``_resolve_state_for_probe`` (pure) and the per-feature
        application into ``_apply_state_to_feature`` (mutates
        ``self._flags`` + ``changed``).
        """
        changed: list[str] = []
        for probe in probes:
            resolved = self._resolve_state_for_probe(probe)
            if resolved is None:
                continue
            features_affected, new_state = resolved
            for feature in features_affected:
                self._apply_state_to_feature(feature, new_state, changed)
        return changed

    def _resolve_state_for_probe(
        self, probe: dict[str, Any],
    ) -> tuple[list[str], bool] | None:
        """Map a single probe dict to ``(features, new_state)``.

        Returns ``None`` for any probe that should be ignored :

            * Missing or unknown probe id (``id`` / ``name``
              key absent, or not in ``self._mapping``).
            * No verdict carried (``verdict`` / ``severity``
              key absent).
            * Verdict is ``warn`` — the documented "leave as-is"
              outcome.

        Verdict normalisation accepts both legacy ``severity``
        and current ``verdict`` keys, so the upstream probe
        format can shift without breaking this consumer.
        """
        probe_id = probe.get("id") or probe.get("name")
        if not probe_id or probe_id not in self._mapping:
            return None
        verdict = probe.get("verdict") or probe.get("severity")
        if not verdict:
            return None
        verdict_norm = str(verdict).lower()
        if verdict_norm in ("fail", "error"):
            new_state = False
        elif verdict_norm in ("ok", "info"):
            new_state = True
        else:
            # 'warn' (or anything unrecognised) — leave untouched.
            return None
        return self._mapping[probe_id], new_state

    def _apply_state_to_feature(
        self,
        feature: str,
        new_state: bool,
        changed: list[str],
    ) -> None:
        """Mutate ``self._flags[feature]`` and record the diff.

        Unknown features are auto-added at ``True`` so a flag
        registered after the initial ``ALL_FEATURES`` snapshot
        (e.g. a runtime-discovered capability) still gets
        managed coherently. Only an actual state change is
        appended to ``changed``.
        """
        if feature not in self._flags:
            self._flags[feature] = True
        if self._flags[feature] != new_state:
            self._flags[feature] = new_state
            changed.append(f"{feature}={new_state}")
