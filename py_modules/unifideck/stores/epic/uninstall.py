"""Epic uninstall primitives — file removal + legendary bookkeeping.

Extracted from ``install.py`` when that module hit the volumetry cap.
These are the mechanics ``EpicInstaller.uninstall_game`` orchestrates;
it keeps the policy (what order, what to report) and they do the work.

Why any of this exists: ``legendary uninstall`` cannot be trusted to
delete files. Its per-game catalog lookup can fail with HTTP 401 (expired
Epic auth), after which it **skips the deletion but still exits 0**,
printing "please remove <path> manually". So the install directory is
resolved from legendary's *local* ``installed.json`` (no network), the
CLI is run only as best-effort metadata cleanup, and the deletion plus
the registry purge are done here.

All functions are free functions taking what they need explicitly — they
never touched installer state beyond the CLI path and its timeout.

.. note::
   ``_is_safe_to_delete`` duplicates ``unifideck.core.safe_delete``,
   which is stricter: it resolves symlinks, rejects every ancestor of
   ``$HOME``, and requires depth ≥ 4 where this requires ≥ 3.
   Consolidating is worth doing, but it *tightens* deletion — a custom
   install root like ``/games/Foo`` (3 parts) is allowed here and would be
   refused there — so it needs its own change with its own testing rather
   than riding along with an unrelated fix.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shutil
from pathlib import Path

from unifideck.core.binaries import clean_cli_env

from .legendary import legendary_config_dir

logger = logging.getLogger(__name__)

def read_legendary_install_path(game_id: str) -> str | None:
    """Read a game's ``install_path`` from legendary's ``installed.json``.

    A local file read — no catalog/network call — so it works even when
    ``legendary uninstall`` 401s on Epic's catalog API and bails out.
    """
    path = legendary_config_dir() / "installed.json"
    try:
        with path.open() as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    entry = data.get(game_id) if isinstance(data, dict) else None
    if isinstance(entry, dict):
        p = entry.get("install_path")
        return p if isinstance(p, str) and p else None
    return None

def purge_legendary_install_entry(game_id: str) -> None:
    """Drop a game from legendary's ``installed.json``.

    ``legendary uninstall`` leaves the entry behind when its catalog
    lookup fails (HTTP 401), which makes the next library sync re-flag
    the game installed (the Epic library derives install state from
    ``legendary list-installed``). Removing the row keeps it honest.
    """
    path = legendary_config_dir() / "installed.json"
    try:
        with path.open() as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return
    if isinstance(data, dict) and game_id in data:
        data.pop(game_id, None)
        try:
            with path.open("w") as f:
                json.dump(data, f, indent=2)
        except OSError as e:
            logger.warning(
                "[EpicUninstall] could not rewrite installed.json: %s", e,
            )

def _is_safe_to_delete(p: Path) -> bool:
    """Guard against ``rmtree`` on the home dir, ``/``, or shallow roots."""
    resolved = p.resolve()
    home = Path.home().resolve()
    return resolved not in (home, Path("/")) and len(resolved.parts) >= 3

async def best_effort_legendary_uninstall(
    cli_path: str, game_id: str, timeout: float,  # noqa: ASYNC109 — timeout forwarded to asyncio.wait_for on a subprocess, not an asyncio.timeout context
) -> None:
    """Run ``legendary uninstall`` without trusting its outcome.

    Lets legendary tidy its own manifest/metadata when it's online and
    authed. Never fatal — the caller deletes the files itself.
    """
    if not cli_path:
        return
    try:
        proc = await asyncio.create_subprocess_exec(
            cli_path,
            "uninstall",
            game_id,
            "--yes",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=clean_cli_env(),
        )
    except OSError as e:
        logger.warning("[EpicUninstall] could not spawn legendary: %s", e)
        return
    try:
        await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        logger.warning("[EpicUninstall] legendary uninstall timed out")

async def delete_install_dir(install_path: str | None, game_id: str) -> bool:
    """``rmtree`` the install dir. Returns True if it's gone after."""
    if not install_path:
        logger.warning(
            "[EpicUninstall] no tracked install path for %s; "
            "nothing to delete", game_id,
        )
        return True
    p = Path(install_path)
    if not await asyncio.to_thread(p.exists):
        return True
    if not _is_safe_to_delete(p):
        logger.error("[EpicUninstall] refusing to delete unsafe path %s", p)
        return False
    try:
        await asyncio.to_thread(shutil.rmtree, p, ignore_errors=False)
    except OSError as e:
        logger.warning("[EpicUninstall] rmtree %s failed: %s", p, e)
    gone = not await asyncio.to_thread(p.exists)
    logger.info("[EpicUninstall] deleted %s (gone=%s)", p, gone)
    return gone

async def delete_prefix(game_id: str) -> None:
    """Remove the game's Proton prefix (``delete_prefix`` path)."""
    prefix = Path.home() / ".local/share/unifideck/prefixes" / game_id
    if not await asyncio.to_thread(prefix.exists):
        return
    if not _is_safe_to_delete(prefix):
        logger.error(
            "[EpicUninstall] refusing to delete unsafe prefix %s", prefix,
        )
        return
    await asyncio.to_thread(shutil.rmtree, prefix, ignore_errors=True)
    logger.info("[EpicUninstall] deleted prefix %s", prefix)
