"""Core sub-package — cross-cutting primitives used everywhere.

The ``core/`` sub-package holds the **foundation layer** —
primitives that every higher-level package (services, stores,
rpc, event_bus) depends on:

* ``types/``           — typed records (``Game``, ``Result``
  family, ``Events`` enum, etc.);
* ``io/``              — async file I/O helpers + atomic
  write primitive;
* ``binaries/``             — bundled-CLI resolver + version
  signature checks;
* ``net/``             — TLS / SSL helpers;
* ``cache_manager``    — TTL-keyed cache with persistent
  optional backing;
* ``manifest``         — install-manifest reader/writer
  (per-game state on disk);
* ``metrics_collector`` — plugin-wide counters/timers;
* ``sync_service``     — multi-store library-sync
  orchestrator;
* ``exe_finder``       — heuristic executable resolver for
  installed games;
* ``paths``            — well-known path helpers.

This top-level ``__init__.py`` re-exports a curated subset
of the most-used names so consumers can ``from unifideck.core
import Game, Result, Events`` without knowing the internal
split.
"""
from __future__ import annotations

from .cache_manager import CacheManager
from .types import (
    AuthResult,
    CLITool,
    DownloadResult,
    Events,
    Game,
    InstallResult,
    Result,
    StoreError,
    StoreInfo,
    StoreStatus,
    SyncResult,
)

__all__ = [
    "AuthResult",
    "CLITool",
    "CacheManager",
    "DownloadResult",
    "Events",
    "Game",
    "InstallResult",
    "Result",
    "StoreError",
    "StoreInfo",
    "StoreStatus",
    "SyncResult",
]
