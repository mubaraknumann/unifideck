"""
Wine prefix helpers — symlink fixups, marker writing, basic file ops.

Helper class with a grab-bag of operations the prefix builders rely on:

* ``fix_pfx_symlink`` — repairs a ``<prefix>/pfx`` symlink pointing at the
  wrong target. **Not** the same job as
  ``shared/prefix_clone.ensure_pfx_symlink``, which creates a *missing*
  one; their guards are opposites and neither substitutes for the other;
* ``write_bootstrap_marker`` — stamps a prefix as "Unifideck-managed",
  through ``shared/prefix_clone.write_marker``;
* misc. ``Path``-based wrappers around create/delete/check operations.

Cloning itself is **not** here any more: every clone goes through
``shared/prefix_clone.rsync_clone``, which is where the ``--checksum``
rule for repairing an existing prefix is stated and enforced.

Kept as a separate module so the builders can stay focused on the
high-level construction logic.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

from unifideck.stores.shared.prefix_clone import (
    PrefixMarker,
    rsync_clone,
    write_marker,
)

if TYPE_CHECKING:
    from .manager import UbisoftPrefixManager
logger = logging.getLogger(__name__)
_SILENT_INSTALL_FLAG = "/S"

class _PrefixHelpers:
    """Prefix helpers."""

    def __init__(self, parent: UbisoftPrefixManager) -> None:
        """Initialize the instance."""
        self._parent = parent

    async def clone_prefix_from_template(
        self,
        space_id: str,
        prefix_path: str,
    ) -> bool:
        """Clone prefix from template."""
        logger.info(
            "[UbisoftPrefixManager] cloning template for %s",
            space_id,
        )
        try:
            await asyncio.to_thread(lambda: Path(prefix_path).mkdir(parents=True, exist_ok=True))
            ok = await rsync_clone(
                Path(self._parent._config.template_dir_expanded),
                Path(prefix_path),
                exclude_games=False,
            )
            if not ok:
                logger.error(
                    "[UbisoftPrefixManager] rsync clone failed for %s",
                    space_id,
                )
                return False
            self.write_bootstrap_marker(
                prefix_path,
                "cloned_from_template",
                space_id,
            )
            self.try_inject_auth_state([prefix_path])
            logger.info(
                "[UbisoftPrefixManager] prefix cloned for %s",
                space_id,
            )
            return True
        except Exception:
            logger.exception("[UbisoftPrefixManager] clone failed")
            return False

    async def create_prefix_from_fresh_install(
        self,
        space_id: str,
        prefix_path: str,
    ) -> bool:
        """Create prefix from fresh install."""
        logger.info(
            "[UbisoftPrefixManager] fresh install for %s",
            space_id,
        )
        installer_path = await self._parent._installer_cache.ensure_cached()
        if not installer_path:
            return False
        try:
            await asyncio.to_thread(lambda: Path(prefix_path).mkdir(parents=True, exist_ok=True))
            success = await self.run_silent_installer(
                prefix_dir=prefix_path,
                installer_path=installer_path,
                gameid=f"umu-ubisoft-{space_id}",
                store_game_id=f"ubisoft:{space_id}",
            )
            if not success:
                return False
            if not self._parent._paths.find_upc_exe(prefix_path):
                logger.error(
                    "[UbisoftPrefixManager] upc.exe not "
                    "found after fresh install for %s",
                    space_id,
                )
                return False
            self.write_bootstrap_marker(
                prefix_path,
                "fresh_install",
                space_id,
            )
            self.try_inject_auth_state([prefix_path])
            return True
        except Exception:
            logger.exception("[UbisoftPrefixManager] fresh install failed for %s", space_id)
            return False

    async def create_template_from_game_prefix(
        self,
        game_prefix: str,
    ) -> None:
        """Create template from game prefix."""
        template_dir = self._parent._config.template_dir_expanded
        logger.info(
            "[UbisoftPrefixManager] creating template from first game prefix",
        )
        try:
            await asyncio.to_thread(lambda: Path(template_dir).mkdir(parents=True, exist_ok=True))
            ok = await rsync_clone(
                Path(game_prefix),
                Path(template_dir),
                exclude_games=False,
            )
            if not ok:
                return
            self.write_bootstrap_marker(
                template_dir,
                "template",
                None,
            )
            self.try_inject_auth_state([template_dir])
        except Exception as e:
            logger.warning(
                "[UbisoftPrefixManager] template creation from game prefix failed: %s",
                e,
            )

    async def create_template_from_auth_prefix(
        self,
        auth_dir: str,
    ) -> None:
        """Create template from auth prefix (canonical identity source).

        Under the shared-identity invariant the ``.template`` prefix is
        always an rsync clone of ``.upc-auth`` — never a standalone fresh
        install.  This guarantees all prefixes in the Ubisoft family share
        the same ``MachineGuid`` + DPAPI registry state, so the credential
        vault decrypts everywhere.
        """
        template_dir = self._parent._config.template_dir_expanded
        auth_real, template_real = await asyncio.to_thread(
            lambda: (os.path.realpath(auth_dir), os.path.realpath(template_dir)),
        )
        if auth_real == template_real:
            return
        logger.info(
            "[UbisoftPrefixManager] deriving template from auth prefix",
        )
        try:
            await asyncio.to_thread(lambda: Path(template_dir).mkdir(parents=True, exist_ok=True))
            # ``checksum``: one caller
            # (``regenerate_template_from_auth_if_diverged``) rmtrees the
            # template first, but the sign-in path reaches here with the old
            # template still in place — so this is a repair, and rsync's
            # size-plus-mtime quick check would skip the identity files it
            # exists to replace. See ``prefix_clone.rsync_clone``.
            ok = await rsync_clone(
                Path(auth_dir),
                Path(template_dir),
                exclude_games=True,
                checksum=True,
            )
            if not ok:
                logger.error(
                    "[UbisoftPrefixManager] rsync clone (auth→template) failed",
                )
                return
            self.write_bootstrap_marker(
                template_dir,
                "template_from_auth",
                None,
            )
            self.try_inject_auth_state([template_dir])
            logger.info(
                "[UbisoftPrefixManager] template derived from auth prefix "
                "— shared identity established",
            )
        except Exception as e:
            logger.warning(
                "[UbisoftPrefixManager] template derivation from auth failed: %s",
                e,
            )

    async def run_silent_installer(
        self,
        *,
        prefix_dir: str,
        installer_path: str,
        gameid: str,
        store_game_id: str | None = None,
    ) -> bool:
        """Run silent installer."""
        umu_run = self._parent._binaries.find_umu_run()
        if not umu_run:
            logger.error(
                "[UbisoftPrefixManager] umu-run not found",
            )
            return False
        env = self._parent._binaries.build_umu_env(
            wineprefix=prefix_dir,
            gameid=gameid,
            store_game_id=store_game_id,
        )
        python_bin = self._parent._binaries.find_python()
        logger.info(
            "[UbisoftPrefixManager] installer run: PROTONPATH=%s GAMEID=%s",
            env.get("PROTONPATH"),
            env.get("GAMEID"),
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                python_bin,
                umu_run,
                installer_path,
                _SILENT_INSTALL_FLAG,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError:
            logger.exception("[UbisoftPrefixManager] subprocess spawn failed")
            return False
        return await self._await_installer_completion(proc)

    @staticmethod
    async def _await_installer_completion(
        proc: asyncio.subprocess.Process,
    ) -> bool:
        """Await installer completion."""
        try:
            _stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=15 * 60,
            )
        except TimeoutError:
            logger.exception(
                "[UbisoftPrefixManager] installer timed out after 15 min — killing",
            )
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
            return False
        if proc.returncode != 0:
            stderr_text = (
                stderr.decode(
                    errors="replace",
                )[:500]
                if stderr
                else ""
            )
            logger.error(
                "[UbisoftPrefixManager] installer exited %d: %s",
                proc.returncode,
                stderr_text,
            )
            return False
        return True

    @staticmethod
    def fix_pfx_symlink(prefix_dir: str) -> None:
        """Repair a ``<prefix>/pfx`` symlink that points somewhere wrong.

        Not a copy of ``prefix_clone.ensure_pfx_symlink``, and the two are
        not interchangeable — their guards are opposites. This one returns
        early when the link is **absent** and retargets a **wrong existing**
        link; that one returns early when a link is **present** and creates
        a **missing** one. Neither can do the other's job.
        """
        pfx_link = str(Path(prefix_dir) / "pfx")
        if not Path(pfx_link).is_symlink():
            return
        try:
            current_target = Path(pfx_link).readlink()
            # ``readlink()`` returns ``Path`` but the comparison
            # set mixes ``Path`` (``prefix_dir``) and ``str``
            # (``"."``). Coerce both sides to ``str`` so mypy
            # sees overlapping types — and the semantic stays
            # identical (Path equality goes through ``__fspath__``
            # which compares the string form anyway).
            if str(current_target) in (str(prefix_dir), "."):
                return
            Path(pfx_link).unlink()
            Path(pfx_link).symlink_to(prefix_dir)
            logger.info(
                "[UbisoftPrefixManager] fixed pfx symlink: %s → %s",
                current_target,
                prefix_dir,
            )
        except OSError as e:
            logger.warning(
                "[UbisoftPrefixManager] could not fix pfx symlink: %s",
                e,
            )

    def write_bootstrap_marker(
        self,
        prefix_dir: str,
        source: str,
        space_id: str | None,
    ) -> None:
        """Stamp a prefix as ours, in the shared ``PrefixMarker`` shape.

        **The filename does not change** and must not: prefix ownership is
        proved by ``compatdata_scan.MARKER_PREFIXES``, which matches
        ``unifideck_ubisoft_bootstrap.marker`` through its ``unifideck_``
        arm, and every prefix already on a user's disk carries that name.

        Only the *content* moved, from plaintext lines to the JSON every
        other wrapper store writes. Safe because all five readers of this
        marker test ``is_file()`` and none parses it — so a prefix written
        by an older build still reads correctly. The converse does not
        hold: ``prefix_clone.is_owned_by`` reports False for a legacy
        plaintext marker, so no Ubisoft path may be moved onto it until
        those markers are upgraded in place.
        """
        write_marker(
            Path(prefix_dir),
            self._parent._config.bootstrap_marker,
            PrefixMarker(
                store="ubisoft",
                created_at=time.time(),
                source=source,
                game_id=space_id,
            ),
        )

    def try_inject_auth_state(
        self,
        prefix_paths: list[str],
    ) -> None:
        """Try inject auth state."""
        if not prefix_paths:
            return
        try:
            self._parent._inject_auth_state(prefix_paths)
        except Exception as e:
            logger.warning(
                "[UbisoftPrefixManager] auth state injection failed: %s",
                e,
            )
