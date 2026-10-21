"""stores/microsoft/xodus/updates.py — which installed Xbox PC games have an update.

``MicrosoftStore.check_for_updates`` delegates here; the update sweep calls
it with a 300 s budget per store. One temporary ``xodus-service`` session
answers every installed game, one query at a time.

Nothing is started, and the answer is ``[]``, when the feature is off, the
components are missing, the account is signed out, or no game is installed.
While a download holds the credential lock, or a game is running, the last
answer is returned instead: both use the same tokens, and refreshing them
from two processes at once can lose the sign-in.

A game whose record is still ``updating`` (a failed or cancelled update) is
always reported, so the Update button stays until the update finishes.
A product the service could not answer for is logged with its stage and
left out. That costs the user only a missing Update badge until the next
sweep.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from unifideck.launcher.xbox_pc.component_locks import components_in_use
from unifideck.launcher.xbox_pc.install_records import list_install_records
from unifideck.launcher.xbox_pc.layout import PID_RE

from . import install_steps as steps
from .errors import cli_error_text
from .game_facts import VERSION_RE
from .ids import store_game_id
from .update_service import ServiceError, query, service_session

logger = logging.getLogger(__name__)

#: The whole check must end inside the sweep's 300 s per-store budget.
CHECK_BUDGET_S = 240.0


def _installed_records() -> list[dict[str, Any]]:
    """Records of games that are installed or mid-update. Blocking."""
    records, _unreadable = list_install_records()
    return [r for r in records if r.get("state") in ("installed", "updating")]


def _checkable(records: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """``(pid, version)`` of every installed game the service can check."""
    found: list[tuple[str, str]] = []
    for record in records:
        pid, version = str(record.get("productId")), str(record.get("version") or "")
        if record.get("state") == "installed" and PID_RE.match(pid) and VERSION_RE.match(version):
            found.append((pid, version))
    return found


class XboxPcUpdates:
    """The update check of installed Xbox PC games."""

    def __init__(self, xbox_pc: steps.XboxPcLike, cli: steps.XodusCliLike) -> None:
        """Keep the facade and the CLI adapter."""
        self._xbox_pc = xbox_pc
        self._cli = cli
        self._last: list[str] = []

    async def check_installed(self) -> list[str]:
        """``store_game_id`` of every game with an update. Never raises except on cancel."""
        if await steps.ready_code(self._xbox_pc) is not None:
            return []
        records = await asyncio.to_thread(_installed_records)
        if not records:
            self._last = []
            return []
        names = {str(r["productId"]): store_game_id(r) for r in records}
        pending = sorted(store_game_id(r) for r in records if r.get("state") == "updating")
        if self._cli.credential_lock.locked() or await asyncio.to_thread(components_in_use):
            logger.info("[xbox_updates] a download or a game holds the sign-in; using the last answer")
            return sorted(set(self._last) | set(pending))
        newer: list[str]
        try:
            newer = await asyncio.wait_for(self._ask(_checkable(records)), CHECK_BUDGET_S)
        except TimeoutError:
            logger.warning("[xbox_updates] the update check ran out of time")
            newer = []
        self._last = sorted({names[pid] for pid in newer} | set(pending))
        return self._last

    async def _ask(self, games: list[tuple[str, str]]) -> list[str]:
        """Start one service session and query every game."""
        if not games:
            return []
        async with self._cli.credential_lock:
            try:
                paths = await self._cli.backend()
                async with service_session(paths.service, self._cli.credential_dir()) as sock:
                    return await self._query_all(sock, games)
            except ServiceError as e:
                logger.warning("[xbox_updates] update check unavailable: %s", e)
            except Exception as e:
                logger.warning("[xbox_updates] update check unavailable: %s", cli_error_text(e))
        return []

    @staticmethod
    async def _query_all(sock: str, games: list[tuple[str, str]]) -> list[str]:
        """Ask about each game in turn; keep the ones with a newer package."""
        newer: list[str] = []
        for pid, version in games:
            verdict = await query(sock, pid, version)
            if verdict.available:
                logger.info("[xbox_updates] %s %s -> %s", pid, version, verdict.latest)
                newer.append(pid)
            elif verdict.available is None:
                logger.warning(
                    "[xbox_updates] could not check %s (stage %s)", pid, verdict.stage,
                )
        return newer
