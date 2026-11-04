"""services/bootstrap/constructor.py — Public service-construction entry points.

Two functions walking ``_SERVICE_DEFS`` via ``_instantiate_service``,
differing in **scope** and **dependency availability**:
- ``bootstrap_services()`` — full plugin path. Every service in
  the table attempted; each failure isolated on the container.
- ``build_service_subset()`` — reduced path for the out-of-process
  launcher. Only a named subset attempted; registry/cache/pipeline
  passed as None.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .container import ServiceContainer
from .paths import ServicePaths
from .service_defs import _SERVICE_DEFS, _instantiate_service

if TYPE_CHECKING:
    from collections.abc import Iterable

    from unifideck.config import ConfigManager
    from unifideck.core.cache_manager import CacheManager
    from unifideck.event_bus.bus_pipeline import BusPipeline
    from unifideck.event_bus.event_bus import EventBus
    from unifideck.stores import StoreRegistry

logger = logging.getLogger(__name__)


def bootstrap_services(
    bus: EventBus,
    registry: StoreRegistry,
    cache: CacheManager,
    config: ConfigManager,
    pipeline: BusPipeline,
    plugin_dir: str | None = None,
) -> ServiceContainer:
    """Instantiate every Layer-5 service into a ServiceContainer.

    Each service created in an isolated try/except — one failure
    leaves that slot as None without aborting plugin boot
    (degraded mode). Failures logged at WARNING so production
    deployments see them in the Decky log.

    Must be called AFTER ``registry.auto_discover`` — some
    services subscribe to per-store events at construction time.

    Post-loop wiring delegates to ``_wire_browser_monitor`` and
    ``_wire_edge_browser`` because those services depend on the
    just-built ``cdp`` slot — service-defs lambdas only see
    (bus, registry, cache, config, paths, pipeline), with no
    access to a partial container, so we wire them outside the
    main loop.
    """
    logger.info("[Bootstrap] resolving service paths from config")
    paths = ServicePaths.from_config(config, plugin_dir)

    container = ServiceContainer()
    logger.info("[Bootstrap] instantiating %d Layer-5 services", len(_SERVICE_DEFS))

    for def_entry in _SERVICE_DEFS:
        attr = def_entry[0]
        try:
            instance = _instantiate_service(
                def_entry,
                bus=bus,
                registry=registry,
                cache=cache,
                config=config,
                paths=paths,
                pipeline=pipeline,
            )
            setattr(container, attr, instance)
        except Exception as e:
            logger.warning(
                "[Bootstrap] failed to instantiate service '%s': %s",
                attr, e,
            )

    # Post-loop wiring for services that depend on container slots
    # built during the loop above.
    cdp_port = _resolve_cdp_port(config)
    _wire_browser_monitor(container, config, cdp_port)
    _wire_edge_browser(container, config, cdp_port)
    _wire_user_paths_coordinator(container, bus, paths.steam_root)
    return container


def _wire_user_paths_coordinator(
    container: ServiceContainer, bus: EventBus, steam_root: str,
) -> None:
    """Wire the per-user path re-binder (ACCOUNT_SWITCHED + frontend push).

    Isolated so a failure here (never expected) degrades to "paths don't
    re-bind on switch" rather than aborting boot.
    """
    try:
        from unifideck.services.user_paths_coordinator import UserPathsCoordinator
        container.user_paths_coordinator = UserPathsCoordinator(
            bus, container, steam_root,
        )
    except Exception as e:
        logger.warning(
            "[Bootstrap] failed to wire UserPathsCoordinator: %s", e,
        )


def _resolve_cdp_port(config: ConfigManager) -> int:
    """Read the Edge CDP port from config with a defensive fallback.

    The port is shared between ``EdgeBrowser`` (launcher) and
    ``OAuthBrowserMonitor`` (redirect capture) so they target
    the same Edge instance. Misconfigured values fall back to
    the canonical default (9222) instead of raising, since CDP
    port misconfig should degrade auth not block plugin boot.
    """
    try:
        return int(config.get("edge.cdp_port", 9222))
    except Exception:
        return 9222


def _wire_browser_monitor(
    container: ServiceContainer,
    config: ConfigManager,
    cdp_port: int,
) -> None:
    """Build OAuthBrowserMonitor on top of ``container.cdp``.

    Quiet on ``cdp is None`` (the cdp client itself failed to
    instantiate) — leaves ``browser_monitor`` as None so stores
    that require it skip auth gracefully via the injector layer.
    """
    if container.cdp is None:
        logger.info("[bootstrap] browser_monitor skipped — no cdp client")
        return
    try:
        from unifideck.auth.browser import OAuthBrowserMonitor
        container.browser_monitor = OAuthBrowserMonitor(
            cdp_client=container.cdp, config=config,
            edge_cdp_port=cdp_port,
        )
        logger.debug("[bootstrap] browser_monitor wired")
    except Exception as e:
        logger.warning(
            "[bootstrap] failed to wire browser_monitor: %s", e,
        )


def _wire_edge_browser(
    container: ServiceContainer,
    config: ConfigManager,
    cdp_port: int,
) -> None:
    """Build the shared EdgeBrowser instance (the plugin's browser + CDP launcher).

    The PDF spec lists it under ``auth/edge_browser/`` but never
    wires it into a service ; we instantiate it here so the
    injector layer can hand a single shared instance to every
    OAuth store. ``locale_fn`` is a callback (not a value) so
    config changes are picked up at launch time, not at boot.
    """
    try:
        from unifideck.auth.edge_browser import EdgeBrowser
        container.edge_browser = EdgeBrowser(
            cdp_port=cdp_port,
            locale_fn=lambda: str(config.get("ui.locale", "en-US")),
        )
        logger.info("[bootstrap] edge_browser wired")
    except Exception as e:
        logger.warning(
            "[bootstrap] failed to wire edge_browser: %s", e,
        )


def build_service_subset(
    bus: EventBus,
    config: ConfigManager,
    services: Iterable[str],
) -> ServiceContainer:
    """Construct a named subset of Layer-5 services.

    Used by ``launcher/bootstrap.py`` for the out-of-process
    launcher's reduced graph (shortcut, proton, cloudsave,
    launch_history typically). Registry / cache / pipeline passed
    as None to ``_instantiate_service``; services whose lambdas
    dereference those components will fail — caller's
    responsibility to only request compatible services.

    Unknown service names are logged + skipped.
    """
    paths = ServicePaths.from_config(config)
    container = ServiceContainer()

    # Map requested names to their definition row
    def_map = {row[0]: row for row in _SERVICE_DEFS}

    for name in services:
        if name not in def_map:
            logger.warning("[BootstrapSubset] unknown service requested: %s", name)
            continue

        try:
            instance = _instantiate_service(
                def_map[name],
                bus=bus,
                registry=None,
                cache=None,
                config=config,
                paths=paths,
                pipeline=None,
            )
            setattr(container, name, instance)
        except Exception as e:
            logger.warning(
                "[BootstrapSubset] failed to instantiate service '%s': %s",
                name, e,
            )

    return container
