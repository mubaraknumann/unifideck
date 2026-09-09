"""services/shortcut/service.py — ShortcutService facade class.

Non-Steam shortcut management. Mutates ``shortcuts.vdf``
(Steam's registry) and ``games.map`` (Unifideck's own exe
manifest read by the launcher wrapper at game-launch time).

Shell class composing multiple mixins:
- ``EventsMixin``       : ``@subscribe`` handlers
- ``_GamesMapMixin``    : typed mutations + queries
- ``_VdfShortcutsMixin``: escape-hatch read/write + auth
                          shortcut delegator

Shell itself owns ``__init__`` / ``stop`` / ``generate_app_id``
and three loaders that pair ``_loaded`` flags with
``persistence.py`` stateless helpers.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from unifideck.event_bus.event_bus_devex import auto_wire

from .events import EventsMixin
from .games_map import GameMapEntry, generate_app_id
from .games_map_mixin import UNIFIDECK_TAG, _GamesMapMixin
from .persistence import (
    merge_foreign_shortcuts,
    read_games_map,
    vdf_write_lock,
    write_games_map,
    write_vdf,
)
from .vdf_read import VdfStatus, read_vdf_checked
from .vdf_shortcuts import _VdfShortcutsMixin
from .write_guard import census, check_write, log_refusal

if TYPE_CHECKING:
    from unifideck.event_bus.event_bus import EventBus

logger = logging.getLogger(__name__)

__all__ = ["UNIFIDECK_TAG", "ShortcutService"]


class ShortcutService(
    EventsMixin,
    _GamesMapMixin,
    _VdfShortcutsMixin,
):
    """Facade for shortcuts.vdf + games.map mutations."""

    def __init__(
        self,
        bus: EventBus,
        shortcuts_path: str,
        games_map_path: str,
        launcher_path: str = "",
    ) -> None:
        """Store refs + paths, init empty state + per-file loaded flags."""
        self._bus = bus
        self._shortcuts_path = shortcuts_path
        self._games_map_path = games_map_path
        # ``launcher_path`` is the ``Exe`` written into every
        # generated ``shortcuts.vdf`` entry — Steam launches this
        # binary when the user clicks the tile, and it then reads
        # ``LaunchOptions`` (``"<store>:<game_id>"``) to install /
        # play the game via the right backend. Always set in
        # production via the service_defs wiring; defaults to ""
        # only for unit tests that exercise the mixin in isolation.
        self._launcher_path = launcher_path

        self._shortcuts: dict[str, Any] = {}
        # Type fix (lot 11g): the source of truth is
        # ``persistence.read_games_map`` which returns
        # ``dict[str, GameMapEntry]`` (the NamedTuple). The prior
        # declaration as ``dict[str, dict[str, str]]`` was a
        # drift artifact and caused mypy assignment errors at
        # every load site.
        self._games_map: dict[str, GameMapEntry] = {}

        self._shortcuts_loaded = False
        self._games_map_loaded = False
        # Set when the last read could not be trusted. Distinct from
        # ``not _shortcuts_loaded`` (which also covers "never read yet")
        # so callers can tell "no shortcuts" from "we cannot see them".
        self._shortcuts_unreadable = False

        # ``auto_wire(self, bus)`` walks ``self``'s methods
        # and registers every ``@subscribe(Events.X)``-marked
        # handler with the bus. Earlier this site called
        # ``self._bus.auto_wire(self)`` as if it were a bus
        # method, but ``auto_wire`` is module-level — the
        # call raised ``AttributeError`` and every
        # subscription was lost (caught and silenced upstream).
        auto_wire(self, self._bus)

    def set_shortcuts_path(self, shortcuts_path: str) -> None:
        """Re-point at a different user's ``shortcuts.vdf`` at runtime.

        Called when the active Steam user is (re)confirmed after boot — the
        frontend push or an account switch — via
        :func:`unifideck.steam.current_user.rebind_user_paths`. Resetting
        ``_shortcuts_loaded`` is load-bearing: the in-memory ``_shortcuts``
        dict is per-user, so without it the NEXT ``_save_all`` would write the
        *previous* user's cached entries into the new user's file. Clearing the
        cache forces a fresh read of the correct file on the next access.
        """
        if shortcuts_path == self._shortcuts_path:
            return
        self._shortcuts_path = shortcuts_path
        self._shortcuts = {}
        self._shortcuts_loaded = False
        self._shortcuts_unreadable = False

    async def stop(self) -> None:
        """Unsubscribe from EventBus events and persist pending changes."""
        self._bus.unsubscribe_all(self)
        await self._save_all()

    @staticmethod
    def generate_app_id(launcher: str, identity: str) -> int:
        """Delegate to module-level generate_app_id in games_map.py."""
        return generate_app_id(launcher, identity)

    async def _load_shortcuts(self) -> None:
        """Load shortcuts.vdf into memory (idempotent).

        An unreadable file is **not** cached as an empty library. That
        conflation is what let a single failed parse look like "the user
        has no shortcuts": reconcile would rebuild ours from nothing and
        the following write would replace the user's whole non-Steam
        library with only our entries. Leaving ``_shortcuts_loaded``
        False keeps the read retryable and keeps ``_save_all`` from
        writing, so the next sync gets another chance at the real file.
        """
        if self._shortcuts_loaded:
            return

        result = await read_vdf_checked(self._shortcuts_path)
        if result.status is VdfStatus.UNREADABLE:
            self._shortcuts = {"shortcuts": {}}
            self._shortcuts_unreadable = True
            return

        self._shortcuts = result.data
        self._shortcuts_unreadable = False
        self._shortcuts_loaded = True

    async def _load_games_map(self) -> None:
        """Load games.map with retry-on-corruption (idempotent)."""
        if self._games_map_loaded:
            return

        self._games_map = await read_games_map(self._games_map_path)
        self._games_map_loaded = True

    async def _save_all(
        self, *, allow_foreign_drops: frozenset[int] = frozenset(),
    ) -> None:
        """Persist shortcuts.vdf + games.map atomically.

        Before writing shortcuts.vdf, re-read the on-disk file and
        merge back any *foreign* shortcut a concurrent writer added
        since our in-memory snapshot was loaded — NonSteamLaunchers'
        scanner service, Steam's shutdown flush, or a manual add. Our
        write is a full read-modify-write of a long-lived cached dict
        (``_shortcuts_loaded`` never re-reads), so without this merge
        it clobbers those entries (UD-043 data loss). Ownership is the
        launcher-``Exe`` gate reconcile already uses: our own entries
        stay memory-authoritative (our deletes are honoured), only
        foreign entries missing from memory are restored.

        The merge is best-effort, though — it can only restore what it
        can identify — so the write is then *checked*: if it would drop
        a foreign entry no caller declared, or the on-disk state could
        not be read at all, the write is refused and the file left
        exactly as it is. See :mod:`write_guard`. Callers that mean to
        remove a foreign-looking row name its appid in
        ``allow_foreign_drops``.

        **Serialised against every other writer of these two files.** The
        merge above defends against a *foreign* concurrent writer; it does
        nothing about two of ours. Both this and the icon pass in
        :mod:`events` do their own read, edit and write, so running
        concurrently they each start from the same snapshot and the second
        discards the first's entries. Worse, they shared one fixed temp path,
        so one write consumed the other's temp file mid-rename: measured
        during a logout as 260 failures in twelve seconds, several leaving
        ``wrote 398 entries but the file holds 0``. The lock covers the whole
        cycle, because guarding only the write would still lose the merge.

        Not reentrant, and does not need to be: every caller of this method
        is a top-level operation (reconcile phases, the games-map mixin,
        startup, the LastPlayTime migration) and none runs inside another.
        """
        async with vdf_write_lock():
            if self._shortcuts_loaded:
                await self._save_shortcuts(allow_foreign_drops)

            if self._games_map_loaded:
                await write_games_map(self._games_map_path, self._games_map)

    async def _save_shortcuts(
        self, allow_foreign_drops: frozenset[int],
    ) -> None:
        """Merge, check, then write shortcuts.vdf — see :meth:`_save_all`."""
        disk = await read_vdf_checked(self._shortcuts_path)
        merge_foreign_shortcuts(
            self._shortcuts, disk.data, self._launcher_path,
            allow_foreign_drops,
        )
        verdict = check_write(
            self._shortcuts, disk, self._launcher_path, allow_foreign_drops,
        )
        if not verdict.allowed:
            log_refusal(verdict, disk, self._launcher_path)
            await self._emit_write_refused(verdict.reason)
            return

        await write_vdf(
            self._shortcuts_path, self._shortcuts, self._data_dir,
        )
        # The path is logged, not just the census, because the failure it
        # exposes is invisible otherwise: the backend resolves the Steam user
        # from disk heuristics unless the frontend pushed the live one, and on
        # a multi-account Deck it can pick the wrong ``userdata/<id>``. The
        # sync then reports N games written while Steam, reading the other
        # account's file, shows none. One line turns "synced N games, Steam
        # shows 0" into a diagnosis.
        logger.info(
            "[ShortcutService] wrote %s: %s",
            self._shortcuts_path,
            census(self._shortcuts.get("shortcuts", {}), self._launcher_path),
        )

    @property
    def _data_dir(self) -> str:
        """Plugin data dir, derived from ``games.map``'s location.

        ``ShortcutService`` is constructed with per-file paths rather
        than the ``ServicePaths`` bundle, and ``games.map`` already lives
        at the data-dir root — the same derivation the LastPlayTime
        migration marker uses.
        """
        return str(Path(self._games_map_path).parent)

    @property
    def _registry_path(self) -> Path:
        """Where this service's shortcut registry lives.

        Derived from the configured data dir rather than taken from
        ``registry.DEFAULT_REGISTRY_PATH``, which is expanded once at
        import time. Using the constant meant the registry was read and
        written at a fixed location no matter where the service was
        pointed — so a differently-configured data dir silently kept
        using the default file.
        """
        return Path(self._data_dir) / "shortcuts_registry.json"

    async def _emit_write_refused(self, reason: str) -> None:
        """Tell the user we declined to touch their shortcuts, and why.

        A refusal is silent from the UI's point of view otherwise: the
        sync reports success and the library simply does not change.

        ``LAUNCHER_STAGE`` is the toast channel the frontend actually
        consumes, in both processes this service runs in — the plugin
        backend (replay buffer → ``boot-event-listener``) and the launcher
        subprocess, which ``build_service_subset`` also builds a
        ``ShortcutService`` in (bus forwarder → bridge file →
        ``launcherToasts`` poll). The previous ``TOAST_NOTIFICATION`` emit
        reached neither, so this toast never fired once since it was
        written — the very silence its docstring exists to prevent.

        ``duration_ms`` is explicit because the string is a full paragraph
        and the frontend's 7.5s error default cuts the read short.
        """
        from unifideck.launcher.rpc import emit_stage

        try:
            await emit_stage(
                self._bus,
                i18n_key="toasts.shortcuts.writeRefused",
                game_title="",
                severity="error",
                duration_ms=12000,
                i18n_params={"reason": reason},
            )
        except Exception as e:
            logger.warning(
                "[ShortcutService] could not emit write-refused toast: %s", e,
            )
