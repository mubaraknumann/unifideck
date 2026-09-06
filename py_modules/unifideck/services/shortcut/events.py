"""services/shortcut/events.py — Event handlers for shortcut lifecycle."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from unifideck.core.sync_generation import UNTAGGED_RUN_ID, run_id_of
from unifideck.core.types import Events, Game
from unifideck.event_bus.event_bus_devex import subscribe

from .stale_predicate import SweepableStores

logger = logging.getLogger(__name__)

# Budget for the post-sync reconcile handler.
#
# ``HandlerWatchdog``'s 5s default is smaller than the work: a healthy
# 1242-game reconcile measured 4.9s on this Deck (2026-08-29 02:15:00 →
# 02:15:05), and 0.9s at 1229 games. So the margin was a few hundred
# milliseconds, and any contention on the loop pushed it over — the
# watchdog cancelled the handler mid-reconcile twice in one session, at
# 9.5s and 11.6s. A cancelled reconcile leaves shortcuts.vdf unwritten
# and never emits SHORTCUT_RECONCILE_COMPLETE, so the user gets neither
# their shortcuts nor the restart prompt (GOG's 228 games had no
# shortcuts for five minutes because of exactly this).
#
# 120s matches ``PER_STORE_FETCH_TIMEOUT_SECONDS`` — generous enough that
# only a genuinely wedged reconcile trips it, which is what the watchdog
# is for. Note this only takes effect because ``_register_with_watchdog``
# now forwards the ``@subscribe(timeout=...)`` override; it used to drop
# it, leaving every handler on the 5s default.
RECONCILE_TIMEOUT_SECONDS = 120.0


def _is_stale_run(kwargs: dict[str, Any], current: int | None) -> bool:
    """Whether a phase event belongs to a run older than ``current``.

    Fails open in both untagged directions — an event with no ``run_id``,
    or a service that has not yet seen a tagged ``SYNC_COMPLETE`` — so a
    partially-migrated emitter degrades to the old always-run behaviour
    rather than silently skipping the icon pass forever.
    """
    incoming = run_id_of(kwargs)
    if incoming == UNTAGGED_RUN_ID or current is None:
        return False
    return incoming != current


def _find_icon_for_appid(grid_dir: str, appid: int) -> str:
    """Return the absolute icon path Steam's grid dir holds for ``appid``.

    Tries the two extensions SteamGridDB exports use, in order.
    Returns ``""`` when no file is found — caller treats empty
    as "no update needed".
    """
    # NOT the same conversion as ``core.compat_bridge.to_unsigned``, despite
    # looking like it — do not fold the two together. That one is a pure
    # signed→unsigned reinterpretation; this one additionally *forces* the
    # high bit, which is the invariant ``games_map.py`` establishes when it
    # generates an appid (``crc32(key) | 0x80000000``). Steam names grid
    # files with that form, so the OR is what makes the filename right even
    # if a caller ever hands us an id from another source.
    unsigned = (appid & 0xFFFFFFFF) | 0x80000000
    for ext in (".jpg", ".png"):
        candidate = Path(grid_dir) / f"{unsigned}_icon{ext}"
        if candidate.exists():
            return str(candidate)
    return ""


def _apply_icon_updates(
    shortcuts: dict[str, Any], grid_dir: str, launcher_path: str = "",
) -> int:
    """Walk *our* shortcuts and update each entry's ``icon`` field in place.

    Returns the count of entries actually mutated (skips entries
    whose ``icon`` field already points at the on-disk file).

    Foreign entries are skipped. ``grid/`` is shared with every other
    non-Steam tool on the device, so a file named for a foreign
    shortcut's appid is *their* artwork — pointing that entry's ``icon``
    at our resolved path rewrites a tile we do not own.
    """
    from .write_guard import is_ours

    updated = 0
    for entry in shortcuts.values():
        if not isinstance(entry, dict) or not is_ours(entry, launcher_path):
            continue
        appid = entry.get("appid")
        if not isinstance(appid, int):
            continue
        icon_path = _find_icon_for_appid(grid_dir, appid)
        if not icon_path or entry.get("icon", "") == icon_path:
            continue
        entry["icon"] = icon_path
        updated += 1
    return updated


def _data_dir_of(svc: Any) -> str:
    """Plugin data dir for *svc*, or ``""`` when it cannot be derived.

    ``games.map`` sits at the data-dir root, so its parent is the dir.
    Returning ``""`` disables backups rather than raising — this runs on
    a background artwork handler, and a stub service without the
    attribute must not break the icon refresh.
    """
    games_map = getattr(svc, "_games_map_path", "") or ""
    return str(Path(games_map).parent) if games_map else ""


async def _update_icons_from_grid(svc: Any) -> int:
    """Scan grid dir for icon files and update shortcuts.vdf.

    Called after the artwork phase completes so any newly-
    downloaded icon files are picked up. Returns the number of
    shortcuts whose icon field was updated.
    """
    shortcuts_path = getattr(svc, "_shortcuts_path", None)
    if not shortcuts_path:
        logger.warning("[ShortcutService] no shortcuts_path — cannot update icons")
        return 0

    grid_dir = str(Path(shortcuts_path).parent / "grid")
    try:
        from unifideck.services.shortcut.persistence import (
            read_vdf,
            vdf_write_lock,
            write_vdf,
        )
    except Exception:
        logger.exception("[ShortcutService] failed to import VDF deps")
        return 0

    # The second read-modify-write of shortcuts.vdf in the package, and the
    # reason the lock is module-level rather than owned by the service: this
    # runs off the artwork phase, concurrently with a reconcile doing its own
    # read-edit-write, and unsynchronised they both start from the same
    # snapshot so one silently discards the other's entries. See
    # ``ShortcutService._save_all``.
    async with vdf_write_lock():
        data = await read_vdf(shortcuts_path)
        shortcuts = data.get("shortcuts")
        if not isinstance(shortcuts, dict):
            return 0

        launcher = getattr(svc, "_launcher_path", "") or ""
        updated = _apply_icon_updates(shortcuts, grid_dir, launcher)
        if updated > 0:
            await write_vdf(shortcuts_path, data, _data_dir_of(svc))
            logger.info(
                "[ShortcutService] updated icons for %d shortcuts in shortcuts.vdf",
                updated,
            )
    return updated


def _sweepable_stores(payload: dict[str, Any]) -> SweepableStores:
    """The stores whose stale shortcuts this sync is allowed to delete.

    **A store's shortcuts may only be swept when we hold a current,
    authoritative statement of what that store contains** — that is, the
    store was fetched this run *and* answered without error. Everything
    else keeps its shortcuts.

    This used to read ``registered_stores`` instead: every registered
    store was sweepable, whether or not it had answered. The reasoning was
    sound for what it named (phantom Ubisoft rows and the legacy
    ``microsoft:ms-auth`` row should not linger forever) but the rule was
    far wider than the reason, and it overrode the guard
    ``_is_stale_managed_shortcut`` documents as *"how staging avoided
    nuking the user's Epic shortcuts after they logged out of Epic"*.
    A store contributes zero games without owning zero games in four ways:
    it raised, it timed out, it returned ``None`` ("I could not read"), or
    it was unavailable and never fetched at all. Each of those deleted
    every shortcut that store owned. The last one records no error, so it
    was invisible in the logs too.

    That is not hypothetical. GOG's ``is_available`` now refuses when
    ``bin/gogdl`` is missing or non-executable (audit §3.2, correctly) —
    so under the old rule a half-applied update that lost the exec bit
    made the next sync delete every GOG shortcut in the library.

    Kept deliberately: a store that answers with an *empty* library is
    still swept, which is the phantom-row cleanup the widening existed
    for. The cost of the narrowing is that those rows now survive until
    the store is signed in again, which is the strictly safer failure
    mode — a stale tile costs one sync, a deleted library costs the
    user's library. The one artifact that cannot be reached that way is
    handled by name; see ``protected.LEGACY_SWEEP_IDS``.
    """
    fetched = payload.get("stores_synced") or []
    errors = payload.get("errors") or {}
    if not isinstance(fetched, (list, tuple, set)):
        return SweepableStores(frozenset())
    failed = set(errors) if isinstance(errors, dict) else set()
    return SweepableStores(
        frozenset(s for s in fetched if isinstance(s, str) and s not in failed),
    )


if TYPE_CHECKING:
    # This is a mixin; `self` will be the ShortcutService facade
    # at runtime. The facade provides ``mark_installed``,
    # ``mark_uninstalled``, ``remove_game`` and ``reconcile`` via
    # ``_GamesMapMixin``. Mypy doesn't see the multiple-inheritance
    # composition here (this file is imported standalone), so we
    # declare the protocol of methods we rely on as
    # TYPE_CHECKING-only forward refs.
    from collections.abc import Sequence


class EventsMixin:
    """Event subscriptions for ShortcutService.

    Expects the composing class to provide the methods listed
    in the ``if TYPE_CHECKING`` block below. Each handler reads
    its payload from the bus and delegates to the facade.
    """

    if TYPE_CHECKING:
        # Type-only declarations — implementations come from
        # ``_GamesMapMixin`` at runtime through the MRO. These
        # stubs exist purely so mypy knows the methods exist on
        # ``self`` when this module is type-checked in isolation.
        #
        # Signatures match ``_GamesMapMixin`` exactly: ``async def
        # foo(...) -> T`` (not ``def foo(...) -> Awaitable[T]``).
        # Lot 12d fix: previously the stubs returned ``Awaitable[T]``
        # which is semantically equivalent but mypy strict considered
        # them incompatible with the ``async def`` definitions in
        # ``_GamesMapMixin`` — surfaced as 3× ``[misc]`` "incompatible
        # definition in base class" errors on the facade class
        # body in service.py.
        async def remove_game(self, app_id: int) -> bool: ...
        async def mark_installed(
            self, store: str, store_game_id: str,
            exe_path: str, install_path: str,
        ) -> int | None: ...
        async def mark_uninstalled(
            self, store: str, store_game_id: str,
        ) -> int | None: ...
        async def reconcile(
            self, games: Sequence[Game], *, force: bool = ...,
            valid_stores: SweepableStores | None = ...,
        ) -> dict[str, int]: ...

    @subscribe(Events.DOWNLOAD_COMPLETE)
    async def _on_download_complete(self, **kwargs: Any) -> None:
        """Flip the existing shortcut's install state to installed.

        The shortcut was created at sync time by reconcile with
        ``tags["2"] = "Not Installed"`` — we only flip the tag and
        write the games.map row. The shortcut's appid is preserved
        so Steam playtime / artwork / categories survive the
        transition.
        """
        game = kwargs.get("game")
        if isinstance(game, Game):
            await self.mark_installed(
                game.store, game.store_game_id,
                game.exe_path or "", game.install_path or "",
            )

    @subscribe(Events.GAME_UNINSTALLED)
    async def _on_game_uninstalled(self, **kwargs: Any) -> None:
        """Flip the existing shortcut's install state to not-installed.

        Symmetric with ``_on_download_complete``: the user still
        owns the game, they just removed the bytes. Keep the
        shortcut and its appid so the frontend cache and detail-page
        UI continue to recognise it. The emitters in
        ``stores/{epic,amazon}/install.py`` pass ``store`` + ``game_id``
        (not ``app_id``), so we look the shortcut up by those.
        """
        store = kwargs.get("store")
        game_id = kwargs.get("game_id")
        if isinstance(store, str) and isinstance(game_id, str):
            await self.mark_uninstalled(store, game_id)

    @subscribe(Events.SYNC_COMPLETE, timeout=RECONCILE_TIMEOUT_SECONDS)
    async def _on_sync_complete(self, **kwargs: Any) -> None:
        """Reconcile shortcuts against the new library state.

        After reconciling, emit ``SHORTCUT_RECONCILE_COMPLETE``
        with the per-batch counters so the frontend can prompt
        the user for a Steam restart when any shortcuts were
        added or removed (Steam holds shortcuts.vdf in memory and
        overwrites our writes on its next shutdown otherwise).
        """
        games = kwargs.get("games", [])
        # Latch the generation even on the empty-library early return, so
        # ``_on_artwork_phase_done`` always compares against the newest run
        # this service has seen rather than a stale one.
        self._last_run_id = run_id_of(kwargs)
        if not games:
            return
        is_force = bool(kwargs.get("is_force", False))
        valid_stores = _sweepable_stores(kwargs)
        logger.info(
            "[ShortcutService] SYNC_COMPLETE → reconciling %d games "
            "(force=%s, sweepable_stores=%s)",
            len(games), is_force, sorted(valid_stores),
        )
        result = await self.reconcile(
            games, force=is_force, valid_stores=valid_stores,
        )
        added = result.get("added", 0)
        removed = result.get("removed", 0)
        kept = result.get("kept", 0)
        reclaimed = result.get("reclaimed", 0)
        # ``self._bus`` is provided by the host (ShortcutService
        # facade); silently skip the emit if for some reason it's
        # unavailable so a missing bus never breaks reconcile.
        bus = getattr(self, "_bus", None)
        if bus is None:
            return
        await bus.emit(
            Events.SHORTCUT_RECONCILE_COMPLETE,
            added=added, removed=removed, kept=kept,
            # Reclaiming re-attaches orphaned VDF rows to the library by
            # appid. It was computed and then dropped on the floor, so a
            # reconcile that only reclaimed (``added=0 removed=0
            # reclaimed=997``, observed 2026-08-29 02:20) told the
            # frontend nothing had changed and no restart was offered —
            # even though the rows Steam had in memory were stale.
            reclaimed=reclaimed,
            total=len(games),
            # Generation this reconcile belongs to, so the frontend can
            # ignore one that arrives for a superseded run.
            run_id=run_id_of(kwargs),
        )

    @subscribe(Events.POST_SYNC_PHASE_CHANGED)
    async def _on_artwork_phase_done(self, **kwargs: Any) -> None:
        """After artwork download completes, update icon paths in
        shortcuts.vdf so Steam displays the shortcut icon in the
        library list and desktop mode."""
        if kwargs.get("phase") != "artwork":
            return
        if kwargs.get("active") is not False:
            return
        # Only rewrite icons for the generation that is actually current.
        # An orphaned batch used to reach here minutes after its sync was
        # superseded and rewrite shortcuts.vdf from its own stale view —
        # observed 2026-08-29 02:17:12, "updated icons for 510 shortcuts"
        # against a 645-game generation when the library was 1242.
        #
        # The current generation is the one from the last SYNC_COMPLETE this
        # service handled; there is deliberately no SyncService reference
        # here, because a ``getattr(self, "_sync_service", None)`` that never
        # resolves is a check that silently never runs.
        if _is_stale_run(kwargs, getattr(self, "_last_run_id", None)):
            logger.info(
                "[ShortcutService] skipping icon update — artwork phase "
                "belongs to superseded run %s (current %s)",
                kwargs.get("run_id"), getattr(self, "_last_run_id", None),
            )
            return
        logger.info("[ShortcutService] artwork phase done — updating icons")
        await _update_icons_from_grid(self)
