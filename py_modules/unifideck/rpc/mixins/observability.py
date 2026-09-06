"""Observability RPC mixin for Plugin class.
"""
from __future__ import annotations

import logging
from typing import Any

from unifideck.rpc.errors import RpcError

logger = logging.getLogger(__name__)

# Bundle tails. The audit log holds up to ``security.audit_log_capacity``
# (500) entries and the validator can emit one error per schema violation;
# a support bundle wants the recent tail, not the whole buffer, so both are
# capped here rather than at the source.
_AUDIT_TAIL = 100
_CONFIG_ERROR_TAIL = 25

class ObservabilityRPCMixin:
    """Event replay, launcher-toast drain, and the support bundle.

    The diagnostic *reads* this mixin used to expose as RPCs
    (``get_plugin_metrics``, ``get_bus_health``, ``get_feature_flags``,
    ``get_probe_history``, plus ``release_quarantine`` and
    ``report_runtime_probes``) were the backend half of a
    "DiagnosticsPanel" that was never built in ``src/``. All six were
    deleted in the audit §1.2 pass; the snapshots worth keeping now
    ride in :meth:`_support_bundle_extra`, which reaches users through
    Capture Logs — the channel that demonstrably works.
    """

    bus: Any
    services: Any
    dispatcher: Any
    watchdog: Any
    latency: Any
    replay: Any
    # Set by ``validate_config_at_startup`` via ``boot_plugin``; read only
    # by ``_config_validation_block``. Annotated with the leading
    # underscore the plugin actually uses — the retired
    # ``ConfigValidationRPCMixin`` once annotated it without one and
    # silently always hit the empty fallback.
    _config_validation_result: Any = None
    _config_degraded: bool = False

    # There is deliberately no ``set_bus_collaborators`` setter. One existed
    # with zero callers: ``bootstrap/pipeline_factory.py`` assigns
    # ``plugin.dispatcher`` / ``.watchdog`` / ``.latency`` / ``.replay``
    # directly, so the setter was a second way to do the same thing that
    # nothing used. It was invisible to both dead-code gates — ``vulture``
    # at ``min_confidence = 80`` does not report unused methods, and check 4
    # collects only public ``async def`` (a sync method is not RPC surface,
    # correctly). Audit register item 38. The annotations above are what
    # mypy needs; assignment stays with the factory that owns the objects.

    def _bus_health(self) -> dict[str, Any]:
        """Aggregate EventBus + collaborator health for the support bundle.

        :class:`EventBus` has no ``health()`` method, so the snapshot is
        built from ``_handlers`` and the pipeline collaborators' real
        APIs (``get_metrics``, ``get_snapshot``, …).

        Counts and timings only — no game titles, ids or paths — which
        is what keeps it safe for a bundle a reporter pastes in public.

        Underscore-prefixed so the RPC auto-wrapper skips it: this was
        a ``get_bus_health`` route until the audit §1.2 pass found it
        had no frontend caller and was not in the bundle either, so its
        output reached nobody.
        """
        bus_handlers: dict[str, int] = {}
        for event_key, handlers in getattr(self.bus, "_handlers", {}).items():
            bus_handlers[event_key] = len(handlers)

        health: dict[str, Any] = {
            "bus": {
                "events_registered": len(bus_handlers),
                "handler_counts": bus_handlers,
            },
        }

        dispatcher = getattr(self, "dispatcher", None)
        if dispatcher is not None:
            m = dispatcher.get_metrics()
            health["dispatcher"] = getattr(m, "__dict__", m)

        watchdog = getattr(self, "watchdog", None)
        if watchdog is not None:
            raw = watchdog.get_metrics()
            health["watchdog"] = {
                name: getattr(m, "__dict__", m) for name, m in raw.items()
            }

        latency = getattr(self, "latency", None)
        if latency is not None:
            health["latency"] = latency.get_snapshot()
        return health

    async def subscribe_replay(self, events: list[str]) -> Any:
        """Return recent events for a frontend reconnect.

        Real method is ``EventReplayBuffer.snapshot(events=...)``
        — see handler twin for the rationale.
        """
        if getattr(self, "replay", None) is None:
            raise RpcError("service_unavailable", service="replay")
        return self.replay.snapshot(events=events)

    async def get_launcher_toasts(self) -> Any:
        """Return launcher-subprocess toasts written since the last poll.

        The game launcher is a separate process; it appends
        LAUNCHER_STAGE toasts to a shared file
        (``launcher.frontend_bridge``) that this RPC drains. A
        *persistent* frontend poll calls it regardless of whether the
        QAM panel is open, so launch-time toasts (first-time prefix
        setup, dependency install, Proton switch, …) appear in Gaming
        Mode. Returns a list of payloads
        ``{i18n_key, i18n_title_key?, i18n_params?, severity?, action?}``.
        """
        drainer = getattr(self, "_launcher_drainer", None)
        if drainer is None:
            from unifideck.launcher.frontend_bridge import LauncherEventDrainer

            drainer = LauncherEventDrainer()
            self._launcher_drainer = drainer
        try:
            return drainer.poll_new()
        except Exception:
            logger.debug("[Observability] launcher toast poll failed", exc_info=True)
            return []

    async def capture_logs(self, dest_path: str = "") -> Any:
        """Collect every log + diagnostic into one zip in Downloads.

        Exists to end the "where are your logs" round-trip: the
        artifacts live in four unrelated places whose paths differ per
        user, distro and Steam layout, so asking a reporter to find
        them by hand reliably produces the wrong subset.

        ``dest_path`` is normally empty, which means "use
        ``logs.export_path`` and then the usual fallbacks". A caller may
        pass a directory or a full ``.zip`` path to override it.

        The returned dict describes the archive (path, size, file
        count) plus what was skipped and which sanity checks failed.
        Every string in it is a stable machine code, never prose, so
        the frontend maps them through i18n.
        """
        svc = getattr(self.services, "support_bundle", None)
        if svc is None:
            raise RpcError("service_unavailable", service="support_bundle")
        try:
            return await svc.capture(dest_path, extra=self._support_bundle_extra())
        except RpcError:
            raise
        except OSError as err:
            # The one expected failure: nothing writable to put it in.
            logger.warning("[Observability] log capture destination failed: %s", err)
            raise RpcError("bundle_dest_unwritable", detail=str(err)) from err
        except Exception as err:
            logger.exception("[Observability] log capture failed")
            raise RpcError("bundle_failed", detail=repr(err)) from err

    def _support_bundle_extra(self) -> dict[str, Any]:
        """Gather the facts only this layer can see.

        Feature flags, the bus/metrics snapshots and the boot-time
        config-validation result live on the plugin instance or in
        service memory, not on the filesystem, so the collector cannot
        reach them.

        Everything here is counts, timings and schema-side identifiers
        only — no titles, ids, paths or config *values* — which is what
        makes it safe in a bundle a reporter pastes in public. See
        :meth:`_config_validation_block` for the one place that needed
        real work to hold that line.

        Kept sync and underscore-prefixed so the RPC auto-wrapper skips
        it. Each lookup is guarded individually — one missing service
        must not cost us the whole bundle.

        Four of these blocks were reachable only through RPCs with no
        frontend caller (audit §1.2). Rather than delete the signal with
        the route, it moved here. A fifth, ``runtime_probes``, was
        dropped outright: its only writer was the ``report_runtime_probes``
        RPC, which no frontend ever called, so the key never appeared in
        a bundle regardless. The whole probe pipeline (frontend CEF
        suite → ``ProbeReactionService`` → ``FeatureFlagService``) is
        unbuilt and should be built or removed as one unit.
        """
        extra: dict[str, Any] = {}
        flags = getattr(self.services, "feature_flags", None)
        if flags is not None:
            try:
                extra["feature_flags"] = flags.get_flags()
            except Exception:
                logger.debug("[Observability] flag snapshot failed", exc_info=True)
        metrics = getattr(self.services, "metrics", None)
        if metrics is not None:
            try:
                extra["plugin_metrics"] = metrics.get_plugin_metrics()
            except Exception:
                logger.debug("[Observability] metrics snapshot failed", exc_info=True)
        try:
            extra["bus_health"] = self._bus_health()
        except Exception:
            logger.debug("[Observability] bus health snapshot failed", exc_info=True)
        security = getattr(self.services, "security", None)
        if security is not None:
            try:
                extra["security"] = {
                    "counters": security.get_counters(),
                    "bruteforce": security.get_bruteforce_status(),
                    "audit_log": security.get_audit_log(limit=_AUDIT_TAIL),
                }
            except Exception:
                logger.debug("[Observability] security snapshot failed", exc_info=True)
        try:
            extra["config_validation"] = self._config_validation_block()
        except Exception:
            logger.debug("[Observability] config validation block failed", exc_info=True)
        return extra

    def _config_validation_block(self) -> dict[str, Any]:
        """Boot-time config validation, with the offending values stripped.

        ``_config_validation_result`` is set by ``validate_config_at_startup``
        and, before the audit §1.2 pass, was readable only through a
        ``get_config_validation_status`` RPC that no frontend called — so a
        malformed ``~/.config/unifideck/config.json`` produced one log line
        and nothing else. This block is what makes it visible.

        Each error contributes ``source``, ``path`` and ``safe_message``
        only. ``ValidationError.message`` is deliberately excluded: it is
        jsonschema's own text, which interpolates the offending config
        *value*, so a bad credential key would render its secret verbatim
        into a bundle people paste in public. ``safe_message`` carries the
        same violation built from the schema side alone.
        """
        result = getattr(self, "_config_validation_result", None)
        block: dict[str, Any] = {
            "degraded": bool(getattr(self, "_config_degraded", False)),
        }
        errors = getattr(result, "errors", None) or []
        warnings = getattr(result, "warnings", None) or []
        block["error_count"] = len(errors)
        block["warning_count"] = len(warnings)
        block["errors"] = [
            {
                "source": getattr(e, "source", ""),
                "path": getattr(e, "path", ""),
                "safe_message": getattr(e, "safe_message", ""),
            }
            for e in errors[:_CONFIG_ERROR_TAIL]
        ]
        return block
