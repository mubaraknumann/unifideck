"""stores/microsoft/xodus/updater.py — update one installed Xbox PC game.

An update is a delta ``xodus-cli streaming`` run into the game's own folder.
The CLI keeps the old package when its final write fails, and it never
deletes ``.xodus-streaming.msixvc`` itself.

The update refuses while the game runs (the launcher holds
``locks/game-<PID>.lock``) or while another download writes the folder
(``.nexus-streaming.lock``). It holds the game lock for its whole run, so
the game cannot start while its files change.

The record goes to ``updating`` before the download and back to
``installed`` only after the same checks an install passes. A failed or
cancelled update leaves it ``updating``: the launcher refuses to start a
half-updated game, and the update check keeps offering the update.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from unifideck.core.types import InstallResult
from unifideck.launcher.xbox_pc.argv import validate_market
from unifideck.launcher.xbox_pc.component_locks import game_lock

from . import install_steps as steps
from .errors import InstallError
from .guards import layout_ok, streaming_active
from .ids import record_for_game
from .progress import ProgressCallback

logger = logging.getLogger(__name__)


def located_record(game_id: str) -> tuple[dict[str, Any], Path, Path]:
    """``(record, installRoot, gameDir)`` of an installed game. Blocking.

    Raises ``install_not_located`` when there is no usable record or the
    folder is gone (an unmounted card included).
    """
    record = record_for_game(game_id)
    if record is None or record.get("state") == "partial":
        raise InstallError("install_not_located")
    root = Path(str(record.get("installRoot") or ""))
    game_dir = Path(str(record.get("gameDir") or ""))
    if not layout_ok(root, str(record["productId"])) or not game_dir.is_dir():
        raise InstallError("install_not_located")
    return record, root, game_dir


class XboxPcUpdater:
    """Updates installed Xbox PC games in place."""

    def __init__(self, xbox_pc: steps.XboxPcLike, cli: steps.XodusCliLike) -> None:
        """Keep the facade and the CLI adapter."""
        self._xbox_pc = xbox_pc
        self._cli = cli

    async def update(self, game_id: str, progress_cb: ProgressCallback = None) -> InstallResult:
        """Update ``game_id``. Never raises except on cancel."""
        try:
            return await self._update(game_id, progress_cb)
        except InstallError as fail:
            logger.warning("[xbox_install] update of %s failed: %s", game_id, fail)
            return steps.failed_result(game_id, fail)
        except (OSError, ValueError) as e:
            # Our own failure. Its message can hold a home path, so only
            # the type reaches the user, as a coded error.
            logger.exception("[xbox_install] update of %s crashed", game_id)
            return steps.failed_result(game_id, InstallError("xodus_failed", type(e).__name__))

    async def _update(self, game_id: str, progress_cb: ProgressCallback) -> InstallResult:
        """Check, lock, then run the update."""
        blocked = await steps.ready_code(self._xbox_pc)
        if blocked:
            raise InstallError(blocked)
        record, root, game_dir = await asyncio.to_thread(located_record, game_id)
        await steps.check_backend(self._cli)
        with game_lock(str(record["productId"])) as held:
            if not held:
                raise InstallError("xbox_game_running")
            if await asyncio.to_thread(streaming_active, game_dir):
                raise InstallError("xbox_install_in_progress")
            return await self._run(record, root, game_dir, progress_cb)

    async def _run(
        self, record: dict[str, Any], root: Path, game_dir: Path,
        progress_cb: ProgressCallback,
    ) -> InstallResult:
        """Mark ``updating``, stream the delta, verify, then commit."""
        pid = str(record["productId"])
        try:
            market = validate_market(str(record.get("market") or ""))
        except ValueError:
            raise InstallError("xbox_market_unknown") from None
        cid = await steps.choose_content(
            self._cli, pid, market, str(record.get("contentId") or "") or None,
        )
        record.update({"state": "updating", "contentId": cid})
        await steps.save_record(record)
        outcome = await steps.stream_into(self._cli, pid, game_dir, market, cid, progress_cb)
        facts = await steps.verify(outcome, game_dir)
        return await steps.commit(record, facts, root, stamp="updatedAt")
