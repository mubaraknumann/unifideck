"""services/proton_service.py — Proton provisioning for non-Steam games.

One responsibility: on plugin load (``start``), background-install the
*latest* GE-Proton released online (``ge_installer.ensure_latest_ge``)
so games default to the newest GE-Proton without blocking any launch.
Best-effort — offline/failure leaves the launcher to fall back to
Proton Experimental at launch time.

This service used to have a second job: forcing a per-store compat tool
into Steam's ``config.vdf`` on ``GAME_INSTALLED``. That path was removed
because it could never run, for four independent reasons — the event had
no live emitter, its payload key (``game_id``) did not match what the
handler read (``app_id``), the per-store tool table was empty for every
store by design, and in the plugin the path was pointed at
``localconfig.vdf`` while ``CompatToolMapping`` lives in
``config/config.vdf``. Proton selection belongs to the launcher
(``launcher/proton/``), which picks the latest GE-Proton and clears
Force-Compat before ``RunGame``; forcing a tool here would pin every
game to it and defeat that policy. ``ProtonToolsManager`` in
``compatibility/proton_helpers.py`` remains the one live
``CompatToolMapping`` writer.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from unifideck.event_bus.event_bus import EventBus

logger = logging.getLogger(__name__)


class ProtonService:
    """Keeps the latest GE-Proton installed for the launcher to pick up."""

    def __init__(self, bus: EventBus) -> None:
        """Store refs. No bus subscriptions — this service only emits."""
        self._bus = bus
        self._ge_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Background-install the latest GE-Proton on plugin load.

        Non-blocking: the (potentially large) GitHub fetch + extract
        runs off the event loop via ``ge_installer.ensure_latest_ge``
        so booting and game launches are never gated on it. Failures
        (offline / GitHub down) are swallowed — the launcher falls
        back to Proton Experimental at launch time. The task reference
        is retained so it isn't garbage-collected mid-flight.
        """
        self._ge_task = asyncio.create_task(self._ensure_latest_ge())

    async def _ensure_latest_ge(self) -> None:
        """Background-install the latest GE-Proton, toasting a new install.

        Stays silent when the latest is already present (the common
        case): the install/ready toasts fire only when a download
        actually happens, so a normal boot is quiet.
        """
        try:
            from unifideck.launcher.proton.infrastructure import (
                external_ge,
                ge_installer,
            )

            tag = await asyncio.to_thread(ge_installer.get_latest_ge_tag)
            external = await asyncio.to_thread(external_ge.find_external_ge_proton)
            if external:
                _path, tool_id, current_ver = external
                logger.info(
                    "[ProtonService] Externally managed GE-Proton detected (%s, ver: %s).",
                    tool_id, current_ver or "unknown",
                )
                if (
                    tag
                    and current_ver
                    and external_ge.is_ge_outdated(current_ver, tag)
                ):
                    logger.info(
                        "[ProtonService] Externally managed GE-Proton is outdated (%s < %s). Notifying user.",
                        current_ver, tag,
                    )
                    await self._emit_proton_toast(
                        "toasts.launcher.externalProtonOutdatedTitle",
                        "toasts.launcher.externalProtonOutdatedBody",
                        tag,
                    )
                if (
                    tag
                    and current_ver
                    and external_ge.is_ge_sufficiently_fresh(current_ver, tag)
                ):
                    logger.info(
                        "[ProtonService] Externally managed GE-Proton is sufficiently fresh (%s, latest: %s). "
                        "Skipping background download.",
                        current_ver, tag,
                    )
                    return

            if not tag:
                logger.info(
                    "[ProtonService] latest GE-Proton unavailable "
                    "(offline?); launcher will use Proton Experimental",
                )
                return
            if await asyncio.to_thread(ge_installer.is_valid_ge_install, tag):
                logger.info("[ProtonService] latest GE-Proton already installed: %s", tag)
                return
            # A download is needed → tell the user it's happening.
            await self._emit_proton_toast(
                "toasts.launcher.installingProton",
                "toasts.launcher.attemptingInstall",
                tag,
            )
            result = await asyncio.to_thread(ge_installer.ensure_latest_ge)
        except Exception:
            logger.exception("[ProtonService] background GE-Proton install failed")
            return
        if result:
            _path, installed_tag = result
            logger.info("[ProtonService] latest GE-Proton ready: %s", installed_tag)
            await self._emit_proton_toast(
                "toasts.launcher.protonReadyTitle",
                "toasts.launcher.protonReadyBody",
                installed_tag,
            )
        else:
            logger.warning(
                "[ProtonService] GE-Proton install failed; "
                "launcher will fall back to Proton Experimental",
            )

    async def _emit_proton_toast(
        self, title_key: str, body_key: str, version: str,
    ) -> None:
        """Best-effort LAUNCHER_STAGE toast for GE-Proton install progress."""
        try:
            from unifideck.launcher.rpc import emit_stage

            await emit_stage(
                self._bus,
                i18n_title_key=title_key,
                i18n_key=body_key,
                game_title="",
                i18n_params={"version": version},
                priority="normal",
            )
        except Exception:
            logger.warning("[ProtonService] proton toast emit failed", exc_info=True)

    async def stop(self) -> None:
        """Cancel the background GE-Proton install if still running."""
        if self._ge_task is not None and not self._ge_task.done():
            self._ge_task.cancel()
