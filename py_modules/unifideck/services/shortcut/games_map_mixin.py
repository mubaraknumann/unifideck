"""services/shortcut/games_map_mixin.py — Games map mutations + queries.

Mutating operations on the shortcuts list + games.map manifest.
Mixin assumes the host exposes ``_bus``, ``_shortcuts``,
``_games_map``, paths, and async ``_load_*`` / ``_save_all``
primitives.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .games_map import UNIFIDECK_TAG, GameMapEntry
from .launch_options import get_full_id
from .reconcile_phases import _ReconcilePhasesMixin

if TYPE_CHECKING:
    from unifideck.event_bus.event_bus import EventBus


logger = logging.getLogger(__name__)

# Re-exported from ``games_map`` so callers can still write
# ``from .games_map_mixin import UNIFIDECK_TAG`` without forming
# a cycle with ``reconcile_phases``.
__all__ = ["UNIFIDECK_TAG"]


class _GamesMapMixin(_ReconcilePhasesMixin):
    """Games-map mutations + queries for ShortcutService."""

    # These are provided by the ShortcutService facade at runtime
    _bus: EventBus
    _shortcuts: dict[str, Any]
    _games_map: dict[str, GameMapEntry]

    # Assume host provides these async load/save primitives
    # async def _load_shortcuts(self) -> None: ...
    # async def _load_games_map(self) -> None: ...
    # async def _save_all(self) -> None: ...

    async def get_exe_for_game_key(self: Any, store: str, game_id: str) -> str | None:
        """Return absolute exe path for ``store:game_id``, or None.

        Read-only query — loads the games.map on first call then
        reuses the in-memory dict.
        """
        entry = await self.get_entry_for_game_key(store, game_id)
        return entry.exe if entry else None

    async def get_entry_for_game_key(self: Any, store: str, game_id: str) -> GameMapEntry | None:
        """Return the full ``GameMapEntry`` (exe + work_dir) or None."""
        await self._load_games_map()
        key = f"{store}:{game_id}"
        # cast: ``self`` is typed Any in the mixin (host provides
        # _load_games_map), so ``self._games_map.get`` returns Any
        # even though ``_games_map: dict[str, GameMapEntry]`` is
        # declared at class scope. Cast to recover the precise type.
        entry: GameMapEntry | None = self._games_map.get(key)
        return entry

    async def set_executable(
        self: Any,
        store: str,
        game_id: str,
        exe_abs: str,
        *,
        work_dir: str = "",
        app_id: int | None = None,
    ) -> bool:
        """Rewrite ONLY the games.map exe column for ``store:game_id``.

        Powers the "Change executable" feature: the user picks a different
        launch target (skip a launcher, fix a wrong auto-detected path, run a
        config tool). We update the ``exe`` column **only** — ``work_dir`` and
        ``app_id`` are carried over verbatim from the existing row, so the
        executable choice is fully decoupled from ``work_dir`` (the launch
        CWD / ``STEAM_COMPAT_INSTALL_PATH`` and the source for ``<?INSTALL?>``
        cloud-save resolution). This is the safe counterpart to hand-editing
        games.map, which collapses the tab-delimited row to v1 and corrupts
        ``work_dir`` to ``dirname(exe)``.

        When the game has **no row yet**, one is created from *work_dir* and
        *app_id* if the caller supplied them. That is not a convenience: the
        row is missing in exactly the cases the picker exists to fix — an
        install whose auto-detected executable came back empty writes none,
        and a store that could not answer a sync used to have its row pruned
        — and refusing here left an unlaunchable game with no way back. A
        caller that cannot supply the two (Epic, whose launch goes through
        legendary's ``--override-exe`` and needs no row) still gets ``False``.
        """
        await self._load_games_map()
        key = f"{store}:{game_id}"
        entry: GameMapEntry | None = self._games_map.get(key)
        if entry is None:
            if not work_dir or app_id is None:
                logger.warning(
                    "[ShortcutService] set_executable %s — no games.map row "
                    "and no work_dir/app_id to build one", key,
                )
                return False
            logger.info(
                "[ShortcutService] set_executable %s — creating the missing "
                "row (work_dir=%s app_id=%s)", key, work_dir, app_id,
            )
            self._games_map[key] = GameMapEntry(
                exe=exe_abs, work_dir=work_dir, app_id=app_id,
            )
            await self._save_all()
            return True
        self._games_map[key] = GameMapEntry(
            exe=exe_abs, work_dir=entry.work_dir, app_id=entry.app_id,
        )
        await self._save_all()
        logger.info(
            "[ShortcutService] set_executable %s → %s (work_dir/app_id kept)",
            key, exe_abs,
        )
        return True

    def _forget_registry_rows(self: Any, app_id: int) -> None:
        """Drop the persistent registry rows for a removed shortcut.

        Nothing pruned the registry before, so a removed game's appid
        stayed on file forever — and reconcile treats a registry row as
        licence to reclaim whichever shortcut currently carries that
        appid. Best-effort: failing to prune costs a stale row, and must
        not fail the removal that already happened.
        """
        from .registry import load_registry, save_registry, unregister

        try:
            path = self._registry_path
            registry = load_registry(path)
            if unregister(registry, app_id):
                save_registry(registry, path)
        except Exception:
            logger.exception(
                "[ShortcutService] could not prune registry rows for %s",
                app_id,
            )

    @staticmethod
    def _drop_shortcut_entries(
        shortcuts: Any,
        app_id: int,
        launcher_path: str = "",
    ) -> bool:
        """Delete every *Unifideck-owned* entry matching ``app_id``.

        Tolerates corrupt VDF (non-dict at root or under
        ``"shortcuts"``) by treating those branches as "no match".
        Returns True if at least one entry was deleted.

        The ownership gate is load-bearing, not defensive: non-Steam
        app_ids are a 2**31 space that Steam, NonSteamLaunchers, Heroic
        and we all draw from, so a collision is a live possibility on a
        busy library — and an app_id match alone would delete whichever
        of the user's own shortcuts happened to land on the same value.
        """
        from .write_guard import is_ours

        if not isinstance(shortcuts, dict) or "shortcuts" not in shortcuts:
            return False
        shortcuts_dict = shortcuts["shortcuts"]
        if not isinstance(shortcuts_dict, dict):
            return False
        keys_to_delete = [
            key for key, entry in shortcuts_dict.items()
            if isinstance(entry, dict)
            and entry.get("appid") == app_id
            and is_ours(entry, launcher_path)
        ]
        for key in keys_to_delete:
            del shortcuts_dict[key]
        return bool(keys_to_delete)

    def _drop_games_map_entries(self: Any, app_id: int) -> bool:
        """Delete every ``games.map`` entry whose stored app_id matches.

        v3 entries store ``app_id`` directly. Legacy v1/v2 rows
        carry ``app_id == 0`` and are matched by cross-referencing
        the loaded ``shortcuts.vdf``: if any VDF entry with the
        target ``app_id`` has the same ``Exe`` as the games.map
        row, treat it as a match (covers the orphan-backfill case
        before the next ``_save_all`` writes the v3 row).
        Returns True if at least one entry was deleted.
        """
        legacy_exes = _GamesMapMixin._collect_legacy_exes(
            self._shortcuts, app_id,
        )
        keys_to_delete = [
            key
            for key, entry in self._games_map.items()
            if entry.app_id == app_id
            or (entry.app_id == 0 and entry.exe in legacy_exes)
        ]
        for key in keys_to_delete:
            del self._games_map[key]
        return bool(keys_to_delete)

    @staticmethod
    def _collect_legacy_exes(shortcuts: Any, app_id: int) -> set[str]:
        """Exe paths of VDF entries matching *app_id*.

        Used to match legacy v1/v2 games.map rows (``app_id == 0``)
        against the loaded ``shortcuts.vdf`` during deletion.
        """
        root = shortcuts.get("shortcuts") if isinstance(shortcuts, dict) else None
        if not isinstance(root, dict):
            return set()
        exes: set[str] = set()
        for entry in root.values():
            if isinstance(entry, dict) and entry.get("appid") == app_id:
                exe = entry.get("Exe") or entry.get("exe") or ""
                if isinstance(exe, str):
                    exes.add(exe.strip('"'))
        return exes

    async def mark_installed(
        self: Any,
        store: str,
        store_game_id: str,
        exe_path: str,
        install_path: str,
    ) -> int | None:
        """Flip an existing shortcut to "installed" without recreating it.

        The shortcut already exists from the prior sync (created by
        ``_reconcile_phase_sync_games`` with ``tags["2"] = "Not Installed"``).
        On install we only need to: (a) flip that tag to ``""``,
        (b) write the games.map entry the launcher uses to resolve
        the exe, and (c) emit a state-change event so SyncService and
        the frontend cache update.

        Critically, we read the existing ``appid`` off the shortcut
        and reuse it for the games.map row. Regenerating with the
        exe path (the worker's old behaviour) would diverge from the
        launcher-anchored id sync wrote, leaving every appid-keyed
        lookup downstream broken.

        Match is strict ``LaunchOptions == f"{store}:{store_game_id}"``.
        Since ``generate_app_id`` is keyed on (launcher, store,
        store_game_id) (see ``games_map.generate_app_id``), the same
        title on different stores has different appids and different
        shortcuts — no title-fallback rebinding is needed.

        Returns the existing app_id, or None if no shortcut matches.
        """
        await self._load_shortcuts()
        await self._load_games_map()

        target_launch = f"{store}:{store_game_id}"
        existing_app_id: int | None = self._flip_existing_install(target_launch)
        if existing_app_id is None:
            return None

        if not exe_path:
            # An empty exe means the store's exe resolver returned
            # nothing — the launcher dispatcher will have no target to
            # run and the game silently fails to launch. Write the
            # entry anyway (work_dir is still useful) but surface the
            # gap loudly so it's caught in logs instead of at click time.
            logger.warning(
                "[ShortcutService] mark_installed %s — empty exe_path; "
                "launcher will not be able to resolve a target",
                target_launch,
            )
        self._games_map[target_launch] = GameMapEntry(
            exe=exe_path, work_dir=install_path, app_id=existing_app_id,
        )

        await self._save_all()
        await self._emit_installed(
            store, store_game_id, existing_app_id, exe_path, install_path,
        )
        logger.info(
            "[ShortcutService] mark_installed %s → app_id=%d",
            target_launch, existing_app_id,
        )
        return existing_app_id

    def _flip_existing_install(self: Any, target_launch: str) -> int | None:
        """Flip the matching shortcut's install tag; return its app_id.

        Returns ``None`` (with a warning) when ``shortcuts.vdf`` is
        empty or no shortcut matches ``target_launch`` yet.
        """
        shortcuts_root = self._shortcuts.get("shortcuts") if isinstance(
            self._shortcuts, dict,
        ) else None
        if not isinstance(shortcuts_root, dict):
            logger.warning(
                "[ShortcutService] mark_installed %s — shortcuts.vdf empty",
                target_launch,
            )
            return None
        existing_app_id: int | None = _GamesMapMixin._flip_install_tag(
            shortcuts_root, target_launch, installed=True,
            launcher_path=getattr(self, "_launcher_path", "") or "",
        )
        if existing_app_id is None:
            logger.warning(
                "[ShortcutService] mark_installed %s — no shortcut found "
                "(sync may not have run yet)",
                target_launch,
            )
        return existing_app_id

    async def _emit_installed(
        self: Any,
        store: str,
        store_game_id: str,
        app_id: int,
        exe_path: str,
        install_path: str,
    ) -> None:
        """Emit SHORTCUT_INSTALL_STATE_CHANGED for a freshly-installed game."""
        if not self._bus:
            return
        from unifideck.core.types.events import Events
        await self._bus.emit(
            Events.SHORTCUT_INSTALL_STATE_CHANGED,
            store=store,
            store_game_id=store_game_id,
            app_id=app_id,
            installed=True,
            exe_path=exe_path,
            install_path=install_path,
        )

    @staticmethod
    def _flip_install_tag(
        shortcuts_root: dict[str, Any],
        target_launch: str,
        *,
        installed: bool,
        launcher_path: str = "",
    ) -> int | None:
        """Find *our* shortcut by LaunchOptions and flip ``tags["2"]``.

        Returns the entry's ``appid``, or None if no match.

        The ``get_full_id`` compare is deliberately canonical rather
        than exact — it has to keep matching after the user appends
        their own params (``MANGOHUD=1``, a ``%command%`` wrapper).
        That tolerance is also why a foreign shortcut carrying one of
        our tokens matches, so ownership is settled by the ``Exe`` gate
        instead of by tightening the string compare.
        """
        from .write_guard import is_ours

        marker = "" if installed else "Not Installed"
        for entry in shortcuts_root.values():
            if not isinstance(entry, dict) or not is_ours(entry, launcher_path):
                continue
            launch = entry.get("LaunchOptions", "")
            if not isinstance(launch, str):
                continue
            if get_full_id(launch) != target_launch:
                continue
            appid = entry.get("appid")
            if not isinstance(appid, int):
                continue
            tags = entry.get("tags")
            if not isinstance(tags, dict):
                tags = {}
                entry["tags"] = tags
            tags["2"] = marker
            return appid
        return None

    async def mark_uninstalled(
        self: Any,
        store: str,
        store_game_id: str,
    ) -> int | None:
        """Symmetric counterpart to :meth:`mark_installed`.

        Flips an existing shortcut back to "not installed" while
        preserving it in shortcuts.vdf — the user still owns the
        game, they just removed the bytes. The shortcut keeps its
        appid so the frontend cache and detail-page UI continue to
        recognise it. We additionally drop the games.map row since
        the launcher can no longer resolve an exe.

        Returns the existing app_id, or None if no shortcut matches.
        """
        await self._load_shortcuts()
        await self._load_games_map()

        target_launch = f"{store}:{store_game_id}"
        shortcuts_root = self._shortcuts.get("shortcuts") if isinstance(
            self._shortcuts, dict,
        ) else None
        if not isinstance(shortcuts_root, dict):
            logger.warning(
                "[ShortcutService] mark_uninstalled %s — shortcuts.vdf empty",
                target_launch,
            )
            return None

        existing_app_id: int | None = _GamesMapMixin._flip_install_tag(
            shortcuts_root, target_launch, installed=False,
            launcher_path=getattr(self, "_launcher_path", "") or "",
        )
        if existing_app_id is None:
            logger.warning(
                "[ShortcutService] mark_uninstalled %s — no shortcut found",
                target_launch,
            )
            return None

        self._games_map.pop(target_launch, None)

        await self._save_all()

        if self._bus:
            from unifideck.core.types.events import Events
            await self._bus.emit(
                Events.SHORTCUT_INSTALL_STATE_CHANGED,
                store=store,
                store_game_id=store_game_id,
                app_id=existing_app_id,
                installed=False,
                exe_path="",
                install_path="",
            )
        logger.info(
            "[ShortcutService] mark_uninstalled %s → app_id=%d",
            target_launch, existing_app_id,
        )
        return existing_app_id

    async def remove_game(self: Any, app_id: int) -> bool:
        """Remove a shortcut by ``app_id``.

        Drops from both ``_shortcuts`` and ``_games_map``, persists via
        ``_save_all``, emits ``SHORTCUT_REMOVED``. Returns True
        on success, False when the app_id wasn't known.
        """
        await self._load_shortcuts()
        await self._load_games_map()

        launcher = getattr(self, "_launcher_path", "") or ""
        dropped_vdf = self._drop_shortcut_entries(
            self._shortcuts, app_id, launcher,
        )
        dropped_map = self._drop_games_map_entries(app_id)
        removed = dropped_vdf or dropped_map

        if removed:
            self._forget_registry_rows(app_id)
            await self._save_all()
            if self._bus:
                from unifideck.core.types.events import Events
                await self._bus.emit(Events.SHORTCUT_REMOVED, app_id=app_id)

        # bool() cast: ``removed`` is ``bool | Any`` because the
        # dropped_* helpers are typed bool but ``self: Any`` taints
        # the dataflow inference. Explicit cast keeps the return
        # type narrow.
        return bool(removed)

    @staticmethod
    def _ensure_shortcuts_root(shortcuts: Any) -> dict[str, Any]:
        """Return the ``shortcuts`` sub-dict, initialising if needed.

        Steam's ``shortcuts.vdf`` always wraps entries under a top-
        level ``"shortcuts"`` key. We tolerate two breakage modes
        seen in the wild (file completely overwritten by a third
        party, or the wrapper key missing) by re-establishing the
        canonical shape and returning the inner dict that the rest
        of ``reconcile`` mutates.

        Note what this does *not* do: substitute an empty dict for a
        ``"shortcuts"`` value of the wrong type. Doing that silently
        discarded a whole library on the next write, and the merge
        could not recover it because it reads the same malformed
        structure. A wrong-typed root is now rejected upstream by
        :func:`vdf_read.read_vdf_checked`, which marks the file
        UNREADABLE so no write is attempted at all.
        """
        if not isinstance(shortcuts, dict):
            shortcuts = {"shortcuts": {}}
        elif not isinstance(shortcuts.get("shortcuts"), dict):
            shortcuts["shortcuts"] = {}
        # cast: ``shortcuts`` is ``Any`` on entry; after the two
        # branches above it's guaranteed to be a ``dict[str, Any]``
        # with a "shortcuts" key. The explicit annotation tells mypy.
        result: dict[str, Any] = shortcuts
        return result

    @staticmethod
    def _find_existing_shortcut_key(
        shortcuts_dict: dict[str, Any],
        app_id: int,
        launcher_path: str = "",
    ) -> str | None:
        """Find *our* existing ``shortcuts.vdf`` key for a given app_id.

        Steam keys shortcuts by an opaque ordinal string ("0", "1",
        ...); the canonical identifier is ``appid``. Returns the
        ordinal of the entry matching ``app_id``, or ``None`` if
        no entry exists yet (caller will allocate a fresh ordinal).

        Foreign entries are never returned. Both callers hand the
        result to a rewrite — ``_reclaim_orphan`` replaces the AppName,
        Exe, LaunchOptions, icon *and tags* wholesale — so returning a
        shortcut we do not own converts one of the user's games into one
        of ours. That conversion tallies as ``reclaimed``, never as
        ``removed``, which is why it could happen invisibly.
        """
        from .write_guard import is_ours

        for vdf_key, entry in shortcuts_dict.items():
            if not isinstance(entry, dict) or entry.get("appid") != app_id:
                continue
            if is_ours(entry, launcher_path):
                return vdf_key
        return None

    @staticmethod
    def _allocate_new_shortcut_key(shortcuts_dict: dict[str, Any]) -> str:
        """Pick the next free ordinal string key for a new shortcut.

        Starts at ``len(shortcuts_dict)`` (cheap O(1) guess) and
        increments past collisions. This keeps keys roughly dense
        without requiring a global counter.
        """
        new_key = str(len(shortcuts_dict))
        while new_key in shortcuts_dict:
            new_key = str(int(new_key) + 1)
        return new_key
