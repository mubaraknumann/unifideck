"""Reconcile phases mixin — bulk shortcut sync from a library snapshot.

Extracted from ``games_map_mixin.py`` (2026-05-17) to keep the
host file under the 550 LOC volumetry cap. Contains the bulk
reconcile method + its five phase helpers — the set-diff
algorithm that adds missing shortcuts, removes stale ones, and
reclaims orphaned entries by AppID from the persistent registry.

The "is this row stale" decision itself lives in ``stale_predicate.py``
(split out 2026-08-26, same cap): it is pure, it is the most destructive
call in the package, and it was already being tested directly.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .games_map import UNIFIDECK_TAG, GameMapEntry, generate_app_id
from .reconcile_helpers import (
    build_launch_index,
    dedup_shortcuts,
    log_restart_banner,
)
from .stale_predicate import SweepableStores, is_stale_managed_shortcut

if TYPE_CHECKING:
    from unifideck.core.types import Game

logger = logging.getLogger(__name__)

class _ReconcilePhasesMixin:
    """Bulk shortcut reconciliation for :class:`ShortcutService`.

    Assumes the host provides ``_load_shortcuts``, ``_load_games_map``,
    ``_save_all``, ``_ensure_shortcuts_root``,
    ``_find_existing_shortcut_key``, ``_allocate_new_shortcut_key``,
    ``_launcher_path``, ``_shortcuts``, ``_games_map``.
    """

    async def reconcile(
        self: Any, games: list[Game], *, force: bool = False,
        valid_stores: SweepableStores | None = None,
    ) -> dict[str, int]:
        """Bulk-sync all shortcuts from a list of Games.

        Regular sync (``force=False``): **additive** — new
        ``store:game_id`` pairs get a shortcut; already-existing
        entries are left untouched. Mirrors staging's
        ``add_games_batch`` behavior.

        Force sync (``force=True``): **overwriting** — existing
        entries have their ``AppName``, ``exe``, ``tags``, and
        ``icon`` fields updated to match current metadata, while
        preserving their ``appid`` so artwork and playtime survive
        the rewrite. Mirrors staging's ``force_update_games_batch``.

        ``valid_stores`` overrides the store prefixes whose stale
        shortcuts may be swept; default (``None``) is the stores that
        returned games. The post-sync caller passes those that
        **answered** — including any answering empty, which is what lets
        phantom rows self-heal — and deliberately not every *registered*
        store, which swept stores that raised, timed out or were never
        fetched, deleting every shortcut they owned (§3.5 B).
        """
        await self._load_shortcuts()
        await self._load_games_map()
        await self._reset_lastplaytime_once()

        from .registry import load_registry, save_registry

        # Explicit path: ``registry``'s default is expanded at import
        # time, so omitting it reads and writes a fixed location
        # regardless of how this service was configured.
        registry_path = self._registry_path
        registry = load_registry(registry_path)
        counts: dict[str, int] = self._apply_reconcile_phases(
            games, registry, force=force, valid_stores=valid_stores,
        )
        if counts["added"] or counts["removed"] or counts["reclaimed"]:
            await self._save_all()
        if counts["added"] or counts["reclaimed"]:
            save_registry(registry, registry_path)
        self._log_reconcile_result(games, counts)
        return counts

    def _apply_reconcile_phases(
        self: Any, games: list[Game], registry: dict[str, Any], *, force: bool,
        valid_stores: SweepableStores | None = None,
    ) -> dict[str, int]:
        """Prune, sync, drop-stale, then dedup; return the counts dict."""
        launcher = getattr(self, "_launcher_path", "") or ""
        valid_keys = {f"{g.store}:{g.store_game_id}" for g in games}
        valid_app_ids = self._compute_valid_app_ids(games, launcher)
        # Default to stores-with-games. This comment used to end "a caller
        # (the post-sync reconcile) can widen this to every registered store
        # so stale shortcuts for a logged-out / empty store also get swept" —
        # which is precisely the widening that made a sync delete every
        # shortcut of any store that failed to answer (§3.5 finding B). The
        # narrow default is the safe one and the parameter is now a
        # ``SweepableStores``, which only ``_sweepable_stores`` can build.
        if valid_stores is None:
            valid_stores = SweepableStores(frozenset(g.store for g in games))
        removed = self._reconcile_phase_prune_map(valid_keys, valid_stores)
        self._shortcuts = self._ensure_shortcuts_root(self._shortcuts)
        shortcuts_dict = self._shortcuts["shortcuts"]
        added, kept, reclaimed = self._reconcile_phase_sync_games(
            games, shortcuts_dict, registry, force=force,
        )
        removed += self._reconcile_phase_drop_stale(
            shortcuts_dict, valid_app_ids, valid_stores, launcher,
        )
        # Dedup AFTER add/drop so reclaimed orphans count toward the
        # winners' scores. Steam occasionally creates duplicate VDF
        # entries with the same launch-options (in-memory desync,
        # crash recovery). Scoped to our own entries by ``launcher``.
        removed += dedup_shortcuts(shortcuts_dict, launcher)
        return {
            "added": added, "removed": removed,
            "kept": kept, "reclaimed": reclaimed,
        }

    @staticmethod
    def _compute_valid_app_ids(games: list[Game], launcher: str) -> set[int]:
        """The set of app_ids the current library should keep."""
        return {
            g.app_id or generate_app_id(launcher, f"{g.store}:{g.store_game_id}")
            for g in games
        }

    def _log_reconcile_result(
        self: Any, games: list[Game], counts: dict[str, int],
    ) -> None:
        """Log the reconcile tally + a Steam-restart banner when changed."""
        logger.info(
            "[ShortcutService] reconcile: %d games → "
            "added=%d kept=%d removed=%d reclaimed=%d",
            len(games), counts["added"], counts["kept"],
            counts["removed"], counts["reclaimed"],
        )
        if counts["added"] > 0 or counts["removed"] > 0:
            log_restart_banner(
                counts["added"], counts["removed"], counts["reclaimed"],
            )

    async def _reset_lastplaytime_once(self: Any) -> None:
        """One-time ``LastPlayTime`` migration — see :mod:`lastplaytime_reset`.

        Body lives in its own module: this file sits against the 550-LOC
        volumetry cap, and a one-shot migration is the natural thing to
        lift out of the steady-state reconcile path.
        """
        from .lastplaytime_reset import reset_lastplaytime_once

        await reset_lastplaytime_once(self)

    # ── Phase helpers ──────────────────────────────────────

    def _reconcile_phase_prune_map(
        self: Any, valid_keys: set[str], valid_stores: SweepableStores,
    ) -> int:
        """Phase 1: drop ``_games_map`` keys absent from ``valid_keys``.

        **Scoped by ``valid_stores``, for the same reason the shortcut sweep
        below is.** A store that could not answer this sync contributes no
        games, so every one of its keys looks stale — and pruning them is
        §3.5 finding B one layer down: the shortcut survives (that sweep is
        already scoped) but loses the games.map row the launcher resolves its
        executable from. The shortcut then sits in the library looking
        installed and does nothing when launched, which reads to the user as
        "it turned into a plain non-Steam shortcut".

        Measured on GameVault, whose server is a machine the user runs and
        so is routinely offline: the fetch failed, the sync correctly logged
        "keeping its existing shortcuts rather than treating this as an empty
        library", and the row was pruned two lines later anyway.

        A store that answered and no longer lists a game still drops it —
        that is a real removal, and it is what this phase is for.
        """
        stale_keys = [
            k for k in self._games_map
            if k not in valid_keys and k.split(":", 1)[0] in valid_stores
        ]
        for key in stale_keys:
            del self._games_map[key]
        return len(stale_keys)

    def _reconcile_phase_sync_games(
        self: Any,
        games: list[Game],
        shortcuts_dict: dict[str, Any],
        registry: dict[str, Any],
        *,
        force: bool = False,
    ) -> tuple[int, int, int]:
        """Phase 2: ensure each game has a map entry and a VDF entry.

        Matching (both staging behaviours):

        * **LaunchOptions** — ``store:game_id`` from the shortcut's
          ``LaunchOptions`` field. Stable across title changes; the
          primary match key. When found, we KNOW this game already
          has a shortcut even if the app_id has drifted.
        * **AppID** — the integer stored in ``shortcut["appid"]``.
          Falls back when LaunchOptions is missing or corrupted.

        Regular sync (``force=False``): matches existing shortcuts
        and **keeps** them — ``added`` only ticks for truly new
        ``store:game_id`` pairs. Moves ``kept`` for existing matches.
        Mirrors staging's ``add_games_batch(skip if exists)``.

        Force sync (``force=True``): matches existing shortcuts and
        **updates** their ``AppName``, ``exe``, ``tags``, and
        ``icon`` fields to match current metadata, while preserving
        the ``appid`` so artwork and playtime carry through the
        rewrite. Mirrors staging's ``force_update_games_batch``.
        """
        # Build a lookup of LaunchOptions → shortcut_key BEFORE
        # iterating games — one O(N) pass across shortcuts, then
        # O(1) per-game. Mirrors staging's approach at
        # shortcuts_manager.py line 1708-1713.
        launcher = getattr(self, "_launcher_path", "") or ""
        launch_to_key = build_launch_index(shortcuts_dict, launcher)
        added = kept = reclaimed = 0
        for game in games:
            outcome = self._sync_one_game(
                game, shortcuts_dict, registry, launch_to_key,
                launcher=launcher, force=force,
            )
            if outcome == "added":
                added += 1
            elif outcome == "reclaimed":
                reclaimed += 1
            else:
                kept += 1
        return added, kept, reclaimed

    def _sync_one_game(
        self: Any,
        game: Game,
        shortcuts_dict: dict[str, Any],
        registry: dict[str, Any],
        launch_to_key: dict[str, str],
        *,
        launcher: str,
        force: bool,
    ) -> str:
        """Reconcile a single ``game`` into ``shortcuts_dict``.

        Returns one of ``"added"``, ``"kept"``, ``"reclaimed"`` for
        the caller's tally. Side effects: updates ``self._games_map``,
        ``shortcuts_dict``, ``registry``.
        """
        from .registry import register

        key = f"{game.store}:{game.store_game_id}"
        app_id = game.app_id or generate_app_id(launcher, key)
        self._update_games_map_row(game, key, app_id)

        if self._try_reclaim_orphan(shortcuts_dict, registry, game, key):
            return "reclaimed"

        # ── Match by LaunchOptions (primary — staging behaviour).
        # Only (re)register on a forced update.
        existing_key = launch_to_key.get(key)
        if existing_key is not None:
            if force:
                self._update_existing_shortcut(
                    shortcuts_dict[existing_key], game, app_id, launcher,
                )
                register(registry, key, app_id, game.title)
            return "kept"

        # ── Match by AppID (fallback — LaunchOptions missing).
        existing_key = self._find_existing_shortcut_key(
            shortcuts_dict, app_id, launcher,
        )
        if existing_key is not None:
            if force:
                self._update_existing_shortcut(
                    shortcuts_dict[existing_key], game, app_id, launcher,
                )
            register(registry, key, app_id, game.title)
            return "kept"

        # ── New shortcut
        new_key = self._allocate_new_shortcut_key(shortcuts_dict)
        shortcuts_dict[new_key] = self._build_shortcut_entry(game, app_id)
        register(registry, key, app_id, game.title)
        return "added"

    def _update_games_map_row(
        self: Any, game: Game, key: str, app_id: int,
    ) -> None:
        """Maintain the games.map exe row for *game* (installed-only).

        games.map is the launcher's exe-path lookup, only for games
        with a local executable (xCloud titles need no row). Library-
        sourced sync ``Game`` objects do NOT carry ``exe_path`` (it's
        resolved at install time by the worker via ``mark_installed``),
        so an installed game arriving with an empty ``exe`` must NOT
        wipe its existing entry — only a truly-uninstalled game drops it.
        """
        exe = game.exe_path or ""
        if game.installed and exe:
            self._games_map[key] = GameMapEntry(
                exe=self._durable_exe(key, exe),
                work_dir=game.install_path or "",
                app_id=app_id,
            )
        elif not (game.installed and key in self._games_map):
            self._games_map.pop(key, None)

    def _durable_exe(self: Any, key: str, store_exe: str) -> str:
        """The exe to record: an established one wins while it still exists.

        The row is also where "Change executable" stores the user's choice
        for the direct-launch stores, so overwriting it from the library
        silently reverts that choice on the next sync. GOG hit this: its
        ``get_library`` carries ``exe_path`` from a fresh disk scan
        (``exe_key="executable"``) because it must discover installs Unifideck
        did not perform, and the scan knows nothing about what the user
        picked. Amazon and Epic were unaffected only because neither carries
        an exe at all — which is what made the bug invisible: the guard below
        was written on the docstring's claim that *no* library-sourced game
        carries one.

        The disk check is what keeps this from going stale. A recorded exe
        that no longer exists means the install moved or was replaced, and
        then the store's fresh value is the better answer. "Reset to default"
        (``reset_game_executable``) remains the explicit way back.
        """
        existing = self._games_map.get(key)
        recorded = getattr(existing, "exe", "") or ""
        if not recorded or recorded == store_exe:
            return store_exe
        if not Path(recorded).is_file():
            logger.info(
                "[ShortcutService] %s recorded exe is gone, taking the "
                "store's: %s", key, store_exe,
            )
            return store_exe
        logger.debug(
            "[ShortcutService] %s keeping established exe %s (store offered "
            "%s)", key, recorded, store_exe,
        )
        return recorded

    def _try_reclaim_orphan(
        self: Any,
        shortcuts_dict: dict[str, Any],
        registry: dict[str, Any],
        game: Game,
        key: str,
    ) -> bool:
        """Reclaim an orphaned shortcut via the registry; True if reclaimed."""
        from .registry import get_registered_appid, register
        registered = get_registered_appid(registry, key)
        if registered is None:
            return False
        launcher = getattr(self, "_launcher_path", "") or ""
        ord_key = self._find_existing_shortcut_key(
            shortcuts_dict, registered, launcher,
        )
        if ord_key is None:
            return False
        self._reclaim_orphan(shortcuts_dict[ord_key], game, registered)
        register(registry, key, registered, game.title)
        return True

    def _update_existing_shortcut(
        self: Any,
        entry: dict[str, Any],
        game: Game,
        app_id: int,
        launcher: str,
    ) -> None:
        """Force-update an existing shortcut in-place — preserves ``appid``.

        Only called during force sync (``force=True``).
        Updates ``AppName``, ``Exe``, ``tags``, and ``LaunchOptions``
        while leaving ``appid`` unchanged so artwork files and Steam's
        playtime tracking survive the rewrite. The ``icon`` field is
        set from ``game.icon_url`` when available (the artwork phase
        populates it from on-disk grid files).

        ``LaunchOptions`` keeps the user's own params. This used to be a
        plain overwrite to ``"<store>:<id>"``, which silently deleted
        anything they had configured -- and since 0.7.5 that includes the
        supported way to enable LSFG and to set per-game environment
        variables (``docs/launch-options.md``). A setting that a Force Sync
        erases is not a setting, and Force Sync is exactly what a user is
        told to run when something looks wrong. ``_reclaim_orphan`` eleven
        lines below already did this correctly; this now matches it.

        Our own ``UNIFIDECK_*`` flags are stripped rather than preserved --
        see :func:`~.launch_options.strip_unifideck_env_tokens` for why
        that half is not optional.
        """
        from .launch_options import get_full_id, rewrite_for_sync
        from .protected import is_protected

        current_options = entry.get("LaunchOptions", "")

        # Defence in depth. This is only ever called for a library game, but
        # the appid fallback that can select the entry matches on appid +
        # ``is_ours`` and never consults the protected set -- and an auth
        # forwarder is ours. Rewriting one turns a sign-in tile into a game
        # tile, which is unrecoverable without a re-auth. The previous plain
        # overwrite was just as destructive here, so this is not a new
        # hazard, but it is a cheap one to close while in the area.
        existing_id = get_full_id(current_options)
        if existing_id is not None and is_protected(existing_id):
            logger.warning(
                "[Reconcile] refusing to rewrite protected shortcut %s "
                "as game %s:%s",
                existing_id, game.store, game.store_game_id,
            )
            return

        exe_quoted = f'"{launcher}"' if launcher else '""'
        entry["AppName"] = game.title
        entry["Exe"] = exe_quoted
        entry["LaunchOptions"] = rewrite_for_sync(
            current_options, f"{game.store}:{game.store_game_id}",
        )
        if game.icon_url:
            entry["icon"] = game.icon_url
        tags_dict = entry.get("tags", {})
        if isinstance(tags_dict, dict):
            tags_dict["0"] = UNIFIDECK_TAG
            tags_dict["1"] = game.store
            tags_dict["2"] = "" if game.installed else "Not Installed"

    def _reclaim_orphan(
        self: Any, entry: dict[str, Any], game: Game, app_id: int,
    ) -> None:
        """Rewrite ``entry`` in place — restores ownership while keeping AppID.

        Keeps the user's launch params and drops our own ``UNIFIDECK_*``
        flags, matching ``_update_existing_shortcut``. An orphan is a
        shortcut we lost track of, so it is *more* likely than most to be
        carrying a stranded action flag, not less.
        """
        from .launch_options import rewrite_for_sync

        launcher = getattr(self, "_launcher_path", "") or ""
        target = f"{game.store}:{game.store_game_id}"
        preserved = rewrite_for_sync(entry.get("LaunchOptions", ""), target)
        entry["appid"] = app_id
        entry["AppName"] = game.title
        if launcher:
            entry["Exe"] = f'"{launcher}"'
        entry["LaunchOptions"] = preserved
        entry["icon"] = game.icon_url or entry.get("icon", "") or ""
        entry["tags"] = {
            "0": UNIFIDECK_TAG,
            "1": game.store,
            "2": "" if game.installed else "Not Installed",
        }

    def _reconcile_phase_drop_stale(
        self: Any,
        shortcuts_dict: dict[str, Any],
        valid_app_ids: set[int],
        valid_stores: SweepableStores | None = None,
        launcher_path: str = "",
    ) -> int:
        """Phase 3: delete Unifideck-managed shortcuts no longer needed."""
        keys_to_delete = [
            vdf_key for vdf_key, entry in shortcuts_dict.items()
            if is_stale_managed_shortcut(
                entry, valid_app_ids, valid_stores, launcher_path,
            )
        ]
        for key in keys_to_delete:
            del shortcuts_dict[key]
        return len(keys_to_delete)

    def _build_shortcut_entry(
        self: Any, game: Game, app_id: int,
    ) -> dict[str, Any]:
        """Construct a shortcuts.vdf entry dict for ``game``.

        Every Unifideck-managed shortcut points its ``Exe`` at the
        plugin's ``bin/unifideck-launcher`` script and stores the
        ``"<store>:<store_game_id>"`` token in ``LaunchOptions``.
        Anchoring on the launcher keeps the AppID stable across
        install / uninstall transitions.
        """
        launcher = getattr(self, "_launcher_path", "") or ""
        exe_quoted = f'"{launcher}"' if launcher else '""'
        start_dir = f'"{game.install_path}"' if game.install_path else '""'
        launch_options = f"{game.store}:{game.store_game_id}"
        cover_icon = game.icon_url or ""
        return {
            "appid": app_id,
            "AppName": game.title,
            "Exe": exe_quoted,
            "StartDir": start_dir,
            "icon": cover_icon,
            "ShortcutPath": "",
            "LaunchOptions": launch_options,
            "IsHidden": 0,
            "AllowDesktopConfig": 1,
            "AllowOverlay": 1,
            "OpenVR": 0,
            "Devkit": 0,
            "DevkitGameID": "",
            "DevkitOverrideAppID": 0,
            # New shortcuts start with no play history (0 = never played).
            # Steam stamps the real time on first launch, and
            # ``_update_existing_shortcut`` preserves it on later syncs.
            # Hardcoding ``time.time()`` here stamped every game with the
            # same sync timestamp, so the App-Details "Last Played" row
            # showed one identical date across the whole library.
            "LastPlayTime": 0,
            "FlatpakAppID": "",
            "tags": {
                "0": UNIFIDECK_TAG,
                "1": game.store,
                "2": "" if game.installed else "Not Installed",
            },
        }
