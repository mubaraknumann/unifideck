"""RPC mixin classes — the plugin's RPC surface, composed via inheritance.

Each mixin declares a few public coroutines that are mixed into
``class Plugin(...)`` in ``main.py``. ``@auto_wrap_rpc_methods`` then
rewrites every public coroutine to return a typed ``Result[T]`` envelope.

``__all__`` must re-export every mixin composed in ``main.py``. That
agreement is machine-enforced by ``scripts/validate_architecture.py``
(see the ``unifideck-drift-guard`` skill): after adding a mixin, update
both ``main.py`` and this file's imports and ``__all__`` together.

Per-mixin scope:

* ``AccountRPCMixin``         — Steam account-switch detection + migration;
* ``ActionRPCMixin``          — ``unifideck://`` URI dispatch;
* ``AchievementsRPCMixin``    — game achievements + last-session summary;
* ``AuthShortcutsRPCMixin``   — per-store auth-shortcut context + compat tool;
* ``CloudSaveRPCMixin``       — cloud-save pull/push/status;
* ``DownloadRPCMixin``        — download-queue management;
* ``EdgeRPCMixin``            — Microsoft Edge install + readiness;
* ``ExecutableRPCMixin``      — user-settable launch executable per game;
* ``LaunchRPCMixin``          — launch / circuit breaker;
* ``LibraryFacetsRPCMixin``   — per-shortcut facets for native Sort/Filters;
* ``ObservabilityRPCMixin``   — metrics, watchdog, replay;
* ``PlaytimeRPCMixin``        — per-game playtime stats;
* ``StorageRPCMixin``         — storage locations + browseable devices;
* ``StoreRPCMixin``           — auth + login state;
* ``SyncRPCMixin``            — library sync + game info;
* ``UIRPCMixin``              — Steam-UI manipulation + locale;
* ``UpdaterRPCMixin``         — self-update + release notes.
"""

from __future__ import annotations

from .account import AccountRPCMixin
from .achievements import AchievementsRPCMixin
from .action import ActionRPCMixin
from .auth_shortcuts import AuthShortcutsRPCMixin
from .cloud_save import CloudSaveRPCMixin
from .download import DownloadRPCMixin
from .edge import EdgeRPCMixin
from .executable import ExecutableRPCMixin
from .launch import LaunchRPCMixin
from .library_facets import LibraryFacetsRPCMixin
from .observability import ObservabilityRPCMixin
from .playtime import PlaytimeRPCMixin
from .storage import StorageRPCMixin
from .store import StoreRPCMixin
from .sync import SyncRPCMixin
from .ui import UIRPCMixin
from .updater import UpdaterRPCMixin

__all__ = [
    "AccountRPCMixin",
    "AchievementsRPCMixin",
    "ActionRPCMixin",
    "AuthShortcutsRPCMixin",
    "CloudSaveRPCMixin",
    "DownloadRPCMixin",
    "EdgeRPCMixin",
    "ExecutableRPCMixin",
    "LaunchRPCMixin",
    "LibraryFacetsRPCMixin",
    "ObservabilityRPCMixin",
    "PlaytimeRPCMixin",
    "StorageRPCMixin",
    "StoreRPCMixin",
    "SyncRPCMixin",
    "UIRPCMixin",
    "UpdaterRPCMixin",
]
