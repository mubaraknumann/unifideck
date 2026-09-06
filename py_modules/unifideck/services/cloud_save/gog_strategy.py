import asyncio
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any

from unifideck.core.binaries import clean_cli_env
from unifideck.launcher.proton.infrastructure.prefix_layout import resolve_drive_c
from unifideck.services.cloud_save import safety
from unifideck.services.cloud_save.gog_cloud_api import (
    GOG_DEFAULT_NAMESPACE,
    fetch_gog_client_id,
    head_object_local_mtime,
    list_cloud_objects,
    resolve_gog_save_locations,
    select_primary_save_target,
    summarize_cloud_objects,
)
from unifideck.services.cloud_save.gog_state_mixin import GOGStateMixin
from unifideck.services.cloud_save.path_resolver import find_save_dir_by_title
from unifideck.services.cloud_save.strategy_base import CloudSaveStrategy
from unifideck.stores.gog.galaxy_api import (
    exchange_game_token,
    fetch_gog_client_creds,
)

logger = logging.getLogger(__name__)


class GOGCloudSaveStrategy(GOGStateMixin, CloudSaveStrategy):
    """Cloud save strategy for GOG games using gogdl.

    On-disk state (resolved save dirs + sync watermarks) and GOG credential
    decryption live in :class:`GOGStateMixin` (``gog_state_mixin.py``).
    """

    store_id = "gog"

    def __init__(
        self, local_save_root: str, config: Any = None, cache: Any = None,
    ) -> None:
        super().__init__(local_save_root, config, cache)
        # GOG-private in-memory cache of the *metadata*-resolved save dir (the
        # base owns the top-level resolved-dir memo). Backed by an on-disk
        # cache (``gog_save_dirs``) so the network round-trip survives restarts.
        self._cached_metadata_dir: dict[str, str] = {}
        # The cloud-storage namespace (gogdl ``--name``) for the primary dir,
        # captured alongside it so sync_down/up target the right objects.
        self._cached_namespace: dict[str, str] = {}
        # ALL of a game's ``(dir, namespace)`` cloud-save targets (a game can
        # split saves across several, e.g. BioShock Remastered's
        # ``saves``+``saves2``); the whole set is synced, not just the primary.
        self._cached_targets: dict[str, list[tuple[str, str]]] = {}

        # Resolve path to the bundled gogdl binary. Use the canonical
        # plugin-root resolver (same one the launch path uses) — bin/ is a
        # SIBLING of py_modules, so naive dirname-walking from this file
        # lands on py_modules and misses bin/, falling back to a bare
        # "gogdl" that isn't on the launcher's PATH.
        from unifideck.core.paths import resolve_plugin_dir
        plugin_dir = str(resolve_plugin_dir(start=Path(__file__)))
        self.gogdl_bin = os.path.join(plugin_dir, "bin", "gogdl")
        if not os.path.exists(self.gogdl_bin):
            self.gogdl_bin = "gogdl"

    def _install_dir(self, game_id: str) -> str:
        """The game's install dir (games.map ``work_dir``), or ``""``."""
        from unifideck.services.cloud_save.save_location_resolver import (
            _install_path_from_games_map,
        )
        return _install_path_from_games_map(self.store_id, game_id, self.config)

    def _is_native_linux(self, game_id: str) -> bool:
        """True if this GOG game is installed as a NATIVE-Linux build.

        The installer prefers the Linux build (``("linux","windows")``) and
        those launch via a root ``start.sh`` wrapper (same signal the launcher
        dispatcher uses). Native games have NO Wine prefix and save to real
        ``$HOME``/XDG dirs, so they need Linux-mode resolution.
        """
        install = self._install_dir(game_id)
        return bool(install) and os.path.isfile(os.path.join(install, "start.sh"))

    def _resolve_store_save_dir(self, game_id: str) -> str | None:
        """GOG-specific save-dir resolution (config override + memo in base).

        Native-Linux builds (no Wine prefix) resolve via the enriched
        Linux-tagged metadata against real ``$HOME``/XDG dirs. Otherwise, for
        Windows-prefix (Proton) games, resolution order (first hit wins):
          1. **GOG cloud-save metadata** (authoritative): the location
             template from ``remote-config.gog.com`` resolved against the
             game's Wine prefix — i.e. where the game actually reads/writes
             saves. This is what Galaxy/Heroic use.
          2. Enriched save-location metadata (unifiDB/PCGamingWiki via Ludusavi).
          3. Heuristic title-match of an existing folder in the prefix.
        Returns ``None`` when no real location exists yet.
        """
        if self._is_native_linux(game_id):
            # No Wine prefix; GOG remote-config cloudStorage is Windows/Mac
            # only — resolve the Linux save path from enriched metadata.
            return self._resolve_enriched(
                game_id, prefix_path="",
                install_path=self._install_dir(game_id), native_linux=True,
            )

        drive_c = resolve_drive_c(self._prefix_root(game_id))

        if drive_c:
            # 1. Authoritative: resolve from GOG's cloud-save config.
            meta_dir = self._resolve_save_dir_from_metadata(game_id, drive_c)
            if meta_dir:
                return meta_dir

            # 2. Enriched save-location metadata (unifiDB / PCGamingWiki via
            # Ludusavi). More reliable than the title-folder scan below, but
            # below GOG's own authoritative config above so it can't regress.
            # ``prefix_path`` is drive_c's parent (the registry-prefix root).
            enriched = self._resolve_enriched(
                game_id, prefix_path=str(drive_c.parent),
            )
            if enriched:
                return enriched

            # 3. Heuristic: match an existing folder by game title.
            title_dir = self._resolve_by_title(game_id, drive_c)
            if title_dir:
                return title_dir

        # No real save location found — e.g. the game was never launched so no
        # Wine prefix exists yet. Return None (NOT a staging dir the game never
        # reads); callers treat None as "unresolved" and skip syncing instead
        # of stranding saves in a folder nothing uses.
        logger.info("[GOGSync] No save dir resolved for %s (no prefix yet)", game_id)
        return None

    def _resolve_by_title(self, game_id: str, drive_c: Path) -> str | None:
        """Heuristic: match an existing prefix subfolder by game title."""
        game_title = (
            self.config.get(f"games.{game_id}.title") or ""
        ) if self.config else ""
        return find_save_dir_by_title(drive_c, game_title, tag="GOGSync")

    # ── GOG cloud-save location resolution (from GOG metadata) ───────
    def _resolve_save_dir_from_metadata(
        self, game_id: str, drive_c: Path,
    ) -> str | None:
        """Resolve the in-prefix save dir from GOG's cloud-save config.

        Fetches the game's ``clientId`` then its cloudStorage location
        template (e.g. ``<?DOCUMENTS?>\\The Witcher 3``) and resolves the
        path variable against the prefix. The result is created and cached
        (in-process + on disk) so the network round-trip happens once per
        game. Returns None on any failure so the caller can fall back.
        """
        cached_mem = self._cached_metadata_dir.get(game_id)
        if cached_mem:
            return cached_mem
        # On-disk cache may be a legacy path-only entry (no namespace) written
        # before the gogdl ``--name`` fix; treat that as a partial hit and
        # re-resolve to capture the namespace, keeping the path as a fallback.
        disk = self._read_cached_save_dir(game_id)
        legacy_path: str | None = None
        if disk:
            path, name = disk
            if name:
                self._cached_metadata_dir[game_id] = path
                self._cached_namespace[game_id] = name
                return path
            legacy_path = path
        try:
            client_id = fetch_gog_client_id(game_id)
            if not client_id:
                return legacy_path
            # install_path resolves ``<?INSTALL?>``-style tokens (older GOG
            # titles save next to their files) — same games.map ``work_dir``
            # source the Ludusavi ``<base>`` tier uses.
            from unifideck.services.cloud_save.save_location_resolver import (
                _install_path_from_games_map,
            )
            install_path = _install_path_from_games_map(
                self.store_id, game_id, self.config,
            )
            targets = resolve_gog_save_locations(client_id, drive_c, install_path)
            primary = select_primary_save_target(targets)
            if primary is None:
                return legacy_path
            chosen, namespace = primary
            chosen.mkdir(parents=True, exist_ok=True)
            path = str(chosen)
            target_pairs = [(str(d), n) for d, n in targets]
            self._cached_metadata_dir[game_id] = path
            self._cached_namespace[game_id] = namespace
            self._cached_targets[game_id] = target_pairs
            self._write_cached_save_dir(game_id, path, namespace, target_pairs)
            logger.info(
                "[GOGSync] Resolved save dir from GOG metadata: %s "
                "(cloud namespace %r; %d sync target(s))",
                path, namespace, len(target_pairs),
            )
            return path
        except Exception as e:
            logger.warning(
                "[GOGSync] GOG metadata save-dir resolution failed for %s: %s",
                game_id, e,
            )
            return legacy_path

    def _resolve_cloud_namespace(self, game_id: str) -> str:
        """The gogdl ``--name`` cloud namespace for ``game_id``'s save dir.

        Captured during metadata resolution (in-memory, then the on-disk
        ``gog_save_dirs`` cache). Falls back to ``__default`` — gogdl's own
        default and the correct namespace for SDK-IStorage games — when the
        dir was resolved by a non-metadata tier (config override, enriched /
        title heuristic) or by an older build before this was cached.
        """
        name = self._cached_namespace.get(game_id)
        if name:
            return name
        disk = self._read_cached_save_dir(game_id)
        if disk and disk[1]:
            self._cached_namespace[game_id] = disk[1]
            return disk[1]
        return GOG_DEFAULT_NAMESPACE

    def _resolve_sync_targets(
        self, game_id: str, primary_dir: str,
    ) -> list[tuple[str, str]]:
        """Every ``(dir, namespace)`` to sync for ``game_id``.

        The full set from GOG metadata (cached) when available — a game can
        keep saves in several locations (BioShock Remastered: ``saves`` in
        Documents + ``saves2`` in Roaming). Falls back to the single
        ``primary_dir`` + its namespace for dirs resolved by a non-metadata
        tier (config override, enriched / title heuristic) or an older cache.
        """
        targets = self._cached_targets.get(game_id)
        if targets is None:
            rec = self._read_cached_record(game_id)
            if rec and rec.get("targets"):
                targets = [(d, n) for d, n in rec["targets"]]
                self._cached_targets[game_id] = targets
        if targets:
            return targets
        return [(primary_dir, self._resolve_cloud_namespace(game_id))]

    # ── Real GOG cloud-save info (cloudstorage.gog.com LIST) ─────────
    async def _fetch_cloud_info(self, game_id: str) -> dict[str, Any] | None:
        """Real GOG-cloud save info: has_saves, newest timestamp, file_count.

        Queries GOG's cloud-storage LIST endpoint (the same one gogdl uses
        internally) so the manual cloud-save UI shows the ACTUAL cloud state
        instead of the local backup mirror. Needs a per-game Galaxy-client
        token exchange (GOG scopes cloud storage per clientId). The base
        memoises this 300s and invalidates it after an upload. Returns ``None``
        on any failure so the caller falls back to the mirror. ``total_bytes``
        is 0 (not in the LIST response) — backfilled by the caller.
        """
        return await asyncio.to_thread(self._query_cloud_info_blocking, game_id)

    def _query_cloud_info_blocking(self, game_id: str) -> dict[str, Any] | None:
        try:
            creds = self._read_gog_credentials() or {}
            user_id = creds.get("user_id")
            refresh_token = creds.get("refresh_token")
            if not user_id or not refresh_token:
                return None
            client_id, client_secret = fetch_gog_client_creds(game_id)
            if not client_id or not client_secret:
                return None
            token = exchange_game_token(client_id, client_secret, refresh_token)
            if not token:
                return None
            objects = list_cloud_objects(user_id, client_id, token)
            if objects is None:
                return None

            def resolve_local_mtime(name: str) -> float | None:
                return head_object_local_mtime(user_id, client_id, token, name)

            return summarize_cloud_objects(
                objects, mtime_resolver=resolve_local_mtime,
            )
        except Exception as e:
            logger.debug("[GOGSync] cloud-info query failed for %s: %s", game_id, e)
            return None

    @staticmethod
    def _clear_save_dir(local_dir: str) -> None:
        """Empty a save dir (keep the dir itself) for a clean gogdl download.

        gogdl save-sync has no ``--force-download``; with a non-empty local dir
        it flags a "conflict" and skips cloud-only files. Clearing first makes
        it see an empty dir and pull everything. Caller MUST snapshot_backup
        first — this is destructive to the local copy.
        """
        try:
            for child in Path(local_dir).iterdir():
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
        except Exception as e:
            logger.warning("[GOGSync] failed to clear save dir %s: %s", local_dir, e)

    async def _run_gogdl_save_sync(
        self, auth_file: str, local_dir: str, game_id: str,
        ts: str, namespace: str, direction_flag: str,
        os_platform: str = "windows",
    ) -> bool:
        """Run one ``gogdl save-sync`` for a single location. Updates that
        location's per-namespace timestamp on success. Returns success.

        ``os_platform`` is ``linux`` for native-Linux GOG builds (GOG cloud is
        a Galaxy/Windows feature, so this may simply no-op for them — local
        backup via the service's mirror still protects the saves)."""
        cmd = [
            self.gogdl_bin,
            "--auth-config-path", auth_file,
            "save-sync",
            local_dir,
            game_id,
            "--os", os_platform,
            "--ts", ts,
            "--name", namespace,
            direction_flag,
        ]
        logger.info("[GOGSync] Running save-sync (%r): %s", namespace, " ".join(cmd))
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=clean_cli_env(),
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                logger.error(
                    "[GOGSync] save-sync (%r) failed with code %d: %s",
                    namespace, proc.returncode, stderr.decode(),
                )
                return False
            match = re.search(r"(\d+\.\d+)", stdout.decode().strip())
            if match:
                self._save_timestamp(game_id, match.group(1), namespace)
                logger.info(
                    "[GOGSync] Updated GOG timestamp (%r) to %s",
                    namespace, match.group(1),
                )
            return True
        except Exception:
            logger.exception(
                "[GOGSync] Error during save-sync (%r) for %s", namespace, game_id,
            )
            return False

    async def _do_sync_down(
        self, game_id: str, local_dir: str, force: bool,
    ) -> bool:
        """Pull GOG cloud saves for ALL of a game's locations.

        A game can split saves across several cloud namespaces (BioShock
        Remastered: ``saves`` + ``saves2``); each maps to its own dir, so we
        sync every one — syncing only the primary would strand the rest.
        With ``force`` (explicit "Use Cloud"), pull a full copy (ts=0) even
        when local saves exist — gogdl otherwise treats a recent last-sync
        timestamp as "already synced" and downloads nothing.
        """
        auth_file = self._convert_gog_token()
        if not auth_file:
            logger.error("[GOGSync] Cannot sync down: GOG credentials conversion failed")
            return False

        os_platform = "linux" if self._is_native_linux(game_id) else "windows"
        overall_ok = True
        for tdir, namespace in self._resolve_sync_targets(game_id, local_dir):
            os.makedirs(tdir, exist_ok=True)
            # The base snapshots the primary dir; snapshot any extra location
            # too so a bad/destructive pull stays recoverable.
            if tdir != local_dir:
                safety.snapshot_backup(tdir, self.store_id, game_id)
            ts = self._get_saved_timestamp(game_id, namespace)
            # A CLEAN full pull is needed when the user explicitly chose "Use
            # Cloud" (force), OR when this dir holds no REAL saves yet (only the
            # settings the game writes on first launch, or a fresh prefix):
            #   1. ts=0 so gogdl doesn't assume "already synced" and skip.
            #   2. CLEARING the dir — with a non-empty local, gogdl flags a
            #      "conflict" and SKIPS cloud-only files (e.g. checkpoints), so
            #      the real saves never arrive (Load Game stays empty). An empty
            #      dir makes gogdl pull everything. Recoverable: snapshot above.
            clean_pull = force or not safety.has_save_data(tdir)
            if clean_pull:
                if ts != "0":
                    logger.info(
                        "[GOGSync] Clean pull (ts=0) for %s (%r)", game_id, namespace,
                    )
                    ts = "0"
                self._clear_save_dir(tdir)
            ok = await self._run_gogdl_save_sync(
                auth_file, tdir, game_id, ts, namespace, "--skip-upload",
                os_platform,
            )
            overall_ok = overall_ok and ok
        if overall_ok:
            logger.info("[GOGSync] sync_down completed successfully for %s", game_id)
        return overall_ok

    async def _do_sync_up(self, game_id: str, local_dir: str) -> bool:
        """Push local saves to GOG cloud for every location that HAS saves.

        Upload INTO the namespace the game actually reads — pushing to the
        wrong (default ``__default``) namespace creates a divergent cloud copy
        Galaxy/the game never sees. We sync each of a game's locations, but
        SKIP any dir without real save data: gogdl deletes cloud objects absent
        locally, so uploading an empty/settings-only dir would wipe that
        namespace's cloud copy. (The base already asserted the primary has
        saves; this guards the extra locations.)
        """
        auth_file = self._convert_gog_token()
        if not auth_file:
            logger.error("[GOGSync] Cannot sync up: GOG credentials conversion failed")
            return False

        os_platform = "linux" if self._is_native_linux(game_id) else "windows"
        overall_ok = True
        for tdir, namespace in self._resolve_sync_targets(game_id, local_dir):
            if not safety.has_save_data(tdir):
                logger.info(
                    "[GOGSync] Skipping upload of %r (no real saves in %s)",
                    namespace, tdir,
                )
                continue
            ts = self._get_saved_timestamp(game_id, namespace)
            ok = await self._run_gogdl_save_sync(
                auth_file, tdir, game_id, ts, namespace, "--skip-download",
                os_platform,
            )
            overall_ok = overall_ok and ok
        if overall_ok:
            logger.info("[GOGSync] sync_up completed successfully for %s", game_id)
        return overall_ok
