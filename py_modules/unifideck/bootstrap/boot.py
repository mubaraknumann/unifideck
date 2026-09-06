"""bootstrap.boot — full plugin cold-start orchestration.

Runs exactly once when Decky Loader loads Unifideck. The
ordering below is load-bearing:

  Layer 2 (core) → Layer 4 (stores) → Layer 5 (services)

Services subscribe to the EventBus in their ``__init__``, so the
event topology is only live after the bootstrap step.

Boot sequence (each step must complete before the next):

  0. Startup migrations — one-time renames/rewrites of on-disk
     state left behind by older versions (see
     ``bootstrap.migrations``). Runs before anything else touches
     disk so a migrated file is already in place the first time a
     store or config layer reads it.
  1. ``EventBus`` instantiation — empty, no pipeline yet
  2. Pipeline construction — watchdog + latency + replay +
     batcher + dispatcher, with dispatcher.start() awaited
  3. ``CacheManager`` instantiation pointing at the data dir
  4. Cache name registration (``register_default_caches``) —
     MUST happen before stores are discovered because store
     constructors may call ``is_available()`` which reads
     from the cache
  5. ``ConfigManager`` with 3-layer merge (defaults + user + code)
  6. Config validation — marks plugin as degraded on failure but
     never prevents boot
  7. ``StoreRegistry`` + ``SyncService`` instantiation
  8. Store auto-discovery — scans ``stores/`` for connectors
  9. Layer-5 services bootstrap via ``ServiceContainer``
  10. ``start_async_services`` — kicks off long-lived service
      workers (cloudsave, download queue, etc.)

Mutates the plugin in place. Never raises.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from unifideck.bootstrap.cache_registry import register_default_caches
from unifideck.bootstrap.migrations import run_startup_migrations
from unifideck.bootstrap.pipeline_factory import build_eventbus_pipeline
from unifideck.config import ConfigManager
from unifideck.config.startup import validate_config_at_startup
from unifideck.core.cache_manager import CacheManager
from unifideck.core.sync_service import SyncService
from unifideck.event_bus.event_bus import EventBus
from unifideck.services.bootstrap import (
    bootstrap_services,
    inject_store_dependencies,
    start_async_services,
)
from unifideck.stores import StoreRegistry

logger = logging.getLogger(__name__)

#: Strong references to fire-and-forget boot tasks. Without this the event loop
#: may garbage-collect a task that is still running; entries remove themselves
#: on completion via ``add_done_callback``.
_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()

async def boot_plugin(
    plugin: Any,
    *,
    decky_plugin_dir: str,
    decky_runtime_dir: str,
    user_config_path_resolver: Any,
) -> None:
    """Cold-start ``plugin`` in place.

    Args:
        plugin: The ``Plugin`` instance. Will have its attributes
            populated in place — the method exists to preserve
            the subtle ordering of ``self.*`` assignments that
            services depend on (each new service may subscribe
            to events emitted by attributes set earlier).
        decky_plugin_dir: The absolute path passed by Decky Loader
            as the plugin root. **Read-only on user installs.**
            Used to resolve ``defaults/`` and
            ``py_modules/unifideck/stores/``.
        decky_runtime_dir: Writable per-plugin runtime directory
            (``DECKY_PLUGIN_RUNTIME_DIR``). Holds the cache and any
            other state that needs to survive plugin reloads.
            Never use ``decky_plugin_dir`` for writable state — that
            location is owned by the install process and is
            read-only on normal user installs.
        user_config_path_resolver: Zero-arg callable that returns
            the user overrides JSON path. Injected so tests can
            stub out the XDG/env resolution without monkey-patching.

    Never raises: validation failures flag degraded mode and
    continue booting; service bootstrap failures are logged by
    the ServiceContainer itself and leave the failed service
    entry as ``None`` for the mixin guards to handle.
    """
    run_startup_migrations()
    pipeline = await _boot_layer2_core(plugin, decky_runtime_dir)
    await _boot_config_and_validate(
        plugin, decky_plugin_dir, user_config_path_resolver,
    )
    _boot_layer4_stores(plugin, decky_plugin_dir)
    await _boot_layer5_services(plugin, pipeline, decky_plugin_dir)
    await _boot_updater(plugin, decky_plugin_dir)
    await _boot_update_sweep(plugin)
    _boot_post_sync_reconcile(plugin)
    await _start_store_background_tasks(plugin)
    _wire_prefix_bridge(plugin)
    logger.info("[Unifideck] plugin loaded")

async def _boot_layer2_core(plugin: Any, decky_runtime_dir: str) -> Any:
    """Layer 2 — EventBus + pipeline + cache.

    Returns the ``BusPipeline`` so ``boot_plugin`` can forward it
    to ``bootstrap_services``.
    """
    plugin.bus = EventBus()
    pipeline = await build_eventbus_pipeline(plugin)
    plugin.cache = CacheManager(
        str(Path(decky_runtime_dir) / "cache"),
    )
    register_default_caches(plugin.cache)
    return pipeline

def _resolve_defaults_path(decky_plugin_dir: str) -> str:
    """Locate the bundled config.json across Decky build layouts.

    Delegates to :func:`config.defaults_path.resolve_defaults_config_path`
    so the plugin backend and the launcher process resolve it identically —
    the launcher used to hardcode the unflattened layout and therefore found
    no defaults on a Decky CLI build.
    """
    from unifideck.config.defaults_path import resolve_defaults_config_path
    return resolve_defaults_config_path(decky_plugin_dir)

async def _boot_config_and_validate(
    plugin: Any,
    decky_plugin_dir: str,
    user_config_path_resolver: Any,
) -> None:
    """Layer 3 — ConfigManager + startup validation.

    Validates the config at boot BEFORE stores are instantiated.
    Failures log a warning, set the "degraded" flag, emit
    CONFIG_VALIDATION_FAILED on the bus for SecurityService's audit
    log, and continue booting anyway. Validation covers user
    overrides as well.

    None of that is user-facing at boot: the flag drives no behaviour
    and there is no UI for it. A malformed config surfaces afterwards
    through Capture Logs — the bundle carries both the audit trail and
    a ``config_validation`` block with the errors. This docstring
    promised a "DiagnosticsPanel" the user could see and fix their
    config in; no such component has ever existed in ``src/``
    (audit §1.2/§1.3).

    ConfigManager merges defaults/config.json + user overrides
    from the XDG location (~/.config/unifideck/config.json by
    default, overridable via UNIFIDECK_USER_CONFIG /
    XDG_CONFIG_HOME). The user file is allowed to be missing at
    first run: the manager skips the user layer and falls back
    to defaults + hardcoded values.
    """
    plugin._user_config_path = user_config_path_resolver()
    defaults_path = _resolve_defaults_path(decky_plugin_dir)
    plugin.config = ConfigManager(
        defaults_path=defaults_path,
        user_path=plugin._user_config_path,
    )
    (
        plugin._config_validation_result,
        plugin._config_degraded,
    ) = await validate_config_at_startup(
        bus=plugin.bus,
        config=plugin.config,
        defaults_path=defaults_path,
        user_config_path=plugin._user_config_path,
    )

def _boot_layer4_stores(plugin: Any, decky_plugin_dir: str) -> None:
    """Layer 4 — StoreRegistry + SyncService + auto-discovery."""
    plugin.registry = StoreRegistry(plugin.bus)
    # SyncService needs the launcher path so it can assign each
    # game a stable Steam-shortcut AppID (deterministic from
    # ``crc32(launcher_path + title)`` — survives install /
    # uninstall transitions). Without this, every game's
    # ``app_id`` stays at the per-store-default ``0`` and
    # downstream ShortcutService.reconcile + ArtworkService can't
    # key on it.
    launcher_path = str(
        Path(decky_plugin_dir) / "bin" / "unifideck-launcher",
    )
    plugin.sync_service = SyncService(
        plugin.registry, plugin.bus, launcher_path=launcher_path,
        config=plugin.config, cache=plugin.cache,
    )
    stores_dir = str(
        Path(decky_plugin_dir) / "py_modules" / "unifideck" / "stores",
    )
    plugin.registry.auto_discover(
        stores_dir,
        bus=plugin.bus,
        cache=plugin.cache,
        plugin_dir=decky_plugin_dir,
        config=plugin.config,
    )

async def _boot_layer5_services(
    plugin: Any, pipeline: Any, decky_plugin_dir: str,
) -> None:
    """Layer 5 — infrastructure services + async workers.

    Three phases :

    1. ``bootstrap_services`` builds the full service container
       (shortcut, download, cdp, browser_monitor, ...).
    2. ``inject_store_dependencies`` walks ``_STORE_INJECTIONS``
 and writes each (attr, service) pair onto its
       auto-discovered store. Stores that expose
       ``_rebuild_auth_after_injection`` get it called so they
       can wire their auth flow against the freshly-injected
       ``_browser_monitor``.
    3. ``start_async_services`` kicks any background tasks
       (download worker, security audit pump, ...).
    """
    plugin.services = bootstrap_services(
        plugin.bus, plugin.registry, plugin.cache, plugin.config,
        pipeline, plugin_dir=decky_plugin_dir,
    )
    inject_store_dependencies(plugin.registry, plugin.services)
    # Post-bootstrap wiring: SyncService lives on ``plugin``, not on
    # the service container, so services that need to register
    # post-sync phases (currently CompatibilityService) get their
    # reference here. Without this call, ``mark_complete`` would
    # fire before the compat fetch finished.
    compat = getattr(plugin.services, "compatibility", None)
    if compat is not None:
        compat.wire_sync_service(plugin.sync_service)
    # Install-time prefix warmup: after a successful install (Epic/GOG/Amazon),
    # the download worker runs the full first-run prefix setup + cloud pull
    # before marking the item complete, so the prefix exists by the first
    # launch (fixes the no-saves-on-first-launch race). Bound to the cloud-save
    # service so the warmup can pull saves once drive_c exists.
    download = getattr(plugin.services, "download", None)
    if download is not None:
        from unifideck.services.download.prefix_warmup import make_prefix_warmup
        download.set_prefix_warmup(
            make_prefix_warmup(getattr(plugin.services, "cloudsave", None)),
        )
    # Resume the download-size warm-up. The walk is a plain asyncio task in
    # THIS process, and the plugin is restarted independently of both Steam
    # and plugin_loader — notably right after a sync, when the user restarts
    # Steam to pick up new shortcuts/artwork. Every resolved size is written
    # through to disk immediately, so a restart only ever loses the couple of
    # lookups in flight; kicking it again here means the remainder finishes
    # instead of waiting for the next sync. No-ops once the cache is full.
    plugin.sync_service.resume_size_backfill()
    await start_async_services(plugin.services)

async def _boot_updater(plugin: Any, decky_plugin_dir: str) -> None:
    """Wire the self-updater service.

    The UpdaterService is lightweight and independent of the
    ServiceContainer — it only needs the EventBus and the path
    to ``package.json`` to read the installed version. Constructed
    separately so a failure here never blocks the rest of boot.

    Starts the 6-hour background polling task so the plugin
    can notify the frontend when a new version is available.
    """
    try:
        from unifideck.services.updater import UpdaterService

        package_json = str(Path(decky_plugin_dir) / "package.json")
        svc = UpdaterService(plugin.bus, package_json)
        plugin._updater_service = svc
        await svc.start_polling()
        logger.info("[Updater] service wired (v%s)", svc.get_current_version())
    except Exception:
        logger.exception("[Updater] failed to wire — update checking disabled")
        plugin._updater_service = None

async def _boot_update_sweep(plugin: Any) -> None:
    """Wire the background game-update sweep.

    Like the self-updater, this only needs the EventBus and the store
    registry, so it is constructed outside the ServiceContainer and a
    failure here never blocks boot — it just means update state falls
    back to being discovered on demand.

    The service is hung off ``plugin`` (not ``plugin.services``) because
    ``DownloadRPCMixin`` reaches it by ``getattr`` on ``self``, the same
    contract ``UpdaterRPCMixin`` uses for ``_updater_service``.
    """
    try:
        from unifideck.services.update_sweep import UpdateSweepService

        svc = UpdateSweepService(plugin.bus, plugin.registry)
        plugin._update_sweep_service = svc
        await svc.start()
    except Exception:
        logger.exception("[UpdateSweep] failed to wire — updates checked on demand")
        plugin._update_sweep_service = None

def _boot_post_sync_reconcile(plugin: Any) -> None:
    """Wire the boot-time post-sync data reconcile.

    The metadata → artwork → compat chain runs as background tasks in this
    process, and this process restarts independently of Steam — most often
    right after a sync, because that is exactly when the user is told to
    restart Steam for new shortcuts and artwork. An interrupted chain used
    to be lost outright: boot checked orphaned *shortcuts* and resumed the
    size backfill, but nothing ever asked whether artwork, metadata or
    compat data were actually complete. One measured session left 111 of
    1242 games with incomplete artwork and 13 (all of Ubisoft, the last
    store signed into) with none at all.

    Constructed outside the ServiceContainer for the same reason as the
    updater and the update sweep: it needs ``plugin.sync_service``, which
    is not a container member. A failure here never blocks boot — the gaps
    simply wait for the next sync, which is the old behaviour.
    """
    try:
        from unifideck.services.post_sync_reconcile import (
            PostSyncReconcileService,
        )

        services = plugin.services
        svc = PostSyncReconcileService(
            plugin.bus,
            plugin.sync_service,
            artwork=getattr(services, "artwork", None),
            metadata=getattr(services, "metadata", None),
            compat=getattr(services, "compatibility", None),
        )
        plugin._post_sync_reconcile_service = svc
        svc.start()
    except Exception:
        logger.exception(
            "[PostSyncReconcile] failed to wire — post-sync gaps will wait "
            "for the next sync",
        )
        plugin._post_sync_reconcile_service = None


def _wire_prefix_bridge(plugin: Any) -> None:
    """Keep ``compatdata`` bridge links in step with the installed games.

    Runs one sweep at boot (repairing prefixes that predate the bridge, and
    pruning links left by an uninstall that happened while the plugin was
    down), then re-sweeps after every sync — which is what makes an uninstall
    disappear from Protontricks without waiting for a restart. Scheduled, not
    awaited: the sweep touches the filesystem and must never delay boot.
    """
    import asyncio

    from unifideck.core.types.events import Events
    from unifideck.services.prefix_bridge import (
        reclaim_redundant_compatdata,
        sync_bridges,
    )
    from unifideck.utils.vdf_compat import resolve_live_steam_root

    def _sweep(*_args: Any, **_kwargs: Any) -> None:
        # Sync handler on purpose — the bus runs these via ``asyncio.to_thread``,
        # which is where this blocking filesystem work belongs.
        try:
            sync_bridges(resolve_live_steam_root())
        except Exception:
            logger.exception("[Unifideck] prefix bridge sweep failed")

    async def _reclaim() -> None:
        """Delete Steam-made prefixes Unifideck initialised and abandoned.

        Boot-only, and deliberately not wired to ``SYNC_COMPLETE``: a game can
        be running by then, and there is no reason to re-scan on every sync.
        Boot is also the one moment nothing can hold a prefix lock yet.

        Needs ``shortcuts.vdf`` for the user-owned veto, so it goes through the
        shortcut service rather than reading the file itself.
        """
        try:
            shortcut_svc = getattr(plugin.services, "shortcut", None)
            shortcuts: dict[str, Any] = {}
            if shortcut_svc is not None:
                await shortcut_svc._load_shortcuts()
                raw = getattr(shortcut_svc, "_shortcuts", {}) or {}
                if isinstance(raw, dict):
                    shortcuts = raw.get("shortcuts", raw)
            steam_root = await asyncio.to_thread(resolve_live_steam_root)
            await asyncio.to_thread(
                reclaim_redundant_compatdata, steam_root, shortcuts,
            )
        except Exception:
            logger.exception("[Unifideck] compatdata reclaim failed")

    try:
        plugin.bus.on(Events.SYNC_COMPLETE, _sweep)
        loop = asyncio.get_running_loop()
        for coro, name in (
            (asyncio.to_thread(_sweep), "prefix-bridge-sweep"),
            (_reclaim(), "compatdata-reclaim"),
        ):
            # Hold a strong reference until completion, else the loop is free
            # to garbage-collect a still-running task (ruff RUF006).
            task = loop.create_task(coro, name=name)
            _BACKGROUND_TASKS.add(task)
            task.add_done_callback(_BACKGROUND_TASKS.discard)
    except Exception:
        logger.exception("[Unifideck] could not wire the prefix bridge")

async def _start_store_background_tasks(plugin: Any) -> None:
    """Kick off per-store background tasks outside the generic Layer-5
    service container (mirrors ``_boot_updater``'s standalone wiring).

    Currently just the Microsoft/Xbox token-refresh poller — see
    ``MicrosoftStore.start_token_refresh_polling``. A failure here must
    never block boot; the store still works via on-demand refresh.
    """
    microsoft = plugin.registry.get("microsoft")
    starter = getattr(microsoft, "start_token_refresh_polling", None)
    if not callable(starter):
        return
    try:
        starter()
    except Exception:
        logger.exception(
            "[Unifideck] failed to start Microsoft token refresh polling",
        )

