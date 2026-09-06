"""services/bootstrap/service_defs.py — Layer-5 wiring table + instantiator.

Single source of truth for Layer-5 service wiring. The
``_SERVICE_DEFS`` tuple declares every service's module path,
class name, and constructor argument builders. The companion
``_instantiate_service()`` helper consumes one row of that
table plus shared dependencies and returns the constructed
service instance.

Both consumers of this file — ``bootstrap_services`` (full
plugin path) and ``build_service_subset`` (launcher
out-of-process reduced path) — funnel through the same
``_instantiate_service`` helper so the two bootstrap paths
never drift apart.

Extracted from the flat ``service_bootstrap.py`` on 2026-04-19.

Adding a new Layer-5 service means:
1. One new attribute on ``ServiceContainer`` (in ``container.py``)
2. One new entry at the bottom of ``_SERVICE_DEFS`` here

No ``constructor.py`` or ``main.py`` edit required.
"""
from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from unifideck.config import ConfigManager
    from unifideck.core.cache_manager import CacheManager
    from unifideck.event_bus.bus_pipeline import BusPipeline
    from unifideck.event_bus.event_bus import EventBus
    from unifideck.stores import StoreRegistry

    from .paths import ServicePaths

# Service wiring table. Each entry is a tuple:
# (container_attr, module_path, class_name, build_args, build_kwargs)
# where ``build_args`` and ``build_kwargs`` are callables that
# receive (bus, registry, cache, config, paths, pipeline) and
# return args/kwargs.
#
# This is the single source of truth for Layer-5 wiring. Adding
# a new service means one entry here plus one attribute on
# ServiceContainer — no main.py edit required.
#
# Note: LauncherService is deliberately absent from this table.
# The plugin itself never launches games — the launch flow runs
# out-of-process via bin/unifideck-launcher → dispatcher.py.
# LauncherService is constructed on demand inside the dispatcher's
# own minimal service graph, not here.
_SERVICE_DEFS: tuple[tuple[Any, ...], ...] = (
    (
        "shortcut", "unifideck.services.shortcut",
        "ShortcutService",
        lambda b, r, c, cfg, p, pl: (
            b, p.shortcuts_path, p.games_map_path, p.launcher_path,
        ),
        lambda b, r, c, cfg, p, pl: {},
    ),
    (
        "download", "unifideck.services.download",
        "DownloadService",
        lambda b, r, c, cfg, p, pl: (b, r, p.queue_file),
        lambda b, r, c, cfg, p, pl: {"launcher_path": p.launcher_path},
    ),
    (
        "metadata", "unifideck.services.metadata_service",
        "MetadataService",
        lambda b, r, c, cfg, p, pl: (b, c),
        lambda b, r, c, cfg, p, pl: {"config": cfg},
    ),
    (
        "artwork", "unifideck.services.artwork",
        "ArtworkService",
        lambda b, r, c, cfg, p, pl: (b, c, p.grid_dir),
        lambda b, r, c, cfg, p, pl: {"config": cfg},
    ),
    # CompatibilityService — post-sync ProtonDB + Deck-Verified
    # fetcher. Wired to ``SyncService.register_post_sync_phase``
    # via :func:`wire_sync_service` after ``bootstrap_services``
    # returns (sync_service lives on plugin, not the container).
    (
        "compatibility", "unifideck.services.compatibility",
        "CompatibilityService",
        lambda b, r, c, cfg, p, pl: (b, c),
        lambda b, r, c, cfg, p, pl: {"config": cfg},
    ),
    # ActivityLogService — persists LIBRARY_SYNC_* events to a
    # rotating JSONL file. Independent of PlaytimeService (which is
    # per-game session tracking, not sync history). Consumed by the
    # RPC handler for the "recent syncs" panel.
    (
        "activity_log", "unifideck.services.activity_log",
        "ActivityLogService",
        lambda b, r, c, cfg, p, pl: (b, p.activity_log),
        lambda b, r, c, cfg, p, pl: {},
    ),
    (
        "proton", "unifideck.services.proton_service",
        "ProtonService",
        lambda b, r, c, cfg, p, pl: (b,),
        lambda b, r, c, cfg, p, pl: {},
    ),
    (
        "cdp", "unifideck.cdp.cdp_client",
        "CDPClient",
        lambda b, r, c, cfg, p, pl: (),
        lambda b, r, c, cfg, p, pl: {"config": cfg},
    ),
    (
        "cloudsave", "unifideck.services.cloud_save",
        "CloudSaveService",
        lambda b, r, c, cfg, p, pl: (b, p.local_save_root),
        lambda b, r, c, cfg, p, pl: {"cloud_root": p.cloud_root, "config": cfg, "cache": c},
    ),
    (
        "metrics", "unifideck.core.metrics_collector",
        "MetricsCollector",
        lambda b, r, c, cfg, p, pl: (b,),
        lambda b, r, c, cfg, p, pl: {},
    ),
    (
        "account", "unifideck.services.account_service",
        "AccountService",
        lambda b, r, c, cfg, p, pl: (b, p.loginusers_path),
        lambda b, r, c, cfg, p, pl: {},
    ),
    (
        "playtime", "unifideck.services.playtime",
        "PlaytimeService",
        lambda b, r, c, cfg, p, pl: (b, p.playtime_db),
        lambda b, r, c, cfg, p, pl: {},
    ),
    # ── Bus-pipeline-aware services ──────────────────────────
    # These three were historically instantiated in
    # _build_eventbus_pipeline as flat attributes on Plugin.
    # Migration to ServiceContainer benefits:
    # - Uniform lifecycle (stop_all_services, start_async_services)
    # - Error isolation (failed wiring = degraded mode, not crash)
    # - Single source of truth for service access
    # - Eliminates getattr(self, "config", None) defensive workarounds
    # The ``pl`` (BusPipeline) param surfaces watchdog/latency/
    # replay/batcher/dispatcher to services that need them. Today
    # only ProbeReactionService consumes it (for watchdog), but
    # future debug/observability services may attach to other
    # pipeline components without further bootstrap changes.
    (
        "feature_flags", "unifideck.services.feature_flag_service",
        "FeatureFlagService",
        lambda b, r, c, cfg, p, pl: (b,),
        lambda b, r, c, cfg, p, pl: {"config": cfg},
    ),
    (
        "probe_reaction", "unifideck.services.probe_reaction_service",
        "ProbeReactionService",
        lambda b, r, c, cfg, p, pl: (b, pl.watchdog if pl else None),
        lambda b, r, c, cfg, p, pl: {"config": cfg},
    ),
    (
        "security", "unifideck.services.security",
        "SecurityService",
        lambda b, r, c, cfg, p, pl: (b,),
        lambda b, r, c, cfg, p, pl: {
            "config": cfg,
            "replay": pl.replay if pl else None,
        },
    ),
    # LaunchHistoryService — circuit breaker storage for the
    # per-game launch failure tracker (rework piste #8). Used by
    # the plugin RPC handlers (get_launch_failures,
    # clear_launch_failures, arm_circuit_bypass) and the
    # ``useCircuitState`` badge (register item 4a).
    #
    # NOT read-only, despite what this comment said until
    # 2026-08-26: the plugin instance subscribes GAME_LAUNCHED /
    # GAME_STOPPED and writes through ``record_success``. That is
    # the ONLY path that clears a tripped breaker — the launcher
    # process records failures, and its own instance is built
    # without a bus (``dispatcher.py`` passes config alone), so it
    # subscribes nothing. Item 46: the handler read an ``rc`` key
    # no emitter sends, so ``record_success`` was unreachable and
    # the breaker could accumulate but never clear.
    (
        "launch_history", "unifideck.services.launch_history",
        "LaunchHistoryService",
        lambda b, r, c, cfg, p, pl: (cfg,),
        lambda b, r, c, cfg, p, pl: {"bus": b},
    ),
    # LaunchLogsService — async facade over
    # ``launcher.diagnostics.log_archive``. Read-only on the
    # plugin side (launches write the archive themselves from
    # the out-of-process launcher binary). Consumed by the RPC
    # method ``get_launch_logs``. It named ``export_launch_logs``
    # too until 2026-08-26; that RPC was deleted in the §1.2 pass
    # and the service method with it (audit register item 4j).
    # Before this entry was added, the mixin referenced
    # ``self.services.launch_logs`` but the attribute was
    # always ``None``, so the endpoint raised
    # ``service_unavailable`` to the frontend regardless of
    # whether the archive existed on disk.
    #
    # Currently unreachable end to end: ``get_launch_logs`` is
    # only reached by the ``show-logs`` toast action, which has
    # no producer. Capture Logs collects the same files.
    (
        "launch_logs", "unifideck.services.launch_logs",
        "LaunchLogsService",
        lambda b, r, c, cfg, p, pl: (),
        lambda b, r, c, cfg, p, pl: {"config": cfg},
    ),
    # SupportBundleService — backs the "Capture Logs" RPC. Needs config
    # (for logs.export_path and logs.archive_path) and paths (for the
    # data dir, Steam root, shortcuts.vdf and the playtime DB), so the
    # audit reports the locations this install actually uses rather
    # than the defaults. Plugin-only: the launcher subset never asks
    # for it, and it holds no state beyond a re-entrancy lock.
    (
        "support_bundle", "unifideck.services.support_bundle",
        "SupportBundleService",
        lambda b, r, c, cfg, p, pl: (),
        lambda b, r, c, cfg, p, pl: {"config": cfg, "paths": p},
    ),
    # Sprint 18e — MicrosoftSubscriptionService. Consumes the
    # shared EventBus and CacheManager; reads config for its
    # endpoint URL. Must be instantiated BEFORE the StoreRegistry
    # wires MicrosoftStore so the store can receive it via its
    # subscription_service kwarg. Order is enforced by the
    # bootstrap sequence (services → stores → registry).
    (
        "microsoft_subscription",
        "unifideck.services.microsoft_subscription",
        "MicrosoftSubscriptionService",
        lambda b, r, c, cfg, p, pl: (b, c),
        lambda b, r, c, cfg, p, pl: {"config": cfg},
    ),
    # AchievementWatcher — GOG live unlock toasts (during play) + an
    # end-of-session summary (persisted for the game-info panel). Needs the
    # registry to reach the GOG store; plugin-only (not in the launcher
    # subset), so registry is always present here.
    (
        "achievements", "unifideck.services.achievements",
        "AchievementWatcher",
        lambda b, r, c, cfg, p, pl: (b, r),
        lambda b, r, c, cfg, p, pl: {"config": cfg},
    ),
    # PlaytimeSyncService — reports finalized local play sessions up to GOG/Epic
    # (Heroic #1240) and reconciles store totals for display. Needs the registry
    # to reach the stores (like AchievementWatcher) + the shared playtime.db path
    # (read sessions, stamp ``reported_at``). Plugin-only.
    (
        "playtime_sync", "unifideck.services.playtime_sync",
        "PlaytimeSyncService",
        lambda b, r, c, cfg, p, pl: (b, r),
        lambda b, r, c, cfg, p, pl: {"config": cfg, "db_path": p.playtime_db},
    ),
)


def _instantiate_service(
    def_entry: tuple[Any, ...],
    bus: EventBus,
    registry: StoreRegistry | None,
    cache: CacheManager | None,
    config: ConfigManager,
    paths: ServicePaths,
    pipeline: BusPipeline | None = None,
) -> Any:
    """Instantiate a single service from a ``_SERVICE_DEFS`` row.

    Shared by full bootstrap and launcher subset bootstrap so the
    two paths never drift. None-safe for registry/cache/pipeline
    — the launcher subset doesn't consume them; services whose
    lambdas dereference ``pl.<component>`` will fail if pl is None.
    Import + constructor errors propagate to the caller.
    """
    _attr, module_path, class_name, build_args, build_kwargs = def_entry
    module = import_module(module_path)
    cls = getattr(module, class_name)

    args = build_args(bus, registry, cache, config, paths, pipeline)
    kwargs = build_kwargs(bus, registry, cache, config, paths, pipeline)

    return cls(*args, **kwargs)
