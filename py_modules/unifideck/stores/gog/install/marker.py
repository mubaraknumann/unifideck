"""Post-install bookkeeping — locate install dir + write marker.

``_PostInstallMarker`` handles the post-download phase of an install:

* **locate the install** — GOG installer behaviour is inconsistent
  (flat vs nested layout); the locator probes for the
  ``goggame-<id>.info`` marker in the predicted folder and falls back
  to a diff-against-snapshot strategy if not found;
* **reorganise flat installs** — when gogdl produces a flat install at
  the base of the download dir, move everything into a canonical
  ``GOG_<id>`` subdirectory;
* **write the ``.unifideck-id`` marker** — JSON file carrying the
  game_id, language, and metadata extracted from ``goggame-*.info``;
* **regenerate the manifest** — re-run ``gogdl info`` to refresh the
  cached manifest after install/update.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from .primitives import GOGFolderOps

if TYPE_CHECKING:
    from .installer import GOGInstaller
logger = logging.getLogger(__name__)

class _PostInstallMarker:
    """Post install marker."""

    def __init__(self, parent: GOGInstaller) -> None:
        """Initialize the instance."""
        self._parent = parent

    @staticmethod
    def snapshot_dirs(base_path: str) -> set[Any]:
        """Snapshot dirs."""
        try:
            return {entry.name for entry in Path(base_path).iterdir()}
        except OSError:
            return set()

    async def locate_install(
        self,
        game_id: str,
        base_path: str,
        folder_name: str | None,
        existing_dirs: set[Any],
    ) -> str | None:
        """Locate install."""
        flat_info = self._find_flat_goggame(base_path, game_id)
        if flat_info:
            return await self._reorganise_flat_install(
                base_path,
                game_id,
                folder_name,
                existing_dirs,
            )
        if folder_name:
            candidate = str(Path(base_path) / folder_name)
            if await asyncio.to_thread(lambda: Path(candidate).is_dir()):
                logger.info(
                    "[GOGInstaller] found at predicted: %s",
                    candidate,
                )
                return candidate
        try:
            # ``Path.iterdir`` is blocking I/O — wrap in to_thread.
            entries = await asyncio.to_thread(
                lambda: list(Path(base_path).iterdir()),
            )
            current = {entry.name for entry in entries}
        except OSError:
            return None
        new_dirs = current - existing_dirs
        for name in new_dirs:
            item_path = str(Path(base_path) / name)
            # Bind ``item_path`` as a default arg of the lambda
            # rather than capturing it by reference. The closure
            # variant tripped ruff B023 (function-uses-loop-variable):
            # because ``asyncio.to_thread`` schedules the lambda for
            # later execution and the loop reassigns ``item_path``
            # on each iteration, the lambda could observe the wrong
            # path if execution were ever deferred across iterations.
            # In practice the ``await`` here gates one iteration at
            # a time so the bug couldn't fire, but binding the value
            # at lambda-creation time is both safer and the
            # idiomatic fix for B023.
            if not await asyncio.to_thread(
                # Lambda param ``p`` typed as ``str`` so mypy
                # can infer the lambda's return type (was
                # ``Cannot infer type of lambda``).
                lambda p=item_path: Path(p).is_dir(),  # type: ignore[misc]
            ):
                continue
            for search_dir in (
                item_path,
                str(Path(item_path) / "game"),
            ):
                if GOGFolderOps.has_goggame_info(
                    search_dir,
                    game_id,
                ):
                    logger.info(
                        "[GOGInstaller] found via scan: %s",
                        item_path,
                    )
                    return item_path
        return None

    @staticmethod
    def _find_flat_goggame(base_path: str, game_id: str) -> bool:
        """Find flat goggame."""
        with contextlib.suppress(OSError):
            for name in [entry.name for entry in Path(base_path).iterdir()]:
                full = str(Path(base_path) / name)
                if not Path(full).is_file():
                    continue
                if name == f"goggame-{game_id}.info":
                    return True
        return False

    @staticmethod
    async def _reorganise_flat_install(
        base_path: str,
        game_id: str,
        folder_name: str | None,
        existing_dirs: set[Any],
    ) -> str:
        """Reorganise flat install."""
        target = folder_name or f"GOG_{game_id}"
        target_path = str(Path(base_path) / target)

        def _sync_move() -> None:
            """Sync move."""
            Path(target_path).mkdir(parents=True, exist_ok=True)
            try:
                current = {entry.name for entry in Path(base_path).iterdir()}
            except OSError:
                return
            new_files = current - existing_dirs
            for item in new_files:
                if item == target:
                    continue
                src = str(Path(base_path) / item)
                dst = str(Path(target_path) / item)
                try:
                    shutil.move(src, dst)
                except OSError as e:
                    logger.warning(
                        "[GOGInstaller] move %s failed: %s",
                        item,
                        e,
                    )

        await asyncio.to_thread(_sync_move)
        logger.info(
            "[GOGInstaller] reorganised flat install → %s",
            target_path,
        )
        return target_path

    async def write_install_marker(
        self,
        install_path: str,
        game_id: str,
        language: str,
    ) -> bool:
        """Write install marker."""
        marker_path = str(Path(install_path) / ".unifideck-id")
        info_data = self._load_info_data_from_goggame(
            install_path,
            game_id,
        )
        info_data["language"] = language
        ok = await asyncio.to_thread(
            self._write_marker_sync,
            marker_path,
            info_data,
        )
        if ok:
            logger.info(
                "[GOGInstaller] wrote marker at %s (lang=%s)",
                marker_path,
                language,
            )
        return ok

    @staticmethod
    def _load_info_data_from_goggame(install_path: str, game_id: str) -> dict[str, Any]:
        """Load info data from goggame."""
        info_data: dict[str, Any] = {"game_id": game_id}
        for candidate in (
            install_path,
            str(Path(install_path) / "game"),
        ):
            if not Path(candidate).is_dir():
                continue
            loaded = _PostInstallMarker._try_load_info_in_dir(
                candidate,
                game_id,
            )
            if loaded is not None:
                info_data = loaded
                if "name" in info_data:
                    break
        return info_data

    @staticmethod
    def _try_load_info_in_dir(directory: str, game_id: str) -> dict[str, Any] | None:
        """Try load info in dir."""
        try:
            entries = [entry.name for entry in Path(directory).iterdir()]
        except OSError:
            return None
        for name in entries:
            if not name.startswith("goggame-"):
                continue
            if not name.endswith(".info"):
                continue
            try:
                with Path(str(Path(directory) / name)).open(encoding="utf-8",
                ) as f:
                    parsed = json.load(f)
                    parsed["game_id"] = game_id
                    return cast("dict[str, Any] | None", parsed)
            except (OSError, json.JSONDecodeError):
                return None
        return None

    @staticmethod
    def _write_marker_sync(marker_path: str, info_data: dict[str, Any]) -> bool:
        """Write marker sync."""
        try:
            with Path(marker_path).open("w", encoding="utf-8") as f:
                json.dump(info_data, f, indent=2)
            return True
        except OSError:
            logger.exception("[GOGInstaller] marker write failed")
            return False

    async def regenerate_manifest(self, game_id: str, platform: str) -> None:
        """Regenerate manifest."""
        try:
            env, creds_path, _gogdl_cleanup = await self._parent._tokens.acquire_gogdl_creds()
            cmd = [
                self._parent._gogdl_bin,
                "--auth-config-path",
                creds_path,
                "info",
                "--platform",
                platform,
                game_id,
            ]
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
                await proc.wait()
                logger.info(
                    "[GOGInstaller] manifest regenerated (code %d)",
                    proc.returncode,
                )
            finally:
                await _gogdl_cleanup()
        except OSError:
            logger.exception("[GOGInstaller] manifest regen failed")
