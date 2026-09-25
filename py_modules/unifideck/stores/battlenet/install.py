"""Battle.net install orchestration — place the prefix, then watch the client.

py_modules/unifideck/stores/battlenet/install.py

Nothing here downloads a game. Battle.net is a wrapper store: the vendor
client inside the prefix owns the download, and the install is a user click
in its window. This module owns the two ends of that — what has to be true
before the click, and knowing when the click's work is done.

**Placement** is the "before". The game installs inside the prefix, so the
prefix's location is the game's location, and Wine derives ``C:``'s free space
from the filesystem under ``drive_c``. Battle.net originally ignored the
storage location the user picked, and the symptom was not a game in the wrong
folder: the client refused an 83 GB install for lack of space, quoting the
45 GB internal drive, while the SD card the user had chosen had 164 GB free.

**Waiting** is the "after", and used to be missing entirely. ``install``
returned success the moment the prefix was cloned, so the download worker
marked the game installed and the tile showed a Play button before the user had
even opened the client. It now blocks on the shared wrapper-store watcher —
the same one Ubisoft uses — and returns the real install directory.

Split out of ``store.py`` when that file hit its size cap. The seam is the
one Ubisoft already uses (``stores/ubisoft/installer/``); the placement policy
lives in ``stores/shared/prefix_placement`` and the watching in
``stores/shared/wrapper_install``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from unifideck.core.types.results import InstallResult
from unifideck.launcher.proton.handlers.wrapper_clients import kill_client
from unifideck.launcher.wrapper_client_cache import capture_client_cache
from unifideck.launcher.wrapper_session_specs import spec_for
from unifideck.stores.shared.prefix_forensics import (
    preserve_vendor_logs,
    salvage_path,
)
from unifideck.stores.shared.prefix_placement import (
    BeforeRemove as CaptureSession,
)
from unifideck.stores.shared.prefix_placement import (
    cleanup_abandoned_prefix,
    reset_for_fresh_install,
    resolve_prefix_target,
)
from unifideck.stores.shared.wrapper_install import watch_manual_install
from unifideck.stores.shared.wrapper_install.watch import install_alive

from . import agent_status, paths
from . import install_state as install_state_mod
from . import library as library_mod
from .install_watch import BattlenetInstallProbe
from .ownership import read_catalog

if TYPE_CHECKING:
    from .id_map import BattlenetIdMap
    from .prefix import BattlenetPrefixManager

logger = logging.getLogger(__name__)

STORE_ID = "battlenet"
LABEL = "Battlenet"

ProgressCb = Callable[[dict[str, Any]], Awaitable[None]] | None
OnReady = Callable[[], Awaitable[None]] | None

# Bound on the cancel-path client stop. Runs synchronously during the
# ``CancelledError`` unwind, so it must stay short.
_CANCEL_STOP_TIMEOUT_S = 5.0


@dataclass(frozen=True, slots=True)
class PreparedInstall:
    """A prefix ready for the client to install into."""

    prefix: Path
    is_update: bool = False


def holds_ready_install(prefix: Path) -> bool:
    """Whether a prefix's client state reports at least one ready install.

    Synchronous so it can be handed straight to a worker thread, and the
    only thing standing between a failed install and a deleted game —
    ``product.db`` plus ``aggregate.json`` are what the client itself
    consults, so this is the strongest evidence available.
    """
    drive_c = paths.drive_c(prefix)
    if drive_c is None:
        return False
    state = install_state_mod.read_install_state(drive_c, prefix)
    return any(game.is_ready for game in state.values())


def _failed(game_id: str, error: str, code: str) -> InstallResult:
    return InstallResult(
        success=False,
        game_id=game_id,
        store=STORE_ID,
        error=error,
        error_code=code,
    )


class BattlenetInstaller:
    """Prepares a game's prefix and hands the download to the client."""

    def __init__(
        self,
        prefixes: BattlenetPrefixManager,
        id_map: BattlenetIdMap,
        capture_session: CaptureSession | None = None,
    ) -> None:
        self._prefixes = prefixes
        self._id_map = id_map
        # Injected rather than imported so the installer keeps knowing nothing
        # about the bus or the store; the store passes its own hook.
        self._capture_session = capture_session

    async def install(
        self,
        game_id: str,
        install_path: str | None = None,
        *,
        progress_cb: ProgressCb = None,
        on_ready: OnReady = None,
    ) -> InstallResult:
        """Place the prefix, open the client, and wait for the game to land.

        Blocks for the whole install. That is the wrapper-store contract: the
        download worker marks the game installed when this returns, so
        returning at prefix-placement time — which is what this used to do —
        put a Play button on a game with no files.
        """
        prepared = await self.prepare(game_id, install_path)
        if isinstance(prepared, InstallResult):
            return prepared
        return await self._watch(game_id, prepared, progress_cb, on_ready)

    async def prepare(
        self, game_id: str, install_path: str | None = None,
    ) -> PreparedInstall | InstallResult:
        """Everything that must be true before the user clicks Install.

        Separate from the wait so the placement rules stay testable without
        standing up a fake vendor client. Returns an :class:`InstallResult` —
        always a failure — when the install cannot proceed at all.

        ``--exec="install <FAMILY>"`` does **not** start a download; that was
        measured against the current client with a known-good family code. The
        install is a user click inside the client, exactly as it is for
        Ubisoft, so all that can be done here is put the prefix in the right
        place.
        """
        # The install run opens the client on the game's page via
        # ``--exec="launch <FAMILY>"``, so a missing family fails the install
        # the same way it fails a launch — and silently. Resolve it here
        # rather than letting the launcher discover the gap.
        if not await self.ensure_family(game_id):
            return _failed(
                game_id,
                "Unifideck doesn't know Battle.net's code for this game — "
                "re-sync your library",
                "family_unknown",
            )
        if not self._prefixes.auth_ready():
            return _failed(game_id, *self._auth_not_ready_reason())
        target = await self._place_prefix(game_id, install_path)
        prefix = await self._prefixes.create_game_prefix(game_id, target)
        if prefix is None:
            await self.cleanup_abandoned(game_id, target)
            return _failed(
                game_id,
                "Could not prepare the game's Battle.net prefix — close "
                "the Battle.net window and try again",
                "prefix_clone_failed",
            )
        return PreparedInstall(prefix=prefix)

    async def update(
        self,
        game_id: str,
        *,
        progress_cb: ProgressCb = None,
        on_ready: OnReady = None,
    ) -> InstallResult:
        """Same wait, existing prefix.

        An update must NOT go through :meth:`prepare`. That path resets the
        prefix for a fresh install, and for this store the prefix *is* the
        game — resetting it would delete what the user is trying to update.
        """
        prefix = self._id_map.resolve_prefix(game_id)
        if prefix is None:
            return _failed(
                game_id,
                "No recorded prefix for this game",
                "prefix_unknown",
            )
        return await self._watch(
            game_id,
            PreparedInstall(prefix=prefix, is_update=True),
            progress_cb,
            on_ready,
        )

    async def _watch(
        self,
        game_id: str,
        prepared: PreparedInstall,
        progress_cb: ProgressCb,
        on_ready: OnReady,
    ) -> InstallResult:
        """Wait for the client to finish, then record what it produced."""
        probe = BattlenetInstallProbe(game_id, prepared.prefix)
        try:
            install_dir = await watch_manual_install(
                probe=probe,
                prefix=prepared.prefix,
                progress_cb=progress_cb,
                on_ready=on_ready,
            )
        except asyncio.CancelledError:
            # An explicit cancel is the only path that closes the client;
            # completion deliberately leaves it open. Cleanup is guarded by
            # ``holds_ready_install``, so a cancel that races a finished
            # download keeps the prefix — which, for this store, is the game.
            kill_client(
                STORE_ID, prepared.prefix, timeout=_CANCEL_STOP_TIMEOUT_S,
            )
            await self._abandon(game_id, prepared, probe.started_at)
            raise
        if not install_dir:
            await self._abandon(game_id, prepared, probe.started_at)
            return _failed(
                game_id,
                "The install was never finished in Battle.net",
                "no_install_detected",
            )
        result = self._record(game_id, prepared, probe, install_dir)
        # This prefix survives, but the template is what the *next* install
        # clones, so a successful install is the cheapest moment there is to
        # top it up. Costs one marker read when there is nothing new.
        await self._capture_client_cache(prepared.prefix, probe.started_at)
        return result

    async def _abandon(
        self, game_id: str, prepared: PreparedInstall, since: float,
    ) -> None:
        """Reclaim a prefix we created that never received a game.

        Skipped for an update: that prefix predates this operation and holds
        the user's existing install, so it was never ours to reclaim.

        ``since`` is when this attempt's client started, and it is what lets
        the cleanup tell the Agent's work in *this* prefix from the inherited
        logs the clone arrived with.
        """
        if prepared.is_update:
            return
        await self.cleanup_abandoned(game_id, prepared.prefix, since=since)

    def _record(
        self,
        game_id: str,
        prepared: PreparedInstall,
        probe: BattlenetInstallProbe,
        install_dir: str,
    ) -> InstallResult:
        """Persist what the client installed and report it upstream.

        The id map is written here because this is the only moment that sees
        all of it. Without it ``get_installed_path`` and ``get_game_size``
        fall through to re-reading ``product.db`` and returning the *first*
        game in the prefix rather than the one asked for.
        """
        row = probe.row()
        exe = row.host_exe_path if row else None
        size = int((row.total_bytes if row else 0) or 0)
        # ``install_dir`` may be the provisional answer the watcher started
        # from — derived from the executable before ``product.db`` carried a
        # path. Now that the install is finished, product.db is authoritative.
        final_dir = (row.host_install_path if row else None) or install_dir
        self._id_map.merge(
            game_id,
            install_path=final_dir,
            exe_path=exe,
            total_bytes=size or None,
        )
        logger.info(
            "[Battlenet] install complete: %s (%.1f GB)",
            final_dir, size / (1024**3),
        )
        return InstallResult(
            success=True,
            game_id=game_id,
            store=STORE_ID,
            install_path=final_dir,
            size_bytes=size,
            metadata={
                "phase": "manual",
                "prefix": str(prepared.prefix),
                "executable": exe,
            },
        )

    async def _place_prefix(
        self, game_id: str, install_path: str | None,
    ) -> Path:
        """Resolve where this game's prefix goes and clear the way for it."""
        target = resolve_prefix_target(
            STORE_ID, game_id, install_path,
            self._prefixes.game_prefix(game_id),
        )
        # Every Install rebuilds the prefix, so clear both the previously
        # recorded location (which differs once the user picks a new disk)
        # and the target itself — capturing each one's session on the way out,
        # because a prefix whose client has run holds a newer token than the
        # auth prefix and deleting it uncaptured is what makes the next
        # install open signed-out.
        await reset_for_fresh_install(
            self._id_map.resolve_prefix(game_id),
            target,
            self._prefixes.remove_game_prefix,
            label=LABEL,
            before_remove=self._capture_session,
        )
        # Record BEFORE the clone: the launcher, install detection and
        # uninstall all read this path back rather than rebuilding it, so an
        # interrupted rsync leaves a reachable prefix, not an invisible
        # orphan.
        self._id_map.merge(game_id, prefix_path=str(target))
        return target

    async def ensure_family(self, game_id: str) -> bool:
        """True once a family code for ``game_id`` is in the id map.

        Normally already there — ``get_library`` records the whole library on
        every sync. This covers the install-without-a-recent-sync case by
        re-reading the catalog for just this title, which costs one catalog
        parse rather than a full library build.
        """
        if self._id_map.resolve_family(game_id):
            return True
        drive_c = paths.drive_c(self._prefixes.auth_prefix)
        if drive_c is None:
            return False
        catalog = await asyncio.to_thread(read_catalog, drive_c)
        family = library_mod.family_from_catalog(catalog, game_id)
        if family is None:
            logger.error("[Battlenet] no family code in the catalog for %s", game_id)
            return False
        self._id_map.merge(game_id, family=family)
        logger.info(
            "[Battlenet] resolved family %s for %s at install time", family, game_id,
        )
        return True

    async def cleanup_abandoned(
        self, game_id: str, prefix: Path, *, since: float | None = None,
    ) -> None:
        """Drop the prefix left by an install that produced no game.

        An interrupted clone to removable media would otherwise leave a
        partial ~1.2 GB tree squatting on the disk the user picked, with the
        id map still pointing at it.

        The client's own logs come out first. They live inside the prefix,
        so this deletion is the only thing standing between a failed install
        and the sole first-hand record of why it failed — and it has already
        cost one field investigation. See ``shared/prefix_forensics``.

        So does the Agent's content store, for the same reason and at a much
        higher price: a user who cancels an install that looked stuck is
        usually cancelling the Agent's own self-update, and without this the
        next attempt re-downloads it from zero and looks equally stuck.

        ``since`` is when this attempt's client started. ``None`` means no
        client ran under this prefix (the clone itself failed), so there is
        nothing of ours in it to keep and the capture is skipped outright.
        """
        await self._salvage_client_logs(game_id, prefix)
        if since is not None:
            await self._capture_client_cache(prefix, since)
        deleted = await cleanup_abandoned_prefix(
            prefix,
            recorded=self._id_map.resolve_prefix(game_id),
            holds_game=self._holds_game,
            remover=self._prefixes.remove_game_prefix,
            label=LABEL,
        )
        if deleted:
            self._id_map.clear_prefix(game_id)

    def _auth_not_ready_reason(self) -> tuple[str, str]:
        """``(message, error_code)`` naming *why* the auth prefix is unusable.

        Two different situations reach the same gate and want different
        words. Telling a user to "sign in first" when they signed in
        successfully an hour ago is how a real report came back a second
        time: their session was fine, the client the session lives beside
        was half-installed. Signing in again does repair it — the sign-in
        shortcut reinstalls the client — but only if the message says so.
        """
        auth = self._prefixes.auth_prefix
        if paths.client_exe(auth) is not None:
            return (
                (
                    "Battle.net's client files are incomplete. Sign in to "
                    "Battle.net again and let the window finish opening. That "
                    "reinstalls the client."
                ),
                "client_incomplete",
            )
        return (
            (
                "Sign in to Battle.net first — the game prefix inherits "
                "your signed-in session"
            ),
            "not_signed_in",
        )

    async def _salvage_client_logs(self, game_id: str, prefix: Path) -> None:
        """Copy Battle.net's own logs out before the prefix goes.

        Best-effort by construction — :func:`preserve_vendor_logs` swallows
        everything and returns a count. A salvage must never be the reason a
        prefix the user is waiting on does not get reclaimed.
        """
        await preserve_vendor_logs(
            STORE_ID, Path(prefix), salvage_path(STORE_ID, game_id),
        )

    async def _capture_client_cache(self, prefix: Path, since: float) -> None:
        """Carry the Agent's content store back up the prefix chain.

        Never raises.

        **Into the auth prefix, not only the template.** Capturing only into
        ``.template`` looked right and does not survive: the template is by
        definition an rsync clone of ``.bnet-auth``, and ``ensure_template``
        rebuilds it from there whenever it is not a usable auth-derived
        snapshot, which a sign-out makes true. Measured end to end on
        2026-08-22: a capture landed at 21:14, the user signed out at 22:22,
        the template was re-derived at 22:45, and the next install paid the
        agent update over again with the store back at its 6.9 MB bootstrap
        size. Writing to auth as well means a rebuild inherits the cache
        instead of discarding it.

        The template still gets its own copy, because it is what the *next*
        install clones and a rebuild is not guaranteed to happen in between.

        What makes one copy of that store interchangeable with another is the
        Agent's TACT tag query, not its size. A completed update *shrinks*
        the store, because the Agent compacts away whatever the new tags do
        not select. So the tag string travels with the copy as its generation.

        **An unfinished store is still worth keeping**, and this is the case
        that matters. Users cancel exactly when the wait looks broken, which is
        mid-agent-update; capturing only finished downloads would keep nothing
        on the one path that actually repeats. It is marked partial so a later
        complete capture still supersedes it.

        What is *not* negotiable is that nothing is writing the store as it is
        copied. A torn content store in the template is inherited by every
        prefix made afterwards, which is the one outcome worse than a slow
        install. Note that "the client is closed" is the wrong test for that:
        completion deliberately leaves the window open, so requiring a dead
        client would silently disable the capture on the success path, which
        is the cheapest one. :meth:`_store_is_quiescent` asks the real
        question instead.
        """
        drive_c = paths.drive_c(Path(prefix))
        spec = spec_for(STORE_ID)
        if drive_c is None or spec is None:
            return
        generation = agent_status.update_generation(drive_c, since)
        if not generation:
            return
        if not await self._store_is_quiescent(prefix, drive_c, since):
            return
        complete = agent_status.self_update_finished(drive_c, since)
        destinations = [self._prefixes.template_prefix]
        # The auth prefix is the durable one, but it is also the prefix the
        # sign-in client runs in, so it only accepts a copy while that client
        # is down. Skipping it costs the next template rebuild, not the
        # capture: the template copy below still happens either way.
        if not self._prefixes.auth_is_busy():
            destinations.append(self._prefixes.auth_prefix)
        for destination in destinations:
            await asyncio.to_thread(
                capture_client_cache,
                spec,
                Path(prefix),
                destination,
                generation,
                complete=complete,
            )

    async def _store_is_quiescent(
        self, prefix: Path, drive_c: Path, since: float,
    ) -> bool:
        """Whether nothing is currently writing the Agent's content store.

        Two ways to be sure, one per exit path:

        * **No client.** The cancel path kills it first, so the store is
          whatever the interrupted download left: the ordinary resumable
          state, and nothing can change it now.
        * **No active operation.** The success path leaves the window open,
          but the Agent runs one exclusive operation at a time and says when
          it holds none. An idle Agent is not writing.

        A state we cannot read is not quiescent: refusing to capture costs one
        slow install, capturing mid-write costs every install after it.
        """
        if not await asyncio.to_thread(install_alive, STORE_ID, Path(prefix)):
            return True
        state = agent_status.read_state(drive_c, since)
        return state is not None and state.active is None

    async def _holds_game(self, prefix: Path) -> bool:
        return await asyncio.to_thread(holds_ready_install, Path(prefix))
