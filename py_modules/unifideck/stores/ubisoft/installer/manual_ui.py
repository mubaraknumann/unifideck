"""
UPC manual-UI driver — prepares the prefix and records what UPC installed.

UPC has no silent-install flag, so the install is the user pressing through
the wizard. UPC itself is opened by the *frontend* via Steam's ``RunGame`` (a
backend-spawned process has no gamescope session and would never render in
Gaming Mode), which leaves this module with the two ends of the operation:
inject the session and set the prefix up beforehand, and register what landed
afterwards.

The watching in between — poll, give-up watchdogs, completion, progress ticks —
is no longer Ubisoft's. It lives in
:mod:`unifideck.stores.shared.wrapper_install.watch`, shared with Battle.net,
and the Ubisoft-specific "has a game appeared" half is
:class:`~.manual_ui_poll.UbisoftInstallProbe`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from unifideck.core.types import InstallResult
from unifideck.launcher.proton.handlers.wrapper_clients import kill_client
from unifideck.stores.shared.wrapper_install import watch_manual_install
from unifideck.stores.ubisoft.config import UbisoftConfig
from unifideck.stores.ubisoft.id_map import UbisoftIdMap
from unifideck.stores.ubisoft.library import UbisoftLibrary
from unifideck.stores.ubisoft.session import UbisoftSession

from . import registry as _reg
from .manual_ui_poll import STORE_ID, UbisoftInstallProbe

logger = logging.getLogger(__name__)

# Bound on the cancel-path client stop. Runs synchronously during the
# ``CancelledError`` unwind — there is no awaiting anything there, a thread
# hop would be cancelled out from under us — so it must stay short.
_CANCEL_STOP_TIMEOUT_S = 5.0

class _ManualUiInstaller:
    """Prepares the prefix for a UPC install and registers the result."""

    def __init__(
        self,
        config: UbisoftConfig,
        library: UbisoftLibrary,
        id_map: UbisoftIdMap,
        session: UbisoftSession,
        active_install_pids: dict[str, int],
    ) -> None:
        """Initialize the instance."""
        self._config = config
        self._library = library
        self._id_map = id_map
        self._session = session
        self._active_install_pids = active_install_pids

    async def install_via_upc_ui(
        self,
        *,
        game_id: str,
        game_name: str | None,
        prefix_path: str,
        progress_cb: Callable[[dict[str, Any]], Awaitable[None]] | None,
        install_path: str | None,
        on_ready: Callable[[], Awaitable[None]] | None = None,
    ) -> InstallResult:
        """Drive a UPC install whose window is opened by the frontend.

        Blocks for the whole install. That is the wrapper-store contract, not
        an implementation detail: the caller marks the game installed when this
        returns, so returning at prefix-setup time would put a Play button on a
        game with no files (which is exactly what Battle.net used to do).
        """
        logger.info(
            "[UbisoftInstaller] preparing manual UPC install for %s", game_id,
        )
        self._session.inject_into_prefix(prefix_path)
        probe = UbisoftInstallProbe(
            self._prepared_install_base(install_path), prefix_path,
        )
        try:
            install_dir = await watch_manual_install(
                probe=probe,
                prefix=prefix_path,
                progress_cb=progress_cb,
                on_ready=on_ready,
            )
        except asyncio.CancelledError:
            # Explicit cancel from the download queue is the ONLY path that
            # closes UPC. Completion must NOT: it is inferred from the install
            # dir's size holding steady, so a mid-download pause (UPC verifying
            # a chunk, a network stall, a phase transition) can look "done" —
            # and killing UPC then interrupts a still-running install. Users
            # reported watching UPC close mid-install and resume on reopen.
            kill_client(
                STORE_ID, prefix_path, timeout=_CANCEL_STOP_TIMEOUT_S,
            )
            raise
        finally:
            self._active_install_pids.pop(game_id, None)
            # Capture a fresh/rotated UPC token from this prefix back to the
            # auth prefix on every exit path (incl. the cancel unwind).
            # Otherwise a token UPC rotated this run is lost and the next
            # install/launch injects the stale auth-prefix credential → UPC
            # opens logged out. capture() is guarded (acts only on a valid,
            # non-logged-out credential), so a half-written session is ignored.
            self._capture_and_propagate_session(prefix_path)
        if not install_dir:
            return InstallResult(
                success=False,
                store=STORE_ID,
                game_id=game_id,
                error="no_install_detected",
            )
        return await self._finalize_manual_install(
            game_id=game_id,
            game_name=game_name,
            install_dir=install_dir,
            prefix_path=prefix_path,
        )

    def _prepared_install_base(self, install_path: str | None) -> str:
        """The directory we asked UPC to install into, created if absent.

        Created before the probe baselines it: a directory that springs into
        existence mid-watch would make its first entry look like a new game.
        """
        install_base = install_path or self._config.default_install_base_expanded
        Path(install_base).mkdir(parents=True, exist_ok=True)
        return install_base

    def _capture_and_propagate_session(
        self,
        prefix_path: str,
    ) -> None:
        """Capture and propagate session."""
        if self._session.capture(prefix_path):
            self._session.propagate_all_to_all()

    async def _finalize_manual_install(
        self,
        *,
        game_id: str,
        game_name: str | None,
        install_dir: str,
        prefix_path: str,
    ) -> InstallResult:
        """Finalize manual install."""
        exe = self._library.find_game_executable(install_dir)
        await self._library.write_install_marker(
            space_id=game_id,
            install_path=install_dir,
            executable=exe or "",
            game_title=game_name or "",
        )
        final_size = _reg.get_directory_size(install_dir)
        logger.info(
            "[UbisoftInstaller] manual install complete: %s (%.0f MB)",
            install_dir,
            final_size / (1024 * 1024),
        )
        await self._seed_launch_id(game_id, prefix_path, game_name)
        return InstallResult(
            success=True,
            store=STORE_ID,
            game_id=game_id,
            install_path=install_dir,
            size_bytes=final_size,
            metadata={"executable": exe},
        )

    def _launch_id_ok(self, game_id: str) -> bool:
        """Whether a usable (non-zero) uplay launch id resolves for a game."""
        resolved = self._id_map.resolve_launch_id(game_id)
        return bool(resolved) and str(resolved) != "0"

    async def _seed_launch_id(
        self,
        game_id: str,
        prefix_path: str,
        game_name: str | None,
    ) -> None:
        """Make sure a uplay launch id is resolvable right after install.

        The launcher builds ``uplay://launch/{id}/0`` from
        ``ubisoft_id_map.json``; with no resolvable id, Play can't launch the
        game directly (it opens UPC bare). UPC writes the config files we read
        from asynchronously, so the configuration refresh can miss on the
        first pass — fall back to the Wine registry, then a unifiDB name
        lookup (mirrors the library detector's ``_auto_resolve_missing_id``).
        Best-effort: a failure here only costs direct-launch, never the
        install itself.
        """
        try:
            await self._id_map.refresh_from_configurations(game_id)
        except Exception as e:
            logger.warning(
                "[UbisoftInstaller] id_map refresh after install failed: %s",
                e,
            )
        if self._launch_id_ok(game_id):
            return
        source = "registry"
        reg_id = self._id_map.extract_game_id_from_registry(prefix_path)
        if not reg_id and game_name:
            source = "name_db"
            with contextlib.suppress(Exception):
                reg_id = await self._id_map.lookup_game_id_by_name(game_name)
        if reg_id:
            self._id_map.set_connect_id(
                game_id,
                reg_id,
                source,
                {
                    "install_id": reg_id,
                    "launch_id": reg_id,
                    "name": game_name or "",
                },
            )
            logger.info(
                "[UbisoftInstaller] seeded uplay launch id for %s: %s",
                game_id, reg_id,
            )
            return
        logger.warning(
            "[UbisoftInstaller] could not resolve a uplay launch id for %s — "
            "Play will open Ubisoft Connect until a library sync seeds it",
            game_id,
        )
