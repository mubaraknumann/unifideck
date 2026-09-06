"""stores/epic/sessions.py — report play sessions to Epic (+ read totals).

Mirror of ``gog/sessions.py`` for Epic. Pushes finalized local sessions to Epic
so the launcher's "Time Played" / other devices reflect them, and reads the
account's totals back for display.

Auth is the legendary launcher OAuth token from ``user.json``, refreshed via
``legendary status`` when stale — shared with ``epic/achievements.py`` through
``LegendaryLauncherAuth``. This header used to say the resolver was kept
self-contained "to avoid disturbing that working path"; measuring the two
copies found one of them silently broken, so they were merged (audit register
item 47).
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from pathlib import Path

from .launcher_auth import LegendaryLauncherAuth
from .playtime_api import fetch_epic_playtime_all, put_epic_session

logger = logging.getLogger(__name__)

# Totals are pulled per-game during a drain; cache the whole-account map briefly
# so one drain only hits ``/all`` once.
_TOTALS_TTL_SECONDS = 60.0


class EpicSessions(LegendaryLauncherAuth):
    """Push play sessions to Epic and read back totals (launcher-token auth)."""

    _LOG_TAG = "epic.sessions"

    def __init__(
        self,
        cli_path: str | None,
        user_file: str,
        machine_id: str,
        info_timeout: float = 30.0,
    ) -> None:
        """Initialize the instance."""
        self._cli_path = cli_path
        self._user_file = Path(user_file).expanduser()
        self._machine_id = machine_id
        self._info_timeout = info_timeout
        self._totals: tuple[float, dict[str, int]] | None = None

    async def report_session(
        self, game_id: str, started_at_unix: int, duration_secs: int,
    ) -> bool:
        """Report one session to Epic. ``True`` on success, else ``False``.

        ``game_id`` is the Epic ``artifactId`` (== legendary app_name). On a
        rejected (401) token, force a refresh and retry once.
        """
        token, account_id = await self._resolve_auth()
        if not token or not account_id:
            return False
        end = datetime.fromtimestamp(
            started_at_unix + duration_secs, tz=UTC,
        )
        start = datetime.fromtimestamp(started_at_unix, tz=UTC)
        start_iso = start.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        end_iso = end.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        token_type = self._read_user().get("token_type", "bearer")

        code = await asyncio.to_thread(
            put_epic_session, account_id, str(game_id), token_type, token,
            start_iso, end_iso, self._machine_id,
        )
        if code == 401:
            token, account_id = await self._resolve_auth(force_refresh=True)
            if not token or not account_id:
                return False
            code = await asyncio.to_thread(
                put_epic_session, account_id, str(game_id), token_type, token,
                start_iso, end_iso, self._machine_id,
            )
        ok = code is not None and 200 <= code < 300
        if not ok:
            logger.info("[epic.sessions] report %s → HTTP %s", game_id, code)
        return ok

    async def get_total_secs(self, game_id: str) -> int | None:
        """Epic's total time played for ``game_id`` (artifactId), in seconds."""
        totals = await self._fetch_totals()
        if totals is None:
            return None
        return totals.get(str(game_id))

    # -- auth (legendary launcher token; mirrors epic/achievements.py) ------

    async def _fetch_totals(self) -> dict[str, int] | None:
        """The account's ``{artifactId: secs}`` map, TTL-cached. 401 → refresh."""
        if self._totals and (time.monotonic() - self._totals[0]) < _TOTALS_TTL_SECONDS:
            return self._totals[1]
        token, account_id = await self._resolve_auth()
        if not token or not account_id:
            return None
        token_type = self._read_user().get("token_type", "bearer")
        code, mapping = await asyncio.to_thread(
            fetch_epic_playtime_all, account_id, token_type, token,
        )
        if code == 401:
            token, account_id = await self._resolve_auth(force_refresh=True)
            if not token or not account_id:
                return None
            code, mapping = await asyncio.to_thread(
                fetch_epic_playtime_all, account_id, token_type, token,
            )
        if code != 200:
            return None
        self._totals = (time.monotonic(), mapping)
        return mapping

