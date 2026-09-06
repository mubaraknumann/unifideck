"""Full-cleanup RPC machinery for :class:`SyncRPCMixin`.

Extracted from ``rpc/mixins/sync.py`` to keep that file under the
550-LOC volumetry cap. Holds the "Delete all Unifideck data" flow —
shortcut removal, grid-artwork wipe, store sign-out, credential
deletion, and cache clearing — plus the small static helpers that
collect the Unifideck-owned app_id set from the persisted shortcut
state. ``SyncRPCMixin`` mixes this in, so the public RPC surface
(``perform_full_cleanup``) is unchanged.
"""
from __future__ import annotations

import logging
from typing import Any

from unifideck.core import cleanup_sweeps, marker_sweep
from unifideck.core.compat_bridge import to_unsigned
from unifideck.core.safe_delete import safe_rmtree
from unifideck.rpc.mixins.cleanup_finalize import _CleanupFinalizeMixin

logger = logging.getLogger(__name__)

class CleanupRPCMixin(_CleanupFinalizeMixin):
    """"Delete all Unifideck data" flow + its app_id collectors."""

    services: Any
    cache: Any
    registry: Any
    # Consumed by ``_CleanupFinalizeMixin``; the concrete Plugin provides
    # both (``SyncRPCMixin`` declares ``sync_service``, ``self.bus.emit`` is
    # the established RPC-layer emit path).
    sync_service: Any
    bus: Any

    async def scan_orphaned_shortcuts(self) -> dict[str, Any]:
        """Detect orphaned Unifideck shortcuts for the frontend to sweep.

        Reads ``shortcuts.vdf`` (read-only — never rewrites it) and
        classifies each entry via
        :func:`unifideck.services.shortcut.orphan_scan.scan_orphans`:

        * ``delete`` — ``Exe`` is our launcher but ``LaunchOptions`` has no
          resolvable ``"<store>:<game_id>"`` (Type A, unrecoverable). The
          frontend removes these live via ``SteamClient.Apps.RemoveShortcut``
          (no Steam restart needed).
        * ``recover`` — valid id but a missing/foreign ``Exe`` (Type B). Not
          fixable here (the frontend has no ``SetShortcutExe``); reported for
          logging. The next library sync's reconcile restores the target
          (in-library) or sweeps it (out-of-library).

        The backend deliberately does NOT write ``shortcuts.vdf`` — a disk
        write would clobber Steam's in-memory shortcut set and reintroduce a
        restart requirement. Returns the raw data dict; the RPC wrapper adds
        the envelope.
        """
        from unifideck.services.shortcut.orphan_scan import scan_orphans

        shortcut_svc = getattr(self.services, "shortcut", None)
        if shortcut_svc is None:
            raise RuntimeError("shortcut service unavailable")

        await shortcut_svc._load_shortcuts()
        shortcuts = getattr(shortcut_svc, "_shortcuts", None) or {}
        root = (
            shortcuts.get("shortcuts") if isinstance(shortcuts, dict) else {}
        )
        launcher_path = getattr(shortcut_svc, "_launcher_path", "") or ""

        result = scan_orphans(root, launcher_path)
        logger.info(
            "[orphan-scan] %d to delete, %d to recover",
            len(result["delete"]), len(result["recover"]),
        )
        # ``scan_orphans`` is typed ``dict[str, list[dict[str, Any]]]``; the RPC
        # response also carries the launcher path (a ``str``), so widen into a
        # fresh ``dict[str, Any]`` rather than mutating the narrow scan result.
        payload: dict[str, Any] = dict(result)
        payload["launcher_path"] = launcher_path
        return payload

    async def _delete_install_dir(self, install_dir: str) -> bool:
        """rm -rf a recorded game install directory.

        ``install_dir`` is a ``games.map`` ``work_dir`` — i.e. a directory we
        recorded for a game we manage — so any location is fair game (SD
        card, ``/mnt`` libraries, user-picked folders). The old substring
        allowlist (``/Games/``, ``/Epic`` …) silently skipped custom paths;
        :func:`safe_rmtree` instead guards structurally (rejects ``/``,
        ``$HOME``, ancestors of ``$HOME``, and shallow paths).
        """
        import asyncio
        from pathlib import Path

        if not install_dir:
            return False
        if not await asyncio.to_thread(Path(install_dir).is_dir):
            return False
        return await asyncio.to_thread(safe_rmtree, install_dir)

    @staticmethod
    def _collect_unifideck_app_ids(shortcut_svc: Any) -> list[int]:
        """Union of app_ids from shortcuts.vdf (tagged) and games.map.

        Pulls the canonical Unifideck-owned app_id set from the
        persisted shortcut state — not the volatile sync cache —
        so cleanup works even when no sync has run this session.
        """
        ids: set[int] = set()
        ids.update(CleanupRPCMixin._collect_ids_from_shortcuts_vdf(shortcut_svc))
        ids.update(CleanupRPCMixin._collect_ids_from_games_map(shortcut_svc))
        return sorted(ids)

    @staticmethod
    def _collect_ids_from_shortcuts_vdf(shortcut_svc: Any) -> set[int]:
        """Walk ``shortcuts.vdf`` and return appids of Unifideck entries."""
        from unifideck.services.shortcut.games_map import UNIFIDECK_TAG
        from unifideck.services.shortcut.launch_options import is_unifideck_shortcut

        ids: set[int] = set()
        shortcuts = getattr(shortcut_svc, "_shortcuts", None) or {}
        root = shortcuts.get("shortcuts") if isinstance(shortcuts, dict) else None
        if not isinstance(root, dict):
            return ids
        launcher = getattr(shortcut_svc, "_launcher_path", "") or ""
        for entry in root.values():
            if not isinstance(entry, dict):
                continue
            if not cleanup_sweeps.is_unifideck_owned(
                entry, UNIFIDECK_TAG, is_unifideck_shortcut, launcher,
            ):
                continue
            app_id = entry.get("appid")
            if isinstance(app_id, int):
                ids.add(app_id)
        return ids

    @staticmethod
    def _collect_ids_from_games_map(shortcut_svc: Any) -> set[int]:
        """Pull non-zero appids out of the games.map manifest."""
        ids: set[int] = set()
        games_map = getattr(shortcut_svc, "_games_map", None) or {}
        if not isinstance(games_map, dict):
            return ids
        for entry in games_map.values():
            app_id = getattr(entry, "app_id", 0)
            if isinstance(app_id, int) and app_id != 0:
                ids.add(app_id)
        return ids

    async def _cleanup_one_app_id(
        self,
        app_id: int,
        delete_files: bool,
        shortcut_svc: Any,
    ) -> tuple[int | None, bool]:
        """Per-app_id cleanup: returns (removed_app_id_or_None, files_deleted)."""
        install_dir: str | None = None
        if delete_files:
            games_map = getattr(shortcut_svc, "_games_map", None) or {}
            if isinstance(games_map, dict):
                for entry in games_map.values():
                    if getattr(entry, "app_id", 0) == app_id:
                        install_dir = getattr(entry, "work_dir", None)
                        break

        removed_id: int | None = None
        try:
            if await shortcut_svc.remove_game(app_id):
                removed_id = app_id
        except Exception:
            logger.exception("[cleanup] remove_game(%s) failed", app_id)

        files_deleted = False
        if delete_files and install_dir:
            files_deleted = await self._delete_install_dir(install_dir)
        return removed_id, files_deleted

    @staticmethod
    def _nonunifideck_unsigned_appids(shortcut_svc: Any) -> set[int]:
        """Unsigned appids of currently-present *non-Unifideck* shortcuts.

        These back other launchers' shortcuts (Heroic, manually-added
        apps, …) that happen to live in the same ``grid/`` dir. They are
        the wipe's protected set — everything else with a non-Steam
        (≥ 2³¹) appid is fair game.
        """
        from unifideck.services.shortcut.games_map import UNIFIDECK_TAG
        from unifideck.services.shortcut.launch_options import (
            is_unifideck_shortcut,
        )

        keep: set[int] = set()
        shortcuts = getattr(shortcut_svc, "_shortcuts", None) or {}
        root = shortcuts.get("shortcuts") if isinstance(shortcuts, dict) else None
        if not isinstance(root, dict):
            return keep
        launcher = getattr(shortcut_svc, "_launcher_path", "") or ""
        for entry in root.values():
            if not isinstance(entry, dict):
                continue
            if cleanup_sweeps.is_unifideck_owned(
                entry, UNIFIDECK_TAG, is_unifideck_shortcut, launcher,
            ):
                continue
            app_id = entry.get("appid")
            if isinstance(app_id, int):
                keep.add(to_unsigned(app_id))
        return keep

    async def _delete_nonsteam_artwork(self, keep_appids: set[int]) -> int:
        """Wipe every non-Steam grid artwork file, except ``keep_appids``.

        "Delete all Unifideck data" should clear artwork outright —
        including art orphaned by past removals that no longer maps to
        any shortcut — so other launchers' live art (``keep_appids``)
        survives but everything else non-Steam is removed.
        """
        import asyncio

        artwork = getattr(self.services, "artwork", None)
        grid_dir = getattr(artwork, "grid_dir", None) if artwork else None
        if not grid_dir:
            logger.warning(
                "[cleanup] artwork service unavailable; skipping grid wipe",
            )
            return 0
        return await asyncio.to_thread(
            cleanup_sweeps.sweep_nonsteam_grid, grid_dir, keep_appids,
        )

    async def _logout_all_stores(self) -> int:
        """Sign out of every store via the registry's logout flow.

        Reuses :meth:`StoreRegistry.logout_all` — the same code path
        the per-store "Sign out" buttons use. Returns the count of
        stores that reported a successful logout.
        """
        registry = getattr(self, "registry", None)
        if registry is None:
            return 0
        try:
            results = await registry.logout_all()
        except Exception:
            logger.exception("[cleanup] registry.logout_all failed")
            return 0
        if not isinstance(results, dict):
            return 0
        # ``logout_all`` maps each store to ``{"success", "error"}`` —
        # count only the entries that actually reported success (a
        # non-empty dict is always truthy, so ``if v`` would over-count).
        return sum(
            1
            for v in results.values()
            if isinstance(v, dict) and v.get("success")
        )

    def _reset_store_availability(self) -> None:
        """Clear the in-memory ``_cached_available`` flag on every store.

        ``check_store_status`` re-probes live, but ``get_store_infos``
        and other surfaces read the cached flag — resetting it makes the
        settings badges reflect signed-out immediately after a wipe,
        without waiting for the next availability probe.
        """
        registry = getattr(self, "registry", None)
        stores = getattr(registry, "_stores", None)
        if not isinstance(stores, dict):
            return
        for store in stores.values():
            try:
                store._cached_available = False
            except Exception:
                logger.exception(
                    "[cleanup] reset _cached_available failed for %s",
                    getattr(store, "store_name", "?"),
                )

    async def _delete_auth_data(self) -> int:
        """Delete every store's persisted auth data + stray temp files.

        Belt-and-suspenders on top of ``registry.logout_all`` — each
        store's ``logout`` *should* clear its own credentials, but it
        no-ops when the auth submodule isn't wired yet and its CLI
        logout swallows timeout/OS errors. Deleting the credential
        files the ``is_available`` probes read guarantees the stores
        report signed-out afterward.
        """
        import asyncio

        return await asyncio.to_thread(cleanup_sweeps.sweep_auth_data)

    async def perform_full_cleanup(
        self, delete_files: bool = False,
    ) -> dict[str, Any]:
        """Wipe every Unifideck-managed shortcut, artwork, auth, and cache.

        Pulls the Unifideck app_id set from the persisted shortcut
        state (``shortcuts.vdf`` + ``games.map``) — not the volatile
        sync cache — so cleanup works even when no sync has run in
        the current process. Removes shortcuts (+ optional install
        dirs under a safe root), wipes orphaned non-Steam grid art,
        signs out of every store, deletes persisted credentials, and
        clears every cache namespace.

        Returns the result data only (no ``success``/``error`` keys);
        the RPC wrapper adds the envelope. On failure, raises
        ``RuntimeError`` and the wrapper converts it to a typed
        ``internal_error`` envelope.
        """
        logger.info("[cleanup] starting (delete_files=%s)", delete_files)
        shortcut_svc = getattr(self.services, "shortcut", None)
        if shortcut_svc is None:
            raise RuntimeError("shortcut service unavailable")

        await shortcut_svc._load_shortcuts()
        await shortcut_svc._load_games_map()

        import asyncio

        # Collect the library roots from install records *now* — the data-dir
        # wipe below deletes games.map (one of the sources). The marked-dir
        # sweep that uses them runs as the very last step, after all other
        # deletion, to avoid racing the rest of the wipe.
        install_roots = (
            await asyncio.to_thread(marker_sweep.collect_install_roots)
            if delete_files
            else set()
        )

        app_ids = self._collect_unifideck_app_ids(shortcut_svc)
        logger.info("[cleanup] %d candidate app_ids", len(app_ids))
        deleted_app_ids, deleted_files_count = await self._remove_shortcuts(
            shortcut_svc, app_ids, delete_files,
        )
        wiped = await self._wipe_residual_data(shortcut_svc)
        residual_total = await self._purge_local_state(delete_files)

        # LAST step (destructive only): delete every Unifideck-marked install
        # dir under the recorded roots — orphan stubs, out-of-default-root
        # games, and custom locations that the games.map pass above can't
        # reach. Runs after everything else so it never races the wipe.
        marked = (
            await asyncio.to_thread(marker_sweep.sweep_all, install_roots)
            if delete_files
            else 0
        )

        # Only now is the live process told: drop the in-memory library and
        # size memo, prune the CLI install records the sweep above needed
        # intact, and announce each cleared game so the UI stops showing
        # games whose files are gone. Best-effort — never raises.
        pruned = await self._finalize_wipe(delete_files)

        logger.info("[cleanup] complete")
        return {
            "deleted_games": len(deleted_app_ids),
            "deleted_files_count": deleted_files_count + marked,
            "deleted_artwork_count": wiped["artwork"],
            "logged_out_count": wiped["logged_out"],
            "deleted_stray_files_count": wiped["stray"],
            "deleted_residual_count": residual_total + pruned,
            "deleted_app_ids": deleted_app_ids,
        }

    async def _purge_local_state(self, delete_files: bool) -> int:
        """Delete residual data-dir state, config creds, and clear caches.

        Returns the total residual entry count (data-dir + config-auth +
        external prefixes). Grouped out of ``perform_full_cleanup`` so that
        orchestrator stays under the fan-out gate.

        Both modes wipe residual state + Unifideck-owned store creds the old
        flow left behind (library cache, shortcut registry, download history,
        playtime, Ubisoft maps/db/cache, GOG refresh token, …). Destructive
        additionally removes prefixes/ + saves/ (the ~20 GB).
        """
        # Destructive: external (SD/custom) Ubisoft prefixes are recorded in
        # the id_map, which the data-dir wipe is about to delete — read and
        # remove those out-of-tree prefixes first so they aren't orphaned.
        external_prefixes = (
            await self._delete_external_prefixes() if delete_files else 0
        )
        residual = await self._wipe_data_dir(delete_files)
        residual += await self._wipe_config_auth()
        self._clear_all_caches()
        return residual + external_prefixes

    async def _remove_shortcuts(
        self, shortcut_svc: Any, app_ids: list[int], delete_files: bool,
    ) -> tuple[list[int], int]:
        """Remove every candidate shortcut (+ optional install dir).

        Suppresses the per-removal artwork handler for the duration of
        the loop: each ``remove_game`` emits ``SHORTCUT_REMOVED`` (whose
        handlers are awaited), which would otherwise glob-and-delete the
        grid dir once per game. The single ``_delete_nonsteam_artwork``
        sweep clears all of it — current art AND orphans — in one pass.
        """
        deleted_app_ids: list[int] = []
        deleted_files_count = 0
        artwork_svc = getattr(self.services, "artwork", None)
        if artwork_svc is not None:
            artwork_svc._suppress_removal_cleanup = True
        try:
            for app_id in app_ids:
                removed_id, files_deleted = await self._cleanup_one_app_id(
                    app_id, delete_files, shortcut_svc,
                )
                if removed_id is not None:
                    deleted_app_ids.append(removed_id)
                if files_deleted:
                    deleted_files_count += 1
        finally:
            if artwork_svc is not None:
                artwork_svc._suppress_removal_cleanup = False
        logger.info(
            "[cleanup] removed %d shortcuts, %d install dirs",
            len(deleted_app_ids), deleted_files_count,
        )
        return deleted_app_ids, deleted_files_count

    async def _wipe_residual_data(self, shortcut_svc: Any) -> dict[str, int]:
        """Wipe orphaned artwork, sign out stores, delete auth, reset flags.

        Shortcut removal already dropped our entries from ``_shortcuts``,
        so the artwork keep-set is exactly the foreign shortcuts that
        remain (other launchers' live art).
        """
        keep_appids = self._nonunifideck_unsigned_appids(shortcut_svc)
        artwork = await self._delete_nonsteam_artwork(keep_appids)
        logged_out = await self._logout_all_stores()
        stray = await self._delete_auth_data()
        self._reset_store_availability()
        logger.info(
            "[cleanup] artwork=%d logged_out=%d stray=%d",
            artwork, logged_out, stray,
        )
        return {"artwork": artwork, "logged_out": logged_out, "stray": stray}

    def _clear_all_caches(self) -> None:
        """Clear every registered cache namespace."""
        for name in list(getattr(self.cache, "_stores", {}).keys()):
            try:
                self.cache.clear(name)
            except Exception:
                logger.exception("[cleanup] cache.clear(%s) failed", name)

    # Names directly under ``~/.local/share/unifideck`` that hold playable
    # game data — kept in non-destructive mode so "installed games stay on
    # disk and can be re-synced" (per the modal). Destructive removes these
    # too (this is where the ~20 GB of Proton prefixes lives).
    # Non-destructive cleanup keeps the games, so it must also keep the only
    # index into them. A wrapper store's id map is what maps a game uid to
    # the prefix its files live in — and those resolvers deliberately never
    # guess a path from a uid (a reconstructed path once stamped a marker
    # into a directory no launch opened, wedging the prefix in a reset loop).
    # Deleting the map while keeping ``prefixes/`` therefore turned every
    # installed wrapper-store game into an unreachable multi-GB orphan:
    # exactly the stale data this flow exists to remove.
    _DATA_DIR_KEEP_WHEN_KEEPING_GAMES = frozenset(
        {
            "prefixes",
            "saves",
            "save_backups",
            "ubisoft_id_map.json",
            "battlenet_id_map.json",
        },
    )

    async def _wipe_data_dir(self, delete_files: bool) -> int:
        """Delete residual state under ``~/.local/share/unifideck``.

        Both modes wipe everything the old flow left behind — library cache,
        shortcut registry, download queue/history, playtime/activity dbs,
        Ubisoft maps/db/installer cache, settings, launch logs — so the
        modal's "removes cached data" promise actually holds. Non-destructive
        preserves :data:`_DATA_DIR_KEEP_WHEN_KEEPING_GAMES`; destructive keeps
        nothing, reclaiming the prefixes and local saves.

        Iterating-and-deleting (rather than an explicit unlink list) means new
        state files added later are swept automatically — the wipe stays
        complete by construction.
        """
        import asyncio

        keep = (
            frozenset()
            if delete_files
            else self._DATA_DIR_KEEP_WHEN_KEEPING_GAMES
        )
        wiped = await asyncio.to_thread(cleanup_sweeps.sweep_data_dir, keep)
        logger.info(
            "[cleanup] data-dir wipe removed %d entries (delete_files=%s)",
            wiped, delete_files,
        )
        return wiped

    async def _delete_external_prefixes(self) -> int:
        """Delete per-game prefixes recorded *outside* the data dir.

        Ubisoft games installed to SD/custom storage record an absolute
        ``prefix_path`` in ``ubisoft_id_map.json`` that lives outside
        ``~/.local/share/unifideck/prefixes`` (e.g. ``~/Games/prefixes/...``
        or a microSD mount), so the blanket data-dir wipe never reaches them.
        Read the map and delete each external prefix before the map file is
        itself removed. Destructive-only.
        """
        import asyncio

        wiped = await asyncio.to_thread(cleanup_sweeps.sweep_external_prefixes)
        if wiped:
            logger.info("[cleanup] removed %d external prefixes", wiped)
        return wiped

    async def _wipe_config_auth(self) -> int:
        """Delete Unifideck-owned store creds under ``~/.config/unifideck``.

        The old auth sweep targeted ``gogdl/gog_credentials.json``, but the
        live GOG refresh token sits at ``gog_credentials.json`` /
        ``gogdl_auth.json`` (top level), so a GOG login survived "Delete all
        data". Remove those (and the Unifideck gogdl config dir). Leaves the
        user's ``config.json`` and Heroic's ``heroic_gogdl`` untouched.
        """
        import asyncio

        return await asyncio.to_thread(cleanup_sweeps.sweep_config_auth)
