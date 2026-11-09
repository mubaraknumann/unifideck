from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from datetime import UTC
from pathlib import Path
from typing import Any

from unifideck.core.binaries import clean_cli_env
from unifideck.launcher.proton.infrastructure.prefix_layout import resolve_registry_prefix
from unifideck.services.cloud_save.path_resolver import WinePrefixResolver
from unifideck.services.cloud_save.strategy_base import CloudSaveStrategy, CloudUnreachableError

logger = logging.getLogger(__name__)

class EpicCloudSaveStrategy(CloudSaveStrategy):
    """Cloud save strategy for Epic Games Store using legendary."""

    store_id = "epic"

    def __init__(
        self, local_save_root: str, config: Any = None, cache: Any = None,
    ) -> None:
        super().__init__(local_save_root, config, cache)
        # Epic account id, looked up lazily and memoised — the ``{EpicID}``
        # cloud-save token is the ACCOUNT id, not the game id.
        self._account_id: str | None = None
        self._account_id_looked_up = False

        # Resolve path to the bundled legendary binary. Use the canonical
        # plugin-root resolver (same one the launch path uses) — bin/ is a
        # SIBLING of py_modules, so naive dirname-walking from this file
        # lands on py_modules and misses bin/, falling back to a bare
        # "legendary" that isn't on the launcher's PATH.
        from unifideck.core.paths import resolve_plugin_dir
        plugin_dir = str(resolve_plugin_dir(start=Path(__file__)))
        self.legendary_bin = os.path.join(plugin_dir, "bin", "legendary")
        if not os.path.exists(self.legendary_bin):
            self.legendary_bin = "legendary"

    def _get_account_id(self) -> str | None:
        """Return the logged-in Epic account id (legendary ``user.json``).

        Epic's ``{EpicID}`` cloud-save token is this account id — NOT the
        game's app id (see legendary ``core.py:834``). Memoised; returns
        None when no authenticated legendary config is found.
        """
        if self._account_id_looked_up:
            return self._account_id
        account_id: str | None = None
        try:
            from unifideck.launcher.proton.compat.epic import (
                resolve_legendary_config_path,
            )
            config_path = resolve_legendary_config_path()
            if config_path:
                user_json = Path(config_path) / "user.json"
                if user_json.is_file():
                    data = json.loads(user_json.read_text(encoding="utf-8"))
                    account_id = data.get("account_id") or None
        except Exception as e:
            logger.debug("[EpicSync] Could not read Epic account id: %s", e)
        self._account_id = account_id
        self._account_id_looked_up = True
        return account_id

    def _legendary_save_path(self, game_id: str, prefix_root: Path) -> str | None:
        """Validating fallback: ask legendary itself for the save path.

        Used only when our resolver's path doesn't exist on disk — catches
        Epic tokens we don't yet handle. legendary derives the Wine prefix
        from ``STEAM_COMPAT_DATA_PATH/pfx`` (our ``pfx`` is a self-referential
        symlink, so the root works). ``get_save_path`` is side-effect-free.
        """
        prev = os.environ.get("STEAM_COMPAT_DATA_PATH")
        try:
            # legendary is forced to Any by the mypy override (it's absent in
            # CI), so these calls need no no-untyped-call ignore; str() keeps
            # the return concretely str | None (not Any).
            from legendary.core import LegendaryCore
            os.environ["STEAM_COMPAT_DATA_PATH"] = str(prefix_root)
            core = LegendaryCore()
            resolved = core.get_save_path(game_id)
            return str(resolved) if resolved and os.path.isdir(resolved) else None
        except Exception as e:
            logger.debug("[EpicSync] legendary get_save_path fallback failed for %s: %s", game_id, e)
            return None
        finally:
            if prev is None:
                os.environ.pop("STEAM_COMPAT_DATA_PATH", None)
            else:
                os.environ["STEAM_COMPAT_DATA_PATH"] = prev

    def _resolve_save_dir_from_enriched(
        self, game_id: str, install_path: str,
    ) -> str | None:
        """Resolve from enriched save-location metadata (unifiDB/PCGamingWiki).

        Thin Epic wrapper over the shared base resolver — supplies the
        registry-prefix path and the legendary install dir (for ``<base>``).
        """
        prefix_path = resolve_registry_prefix(self._prefix_root(game_id))
        return self._resolve_enriched(
            game_id, prefix_path=str(prefix_path), install_path=install_path or "",
        )

    def _resolve_store_save_dir(self, game_id: str) -> str | None:
        """Resolve the Epic save dir via legendary info (override+memo in base)."""
        try:
            # Query legendary for game metadata
            cmd = [self.legendary_bin, "info", game_id, "--json"]
            res = subprocess.run(
                cmd, capture_output=True, text=True,
                env=clean_cli_env(), check=True,
            )
            data = json.loads(res.stdout)

            # legendary nests game metadata under "game" and install info
            # under "install". ``cloud_save_folder`` is a path template like
            # "{AppData}/Publisher/Game/Save/". Reading these at the TOP
            # level (the old bug) always yielded None → "no save dir" → the
            # game's saves were never synced (Continue greyed out).
            game_meta = data.get("game") or {}
            install_meta = data.get("install") or {}

            install_path = install_meta.get("install_path") or ""

            cloud_save_folder = game_meta.get("cloud_save_folder")
            if not cloud_save_folder:
                logger.info("[EpicSync] No cloud_save_folder metadata found for game %s", game_id)
                # Legendary has no template — try enriched save-location
                # metadata (unifiDB/PCGamingWiki) before giving up. The base
                # memoizes a non-None result.
                return self._resolve_save_dir_from_enriched(game_id, install_path)

            # Resolve prefix location
            prefix_root = Path(self.local_save_root).parent / "prefixes" / game_id
            prefix_path = resolve_registry_prefix(prefix_root)

            # ``{EpicID}`` in the template is the Epic ACCOUNT id (legendary's
            # rule, core.py:834). Fall back to the game id only when no
            # authenticated config is found, so non-``{EpicID}`` games (which
            # don't use the token) never regress.
            account_id = self._get_account_id() or game_id
            resolved = WinePrefixResolver.resolve_path(
                cloud_save_folder=cloud_save_folder,
                prefix_path=str(prefix_path),
                install_path=install_path,
                account_id=account_id,
            )
            resolved, source = self._validate_or_fallback(
                game_id, resolved, prefix_root, install_path,
            )

            logger.info("[EpicSync] Resolved save path for %s (%s): %s", game_id, source, resolved)
            return resolved
        except Exception:
            logger.exception("[EpicSync] Failed to resolve local save dir for %s", game_id)
            return None

    def _validate_or_fallback(
        self, game_id: str, resolved: str, prefix_root: Path, install_path: str,
    ) -> tuple[str, str]:
        """Validate the resolver path on disk; fall back to legendary/enriched.

        Returns ``(path, source)``. If our resolved path exists, it's used as
        is ("resolver"). Otherwise prefer legendary's own existing in-prefix
        dir ("legendary"); if legendary also has nothing, try enriched
        metadata ("enriched") before accepting a path the game never reads.
        """
        if os.path.isdir(resolved):
            return resolved, "resolver"
        leg = self._legendary_save_path(game_id, prefix_root)
        if leg and leg != resolved:
            logger.warning(
                "[EpicSync] resolver path %s missing; using legendary's %s",
                resolved, leg,
            )
            return leg, "legendary"
        if not leg:
            # Both our resolver and legendary came up empty/missing — try
            # enriched save-location metadata before accepting a path the
            # game never reads from.
            enriched = self._resolve_save_dir_from_enriched(game_id, install_path)
            if enriched:
                return enriched, "enriched"
        return resolved, "resolver"

    async def _fetch_cloud_info(self, game_id: str) -> dict[str, Any] | None:
        """Real Epic-cloud save info via ``legendary list-saves``.

        Returns ``{"has_saves": bool, "timestamp": <epoch of latest cloud
        save>}`` so the UI shows the ACTUAL cloud state, not the local backup
        mirror (which can be stale). ``None`` on failure → caller falls back.
        The base memoizes this 300s and invalidates it after an upload.
        """
        return await self._query_cloud_info(game_id)

    async def _query_cloud_info(self, game_id: str) -> dict[str, Any] | None:
        """Run ``legendary list-saves <id>`` and parse the latest manifest ts."""
        import re
        from datetime import datetime
        try:
            proc = await asyncio.create_subprocess_exec(
                self.legendary_bin, "list-saves", game_id,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=clean_cli_env(),
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
        except TimeoutError as e:
            raise CloudUnreachableError("list-saves timed out") from e
        except Exception as e:
            logger.debug("[EpicSync] list-saves failed for %s: %s", game_id, e)
            return None
        if proc.returncode != 0:
            # Offline or signed out. No saves exits 0, so this is not an empty cloud.
            raise CloudUnreachableError(f"list-saves exited {proc.returncode}")
        text = stdout.decode("utf-8", "replace")
        stamps: list[float] = []
        # Manifests are named ``YYYY.MM.DD-HH.MM.SS.manifest`` (UTC).
        for m in re.finditer(
            r"(\d{4})\.(\d{2})\.(\d{2})-(\d{2})\.(\d{2})\.(\d{2})\.manifest", text,
        ):
            try:
                # The regex captures exactly 6 fields (Y, M, D, h, m, s);
                # build a fixed 6-tuple so they fill datetime's positional
                # date/time args and ``tzinfo=UTC`` stays a true keyword
                # (splatting a generator made mypy/datetime read tzinfo twice).
                year, month, day, hour, minute, second = (
                    int(x) for x in m.groups()
                )
                dt = datetime(
                    year, month, day, hour, minute, second, tzinfo=UTC,
                )
                stamps.append(dt.timestamp())
            except ValueError:
                continue
        return {"has_saves": bool(stamps), "timestamp": max(stamps) if stamps else 0.0}

    async def _do_sync_down(
        self, game_id: str, local_dir: str, force: bool,
    ) -> bool:
        """Pull Epic cloud saves into ``local_dir`` (base did save-dir+snapshot).

        With ``force`` (explicit "Use Cloud"), add ``--force-download`` so
        legendary pulls even when the local save is newer/same-age — without
        it, ``--skip-upload`` makes legendary skip those cases and pull
        nothing (cli.py:549-560), which is the "can't pull after playing" bug.
        """
        cmd = [
            self.legendary_bin, "sync-saves", game_id,
            "--save-path", local_dir,
            "-y", "--disable-filters",
            "--skip-upload",
        ]
        if force:
            cmd.append("--force-download")
        logger.info("[EpicSync] Running sync_down: %s", " ".join(cmd))

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=clean_cli_env(),
            )
            _stdout, stderr = await proc.communicate()
            stderr_text = stderr.decode(errors="replace")
            if proc.returncode != 0:
                logger.error(
                    "[EpicSync] sync_down failed with code %d: %s",
                    proc.returncode, stderr_text,
                )
                return False
            # legendary exits 0 even when it pulled NOTHING (local newer / same
            # age). Surface that explicitly so a "successful" sync that changed
            # nothing is diagnosable — and hint that a forced pull can override.
            self._log_sync_down_outcome(game_id, stderr_text, force)
            return True
        except Exception:
            logger.exception("[EpicSync] Error during sync_down for %s", game_id)
            return False

    @staticmethod
    def _log_sync_down_outcome(game_id: str, output: str, force: bool) -> None:
        """Log what legendary actually did, parsed from its stable log lines."""
        if "Downloading remote savegame" in output:
            logger.info("[EpicSync] sync_down pulled cloud saves for %s", game_id)
        elif "No cloud or local savegame found" in output:
            logger.info("[EpicSync] sync_down: no cloud or local save for %s", game_id)
        elif "is up to date" in output or "is newer" in output:
            # SAME_AGE / LOCAL_NEWER — legendary skipped the download.
            hint = "" if force else " (use the 'Use Cloud' conflict choice to force a pull)"
            logger.warning(
                "[EpicSync] sync_down pulled NOTHING for %s — local save is "
                "up-to-date or newer than cloud%s", game_id, hint,
            )
        else:
            logger.info("[EpicSync] sync_down completed for %s", game_id)

    async def _do_sync_up(self, game_id: str, local_dir: str) -> bool:
        """Push local saves to Epic cloud (base did save-dir+guard+assert)."""
        cmd = [
            self.legendary_bin, "sync-saves", game_id,
            "--save-path", local_dir,
            "-y", "--disable-filters",
            "--skip-download"
        ]
        logger.info("[EpicSync] Running sync_up: %s", " ".join(cmd))

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=clean_cli_env(),
            )
            _stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                logger.error(
                    "[EpicSync] sync_up failed with code %d: %s",
                    proc.returncode, stderr.decode()
                )
                return False
            logger.info("[EpicSync] sync_up completed successfully for %s", game_id)
            return True
        except Exception:
            logger.exception("[EpicSync] Error during sync_up for %s", game_id)
            return False
