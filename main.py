"""Decky Loader plugin entry point — Unifideck.

This module is what Decky Loader imports at plugin load time. It
declares the ``Plugin`` class that Decky instantiates ; everything
else (services, stores, event bus, RPC handlers) is wired up by
``unifideck.bootstrap.boot.boot_plugin`` from inside ``_main()``.

The module deliberately stays thin :

    * No business logic — the plugin class only owns the lifecycle
      hooks (``_main`` / ``_unload`` / ``_validate_config``).
    * No service construction at import time — that's the job of
      ``boot_plugin``, called once Decky has signalled the plugin is
      mounted and the event loop is alive.
    * No top-level RPC method bodies — RPC surface comes from the
      mixins composed below (count is machine-checked by
      ``scripts/validate_architecture.py``, not restated here) ;
      ``@auto_wrap_rpc_methods``
      decorates each public coroutine so it returns a typed
      ``Result`` envelope instead of raising.

The layered architecture flows downward from this entry (see
``docs/architecture.md`` for the authoritative layer diagram; do not
restate a layer count here). This file references only the RPC mixins
and the bootstrap helpers ; it never imports a service or store directly.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from unifideck.event_bus.bus_pipeline import BusPipeline

DECKY_PLUGIN_DIR = os.environ.get(
    "DECKY_PLUGIN_DIR", str(Path(__file__).parent),
)

# Writable per-plugin runtime location for caches, queues, and any
# state we need to persist across plugin reloads. Decky guarantees
# DECKY_PLUGIN_RUNTIME_DIR is writable by the plugin process and
# survives plugin updates. The fallback is the XDG-compliant data
# directory used when running outside Decky (tests, dev shells).
# NEVER write into DECKY_PLUGIN_DIR — that location is install-managed
# and read-only on normal user installs.
DECKY_PLUGIN_RUNTIME_DIR = os.environ.get(
    "DECKY_PLUGIN_RUNTIME_DIR",
    str(Path("~/.local/share/unifideck").expanduser()),
)

sys.path.insert(0, str(Path(DECKY_PLUGIN_DIR) / "py_modules"))
# ``py_modules/_vendor/`` holds vendored modules whose names would
# shadow stdlib/library modules at the top of ``py_modules``.
# Currently only ``typing_extensions.py`` lives there — mypy 1.10+
# refuses to handle a top-level user module shadowing the
# ``typing_extensions`` PyPI package, so we hide it under a subdir
# and place that subdir on sys.path so vendored consumers
# (``urllib3``, ``packaging``, ``attrs``, …) still resolve their
# ``from typing_extensions import …`` imports correctly.
sys.path.insert(0, str(Path(DECKY_PLUGIN_DIR) / "py_modules" / "_vendor"))

# E402 noqa on the unifideck imports below: ``sys.path.insert``
# MUST run before these so Python can resolve the package — Decky
# Loader injects the plugin's ``py_modules`` directory on sys.path
# at load time, but only AFTER this module is imported. Moving the
# imports above ``sys.path.insert`` would raise ImportError on
# every plugin boot. The pattern is canonical for Decky plugins.
from unifideck.config.user_config_path import resolve_user_config_path  # noqa: E402
from unifideck.rpc import auto_wrap_rpc_methods  # noqa: E402
from unifideck.rpc.mixins.account import AccountRPCMixin  # noqa: E402
from unifideck.rpc.mixins.achievements import AchievementsRPCMixin  # noqa: E402
from unifideck.rpc.mixins.action import ActionRPCMixin  # noqa: E402
from unifideck.rpc.mixins.auth_shortcuts import AuthShortcutsRPCMixin  # noqa: E402
from unifideck.rpc.mixins.cloud_save import CloudSaveRPCMixin  # noqa: E402
from unifideck.rpc.mixins.download import DownloadRPCMixin  # noqa: E402
from unifideck.rpc.mixins.edge import EdgeRPCMixin  # noqa: E402
from unifideck.rpc.mixins.executable import ExecutableRPCMixin  # noqa: E402
from unifideck.rpc.mixins.game_env import GameEnvRPCMixin  # noqa: E402
from unifideck.rpc.mixins.launch import LaunchRPCMixin  # noqa: E402
from unifideck.rpc.mixins.library_facets import LibraryFacetsRPCMixin  # noqa: E402
from unifideck.rpc.mixins.observability import ObservabilityRPCMixin  # noqa: E402
from unifideck.rpc.mixins.optiscaler import OptiScalerRPCMixin  # noqa: E402
from unifideck.rpc.mixins.playtime import PlaytimeRPCMixin  # noqa: E402
from unifideck.rpc.mixins.storage import StorageRPCMixin  # noqa: E402
from unifideck.rpc.mixins.store import StoreRPCMixin  # noqa: E402
from unifideck.rpc.mixins.sync import SyncRPCMixin  # noqa: E402
from unifideck.rpc.mixins.ui import UIRPCMixin  # noqa: E402
from unifideck.rpc.mixins.updater import UpdaterRPCMixin  # noqa: E402

logger = logging.getLogger(__name__)


@auto_wrap_rpc_methods
class Plugin(
    ObservabilityRPCMixin,
    DownloadRPCMixin,
    StorageRPCMixin,
    LaunchRPCMixin,
    StoreRPCMixin,
    AuthShortcutsRPCMixin,
    EdgeRPCMixin,
    ExecutableRPCMixin,
    OptiScalerRPCMixin,
    GameEnvRPCMixin,
    SyncRPCMixin,
    LibraryFacetsRPCMixin,
    UIRPCMixin,
    CloudSaveRPCMixin,
    PlaytimeRPCMixin,
    ActionRPCMixin,
    AccountRPCMixin,
    AchievementsRPCMixin,
    UpdaterRPCMixin,
):
    """The Decky Loader plugin class.

    Decky Loader instantiates this class (no constructor argument),
    keeps the instance for the lifetime of the plugin, and calls
    the four lifecycle hooks below in this order :

        1. ``_main()``                 — once, at plugin mount.
        2. ``_validate_config()``      — once, after ``_main`` returned.
        3. ``_build_eventbus_pipeline()`` — once, also after ``_main``.
        4. ``_unload()``               — once, at plugin unmount.

    Plus ``_register_caches()``, called by ``_main`` indirectly via
    ``boot_plugin``. Splitting cache registration from the rest of
    the boot keeps the cache lifecycle owned by ``_register_caches``,
    which makes it possible to refresh caches without a full reboot
    (used in dev mode and for the user-triggered "rebuild caches"
    action).

    The decorator ``@auto_wrap_rpc_methods`` rewrites every public
    coroutine inherited from the mixins so it returns a typed
    ``Result[T]`` envelope (success / error code / payload). The
    raw coroutine never reaches the JS side — Decky's RPC bridge
    only sees serialised envelopes, which keeps the contract with
    the frontend stable across backend refactors.
    """

    async def _main(self) -> None:
        """Decky lifecycle entry — wire the plugin to its services.

        Imports ``boot_plugin`` lazily to keep the module's import
        graph minimal (the bootstrap subpackage pulls in the entire
        Layer 5 surface, which we don't want loaded until the event
        loop is alive).
        """
        from unifideck.bootstrap.boot import boot_plugin

        await boot_plugin(
            self,
            decky_plugin_dir=DECKY_PLUGIN_DIR,
            decky_runtime_dir=DECKY_PLUGIN_RUNTIME_DIR,
            user_config_path_resolver=resolve_user_config_path,
        )

    async def _validate_config(self) -> None:
        """Cross-check the user's config against the bundled defaults.

        Runs after ``_main`` so the bus and config manager are
        already wired. Stores two pieces of state on the plugin
        instance : ``_config_validation_result`` (the validation
        report) and ``_config_degraded`` (a boolean saying the config
        is partially broken but the plugin can still operate).

        Both are **diagnostics only** — they reach a human through the
        support bundle's ``config_validation`` block
        (``rpc/mixins/observability.py``), not through any UI. Neither
        changes plugin behaviour: there is no "degraded mode". This
        docstring described a frontend warning banner that has never
        existed (audit §1.2/§1.3); the flag was in fact read by nothing
        at all until the bundle fold.
        """
        from unifideck.bootstrap.boot import _resolve_defaults_path
        from unifideck.config.startup import validate_config_at_startup

        # Bundled defaults live in ``defaults/config.json`` in source,
        # but the Decky CLI build flattens that to ``config.json`` at
        # the install root. ``_resolve_defaults_path`` picks whichever
        # layout this install actually has so we don't go into degraded
        # mode just because the install was packaged with the CLI.
        defaults_path = _resolve_defaults_path(DECKY_PLUGIN_DIR)
        (
            self._config_validation_result,
            self._config_degraded,
        ) = await validate_config_at_startup(
            bus=self.bus,
            config=self.config,
            defaults_path=defaults_path,
            user_config_path=self._user_config_path,
        )

    async def _build_eventbus_pipeline(self) -> BusPipeline:
        """Construct the bus pipeline — supervisor + replay buffer + handlers.

        Returns the ``BusPipeline`` so ``boot_plugin`` can hand it
        to whichever component needs to subscribe at boot time
        (typically the cache invalidator and the persistence
        services).
        """
        from unifideck.bootstrap.pipeline_factory import (
            build_eventbus_pipeline,
        )

        return await build_eventbus_pipeline(self)

    async def _unload(self) -> None:
        """Decky lifecycle exit — symmetric cleanup for ``_main``.

        Walks the dependency graph in reverse construction order
        (services → stores → bus → cache → config), drains every
        in-flight task with a deadline, then closes file handles
        and HTTP sessions. Called by Decky on plugin unmount or
        when the user disables the plugin from the UI.
        """
        from unifideck.bootstrap.teardown import unload_plugin

        await unload_plugin(self)

    def _register_caches(self) -> None:
        """Register the canonical cache namespaces with the cache manager.

        Synchronous because cache registration only mutates an
        in-memory registry — no I/O — so it can run before the
        event loop is fully spun up. The actual cache backends
        (disk / memory) are constructed lazily by ``CacheManager``
        on first ``get`` / ``set``.
        """
        from unifideck.bootstrap.cache_registry import (
            register_default_caches,
        )

        register_default_caches(self.cache)
