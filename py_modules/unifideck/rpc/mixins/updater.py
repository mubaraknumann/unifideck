"""Updater RPC mixin for Plugin class.

Exposes plugin self-update operations to the frontend:
- check_plugin_update: version comparison
- get_available_versions: all installable releases

Release notes need no route of their own: ``Release.to_dict`` is
``asdict``, so every row from ``get_available_versions`` already
carries its markdown ``body`` — which is what
``PluginUpdateModal`` renders. A ``get_release_notes(version)``
route existed alongside it with no frontend caller and was
deleted in the audit §1.2 pass.

Installation is handled frontend-side by calling Decky's
``utilities/install_plugin`` directly — this mixin only
provides the metadata needed to populate the UI and construct
the install call.
"""
from __future__ import annotations

import logging
from typing import Any, cast

from unifideck.rpc.errors import RpcError

logger = logging.getLogger(__name__)


class UpdaterRPCMixin:
    """Plugin self-update RPC surface.

    Requires ``self._updater_service`` to be set by
    ``boot_plugin`` before any of these methods are called.
    """

    _updater_service: Any

    async def check_plugin_update(self) -> dict[str, Any]:
        """Check if a newer plugin version is available.

        Returns::

            {
                "available": bool,
                "current": "0.6.1",
                "current_build_id": "0.7.1.g3f9a1c2" | None,
                "latest": {version, name, asset_url, sha256, ...} | None,
            }

        Called on boot, QAM mount, manual trigger, and every
        6 hours by the background poller.
        """
        svc = getattr(self, "_updater_service", None)
        if svc is None:
            raise RpcError("service_unavailable", service="updater")
        return cast("dict[str, Any]", await svc.check_for_update())

    async def get_available_versions(self) -> list[dict[str, Any]]:
        """Return all installable versions from GitHub releases.

        Only versions with downloadable ``.zip`` assets are
        returned. Includes prerelease / dev builds. Sorted
        newest-first.
        """
        svc = getattr(self, "_updater_service", None)
        if svc is None:
            raise RpcError("service_unavailable", service="updater")
        releases = await svc.fetch_releases()
        return [r.to_dict() for r in releases]

    async def force_check_plugin_update(self) -> dict[str, Any]:
        """Like ``check_plugin_update`` but bypasses the 1-hour cache.

        Used only by the explicit "Check for Updates" action —
        the mount-time auto-check and the 6-hour background
        poller keep using the cached path.
        """
        svc = getattr(self, "_updater_service", None)
        if svc is None:
            raise RpcError("service_unavailable", service="updater")
        return cast("dict[str, Any]", await svc.check_for_update(force=True))

    async def force_get_available_versions(self) -> list[dict[str, Any]]:
        """Like ``get_available_versions`` but bypasses the 1-hour cache."""
        svc = getattr(self, "_updater_service", None)
        if svc is None:
            raise RpcError("service_unavailable", service="updater")
        releases = await svc.fetch_releases(force=True)
        return [r.to_dict() for r in releases]

    async def log_update_event(self, stage: str, detail: str) -> None:
        """Record a plugin-install lifecycle event in the Unifideck log.

        The actual download/unzip/reload runs in Decky's loader process
        (which logs to journald), so the updater UI calls this at each
        stage — trigger, ``download_start``, progress milestones,
        ``download_finish``, and errors — to leave a readable install
        trace in ``/home/deck/homebrew/logs/Unifideck/``.

        Args:
            stage: short lifecycle marker (e.g. ``"triggered"``,
                ``"progress"``, ``"download_finish"``, ``"error"``).
            detail: free-form context (version, asset URL, percent, …).
        """
        logger.info("[Updater] install %s — %s", stage, detail)
