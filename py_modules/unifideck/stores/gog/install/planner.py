"""Install mode + verification — pre-flight planning logic.

``GOGInstallPlanner`` answers two pre-install questions:

* **Install mode** — should gogdl run as ``install`` (fresh download
  into a new directory) or ``download`` (update / repair an existing
  install)? The decision rests on whether the target directory
  already contains a valid GOG install.
* **Verification** — after install, does the directory satisfy the
  expected size + file-count + executable presence checks?

The planner also exposes ``_extract_disk_size_from_size_info``, a
module-level helper to parse the human-readable size strings reported
by gogdl into bytes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from unifideck.core.safe_delete import foreign_installs_under, safe_rmtree
from unifideck.stores.gog.config import GOGConfig
from unifideck.stores.gog.tokens import GOGTokenManager

from .primitives import GOGFolderOps

logger = logging.getLogger(__name__)
_CORRUPT_INSTALL_SIZE_THRESHOLD = 100 * 1024 * 1024
_MIN_SIZE_RATIO = 0.8

# Returned by ``determine_install_mode`` when the target directory holds
# another store's install. Not a mode gogdl can run — the caller must fail the
# install rather than proceed, because every other mode either deletes that
# directory or downloads into it.
INSTALL_MODE_BLOCKED = "blocked"


def _may_delete(target_folder: str, *, owner_key: str) -> bool:
    """False when *target_folder* holds an install belonging to someone else.

    Logged at ``error`` because the alternative — what this code did before —
    is silent data loss in a directory the user filled deliberately.
    """
    foreign = foreign_installs_under(target_folder, owner_key=owner_key)
    if not foreign:
        return True
    logger.error(
        "[GOGInstallPlanner] refusing to clean %s: it holds %d install(s) "
        "belonging to %s. Install %s somewhere else, or uninstall those first.",
        target_folder, len(foreign), ", ".join(sorted(foreign)), owner_key,
    )
    return False

def _build_verify_result(
    *,
    actual: int,
    expected: int,
    files: int,
    has_info: bool,
    has_exe: bool,
    size_ratio: float,
    platform: str,
) -> dict[str, Any]:
    """Build the dict returned by ``verify_installation``.

    Centralises the four outcome shapes (incomplete-by-size,
    missing-info, missing-exe, success) so the caller stays a
    flat read of "collect metrics, log, return result". Keeping
    them here also makes it obvious that the dict keys vary by
    outcome — a fact that was easy to miss when the branches
    were inlined.

    Platform carve-out (UD-042): ``goggame-<id>.info`` is a
    Windows/Galaxy-only artifact that is *never* present in GOG's
    Linux-native builds. Treating its absence as an integrity
    failure on ``platform == "linux"`` produced a false
    "incomplete" on every Linux-native install, which needlessly
    triggered a gogdl repair pass (that reliably wedged for ~1h
    behind the finalize watchdog). So the missing-info branch is
    skipped for Linux; the size-ratio and exe checks still guard
    it. For Linux the returned "complete" dict carries
    ``has_info=False`` honestly — downstream only branches on
    ``complete``, so that is safe.
    """
    if expected > 0 and size_ratio < _MIN_SIZE_RATIO:
        return {
            "complete": False,
            "issue": (
                f"Installation may be incomplete: "
                f"only {size_ratio * 100:.0f}% of expected size"
            ),
            "actual_size": actual,
            "expected_size": expected,
            "has_info": has_info,
            "has_exe": has_exe,
        }
    if platform != "linux" and not has_info:
        return {
            "complete": False,
            "issue": "Missing goggame.info file",
            "actual_size": actual,
            "actual_files": files,
            "has_exe": has_exe,
        }
    if not has_exe:
        return {
            "complete": False,
            "issue": "Could not find game executable",
            "actual_size": actual,
            "actual_files": files,
            "has_info": has_info,
        }
    return {
        "complete": True,
        "actual_size": actual,
        "expected_size": expected,
        "actual_files": files,
        "size_ratio": size_ratio,
        "has_info": has_info,
        "has_exe": has_exe,
    }

def _extract_disk_size_from_size_info(size_info: dict[str, Any]) -> int | None:
    """Extract disk size from size info."""
    for lang_key in ("en-US", "en", "*"):
        if lang_key in size_info:
            return int(
                size_info[lang_key].get("disk_size", 0) or 0,
            )
    if size_info:
        first = next(iter(size_info))
        return int(
            size_info[first].get("disk_size", 0) or 0,
        )
    return None

class GOGInstallPlanner:
    """Goginstall planner."""

    def __init__(self, config: GOGConfig, tokens: GOGTokenManager) -> None:
        """Initialize the instance."""
        self._config = config
        self._tokens = tokens

    async def determine_install_mode(
        self,
        game_id: str,
        target_folder: str | None,
    ) -> str:
        """Determine install mode."""
        if not target_folder or not await asyncio.to_thread(lambda: Path(target_folder).exists()):
            logger.info(
                "[GOGInstallPlanner] folder missing → download",
            )
            return "download"
        folder_size = GOGFolderOps.folder_size(target_folder)
        file_count = GOGFolderOps.count_files(target_folder)
        has_info = GOGFolderOps.has_goggame_info(
            target_folder,
            game_id,
        )
        logger.info(
            "[GOGInstallPlanner] folder state: size=%.1fMB, files=%d, has_info=%s",
            folder_size / (1024 * 1024),
            file_count,
            has_info,
        )
        if has_info:
            if folder_size < _CORRUPT_INSTALL_SIZE_THRESHOLD:
                logger.warning(
                    "[GOGInstallPlanner] corrupt install "
                    "(has info but only %.1fMB) → cleanup "
                    "+ download",
                    folder_size / (1024 * 1024),
                )
                if not await self._cleanup_corrupt_install(
                    game_id,
                    target_folder,
                ):
                    return INSTALL_MODE_BLOCKED
                return "download"
            logger.info(
                "[GOGInstallPlanner] valid existing install → repair",
            )
            return "repair"
        if folder_size > _CORRUPT_INSTALL_SIZE_THRESHOLD or file_count > 0:
            logger.warning(
                "[GOGInstallPlanner] orphaned data (no info, "
                "%.1fMB) → cleanup + download",
                folder_size / (1024 * 1024),
            )
            if not await self._cleanup_orphaned_install(
                game_id,
                target_folder,
            ):
                return INSTALL_MODE_BLOCKED
        return "download"

    async def verify_installation(
        self,
        game_id: str,
        install_path: str,
        platform: str,
        exe_finder: Callable[[str], str | None],
    ) -> dict[str, Any]:
        """Verify installation.

        Refactor history (2026-05-14): the dict-construction
        for the four outcome shapes (incomplete-by-size, missing-
        info, missing-exe, success) was inlined, pushing the
        function over the 80-line cap. Pulled into the module-
        level ``_build_verify_result`` helper.
        """
        try:
            expected = await self.get_expected_disk_size(
                game_id,
                platform,
            )
            actual = GOGFolderOps.folder_size(install_path)
            files = GOGFolderOps.count_files(install_path)
            # ``has_goggame_info(path, game_id="")`` falls back to
            # "is *any* ``goggame-*.info`` file present?" when the
            # ``game_id`` arg is empty. For a correctness check
            # that verifies THIS specific game's install, we MUST
            # supply ``game_id`` — otherwise an orphaned info file
            # from a previous install in the same folder yields a
            # false positive and the planner concludes the install
            # is complete when it isn't.
            has_info = GOGFolderOps.has_goggame_info(
                install_path,
                game_id,
            )
            has_exe = exe_finder(install_path) is not None
            size_ratio = (actual / expected) if expected > 0 else 1.0
            logger.info(
                "[GOGInstallPlanner] verify: size=%.1fMB "
                "(%.0f%% of expected), files=%d, "
                "has_info=%s, has_exe=%s",
                actual / (1024 * 1024),
                size_ratio * 100,
                files,
                has_info,
                has_exe,
            )
            return _build_verify_result(
                actual=actual,
                expected=expected,
                files=files,
                has_info=has_info,
                has_exe=has_exe,
                size_ratio=size_ratio,
                platform=platform,
            )
        except Exception as e:
            logger.exception("[GOGInstallPlanner] verify error")
            return {
                "complete": False,
                "issue": f"Verification failed: {e}",
            }

    async def get_expected_disk_size(self, game_id: str, platform: str) -> int:
        """Get expected disk size."""
        gogdl_bin = self._resolve_gogdl_bin()
        if not gogdl_bin:
            return 0
        stdout = await self._spawn_gogdl_info(
            gogdl_bin,
            game_id,
            platform,
        )
        if stdout is None:
            return 0
        return self._parse_size_from_gogdl_info(stdout)

    async def _spawn_gogdl_info(
        self,
        gogdl_bin: str,
        game_id: str,
        platform: str,
    ) -> bytes | None:
        """Spawn GOGDL info."""
        try:
            env, creds_path, _gogdl_cleanup = await self._tokens.acquire_gogdl_creds()
            cmd = [
                gogdl_bin,
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
                stdout, _stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=30,
                )
                return stdout
            finally:
                await _gogdl_cleanup()
        except (TimeoutError, OSError) as e:
            logger.warning(
                "[GOGInstallPlanner] gogdl info failed: %s",
                e,
            )
            return None

    @staticmethod
    def _parse_size_from_gogdl_info(stdout: bytes) -> int:
        """Parse size from GOGDL info."""
        for raw_line in stdout.decode(
            errors="replace",
        ).splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            size_info = data.get("size")
            if not isinstance(size_info, dict):
                continue
            extracted = _extract_disk_size_from_size_info(
                size_info,
            )
            if extracted is not None:
                return extracted
        return 0

    async def _cleanup_corrupt_install(
        self, game_id: str, target_folder: str,
    ) -> bool:
        """Remove a too-small install at *target_folder*. False if kept.

        Same ownership guard as :meth:`_cleanup_orphaned_install` — see there
        for why "this game's install looks wrong" is not a licence to delete
        the whole directory. The support cache below is keyed by game id and
        belongs to GOG unconditionally, so it is cleared either way.
        """
        may_delete = _may_delete(target_folder, owner_key=f"gog:{game_id}")

        def _sync() -> None:
            """Sync."""
            if may_delete and not safe_rmtree(target_folder):
                logger.warning(
                    "[GOGInstallPlanner] corrupt cleanup refused or failed: %s",
                    target_folder,
                )
            elif may_delete:
                logger.info(
                    "[GOGInstallPlanner] removed %s",
                    target_folder,
                )
            support_dir = (
                Path(self._config.gogdl_config_dir).expanduser()
                / "gog-support"
                / game_id
            )
            if support_dir.is_dir():
                try:
                    shutil.rmtree(support_dir)
                    logger.info(
                        "[GOGInstallPlanner] cleared support cache: %s",
                        support_dir,
                    )
                except OSError as e:
                    logger.warning(
                        "[GOGInstallPlanner] could not clear support dir: %s",
                        e,
                    )

        await asyncio.to_thread(_sync)
        return may_delete

    async def _cleanup_orphaned_install(
        self,
        game_id: str,
        target_folder: str,
    ) -> bool:
        """Remove orphaned data at *target_folder*. False if it must be kept.

        "Orphaned" only means *this game* left no ``goggame-*.info`` there. It
        does not mean the directory is unowned: two stores can pick the same
        folder name under the same install root, and this cleanup used to
        ``rmtree`` the lot. On a real device a GameVault archive extracted into
        ``<root>/Bastion`` was destroyed four times over by exactly this path,
        because GOG's folder for the same game is also ``Bastion``.
        """
        if not _may_delete(target_folder, owner_key=f"gog:{game_id}"):
            return False

        def _sync() -> None:
            """Sync."""
            if not safe_rmtree(target_folder):
                logger.warning(
                    "[GOGInstallPlanner] orphan cleanup refused or failed: %s",
                    target_folder,
                )
            else:
                logger.info(
                    "[GOGInstallPlanner] removed orphan %s",
                    target_folder,
                )
            for manifest_path in self._manifest_locations(
                game_id,
            ):
                mp = Path(manifest_path)
                if mp.is_file():
                    try:
                        mp.unlink()
                        logger.info(
                            "[GOGInstallPlanner] cleaned stale manifest: %s",
                            manifest_path,
                        )
                    except OSError as e:
                        logger.warning(
                            "[GOGInstallPlanner] could not clean manifest: %s",
                            e,
                        )

        await asyncio.to_thread(_sync)
        return True

    def _manifest_locations(self, game_id: str) -> list[str]:
        """Manifest locations."""
        base = Path(
            self._config.gogdl_config_dir,
        ).expanduser()
        parent = base.parent
        return [
            str(base / "heroic_gogdl" / "manifests" / game_id),
            str(
                parent / "heroic_gogdl" / "manifests" / game_id,
            ),
            str(base / "manifests" / game_id),
            str(parent / "gogdl" / "manifests" / game_id),
        ]

    def _resolve_gogdl_bin(self) -> str | None:
        """Resolve GOGDL bin."""
        return getattr(self, "_gogdl_bin_override", None)

    def set_gogdl_bin(self, path: str) -> None:
        """Set GOGDL bin."""
        self._gogdl_bin_override = path
