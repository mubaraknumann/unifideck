"""The legendary launcher OAuth token — read, staleness, refresh.

``EpicAchievements`` and ``EpicSessions`` both authenticate against Epic's
own endpoints with the token legendary keeps in ``user.json``, and each held
its own copy of the four pieces that involves. ``sessions.py``'s header
declared the duplication and its reason: *"Kept self-contained rather than
shared with achievements to avoid disturbing that working path; the resolver
is small and stable."*

Measuring the two copies is what changed that call (audit register item 47).
``_parse_ts``, ``_is_expired`` and ``_resolve_auth`` were byte-identical
apart from docstrings and line wrapping — nothing to disturb. But
``_refresh_token`` had **diverged, and each copy carried a defect the other
had fixed**:

* the achievements copy passed no ``env``, so ``legendary status`` inherited
  the plugin's environment. ``clean_cli_env`` exists precisely because that
  leaks ``LD_LIBRARY_PATH`` and friends into bundled CLIs;
* the achievements copy also referenced ``proc`` in its ``except`` block
  without initialising it first, so if ``create_subprocess_exec`` itself
  raised — a ``FileNotFoundError`` for a stale ``cli_path``, which the
  ``if not self._cli_path`` guard does not catch, since it only checks the
  string is non-empty — the handler raised ``UnboundLocalError`` **out of**
  the refresh and masked the real error;
* the sessions copy passed no ``stdin``, leaving the child attached to
  whatever the parent had. legendary prompts on bare ``input()`` in places
  ``--yes`` does not gate (UD-026), and a prompt with a live stdin waits
  rather than failing;
* the sessions copy logged no return code, so a refresh that ran and failed
  looked identical in the log to one that worked.

This implementation takes all four of those, which means it is not either
former copy: a drift finding names a difference, not a direction (audit
§3.2), and here neither side was the direction.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from pathlib import Path
from typing import Any

from unifideck.core.binaries import clean_cli_env
from unifideck.stores.shared.timestamps import parse_timestamp

logger = logging.getLogger(__name__)

#: Refresh the launcher token this many seconds before it actually expires,
#: so a request issued now does not arrive after expiry.
TOKEN_SKEW_SECONDS = 120


class LegendaryLauncherAuth:
    """Mixin: resolve Epic credentials from legendary's ``user.json``.

    The host class supplies three attributes, both consumers already using
    exactly these names:

    * ``_cli_path`` — the legendary binary, or ``None`` when unresolved;
    * ``_user_file`` — ``Path`` to legendary's ``user.json``;
    * ``_info_timeout`` — seconds to allow a refresh subprocess.

    Every method is best-effort. A missing, unreadable or unrefreshable
    token yields ``(None, None)`` so the caller degrades to "not signed in"
    rather than raising into a playtime report or an achievements fetch.
    """

    _cli_path: str | None
    _user_file: Path
    _info_timeout: float

    #: Log prefix, so a shared line still names the subsystem it came from.
    _LOG_TAG = "epic.auth"

    async def _resolve_auth(
        self, force_refresh: bool = False,
    ) -> tuple[str | None, str | None]:
        """``(access_token, account_id)``, refreshing via legendary if stale.

        ``force_refresh`` refreshes even when ``expires_at`` looks valid —
        used to recover from a present-but-rejected token (clock skew or
        revocation), where the local expiry says nothing is wrong.
        """
        data = self._read_user()
        if (force_refresh or self._is_expired(data)) and self._cli_path:
            await self._refresh_token()
            data = self._read_user()
        return data.get("access_token"), data.get("account_id")

    def _read_user(self) -> dict[str, Any]:
        """Read legendary ``user.json`` (``{}`` on any failure)."""
        try:
            if self._user_file.is_file():
                data = json.loads(self._user_file.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            logger.debug("[%s] user.json read failed", self._LOG_TAG, exc_info=True)
        return {}

    @staticmethod
    def _is_expired(data: dict[str, Any]) -> bool:
        """Is the stored token past its skew-adjusted expiry?

        An absent or unparseable ``expires_at`` counts as expired, so an
        unknown state provokes one refresh attempt rather than a request
        that is certain to be rejected.
        """
        exp = parse_timestamp(data.get("expires_at"))
        if exp is None:
            return True
        return time.time() >= (exp - TOKEN_SKEW_SECONDS)

    async def _refresh_token(self) -> None:
        """Best-effort refresh: ``legendary status`` rewrites ``user.json``.

        See this module's docstring for why each argument is here — the two
        former copies were each missing one of them.
        """
        if not self._cli_path:
            return
        logger.info("[%s] refreshing Epic token via legendary", self._LOG_TAG)
        # Bound before the try: ``create_subprocess_exec`` can raise
        # ``FileNotFoundError`` for a stale ``_cli_path``, and the handler
        # below touches ``proc``.
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                self._cli_path, "status",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=clean_cli_env(),
            )
            await asyncio.wait_for(proc.communicate(), timeout=self._info_timeout)
            logger.info(
                "[%s] token refresh done (rc=%s)", self._LOG_TAG, proc.returncode,
            )
        except (TimeoutError, OSError) as e:
            logger.warning("[%s] token refresh failed: %s", self._LOG_TAG, e)
            if proc is not None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
