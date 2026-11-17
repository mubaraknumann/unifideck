"""The account's entitlements on demand: what the game page's DLC list and Install read.

The library sync reads ``xodus-cli library`` too; every parsed answer, the
sync's or ours, is kept here (:meth:`EntitlementCache.remember`), and the
ids with install access are also kept across restarts in the library
snapshot (``owned``).

:meth:`EntitlementCache.owned_ids` never makes the user wait when anything
is known: a stale answer, or the ids kept across restarts, answer at once
while a new read runs in the background. The CLI read takes seconds and is
refused outright while a sync or a running game holds the sign-in, and the
Install button waits on this answer.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from .entitlements import (
    INSTALL_ACCESS,
    EntitlementSnapshot,
    EnvelopeError,
    access_of,
    parse_library_output,
)

logger = logging.getLogger(__name__)


class _ReadLike(Protocol):
    @property
    def status(self) -> str: ...
    @property
    def stdout(self) -> str: ...


class _CliLike(Protocol):
    async def library(self, market: str) -> _ReadLike: ...


class _SnapshotLike(Protocol):
    def owned(self) -> list[str]: ...
    def update(self, **fields: Any) -> None: ...


def installable_ids(snap: EntitlementSnapshot, now: float) -> list[str]:
    """Product ids of ``snap`` with install access (owned, Game Pass), sorted."""
    return sorted(pid for pid in snap.entries if access_of(pid, snap.entries, now) in INSTALL_ACCESS)


class EntitlementCache:
    """The last entitlement read, and reads on demand."""

    def __init__(
        self, cli: _CliLike, snapshot: _SnapshotLike, *,
        enabled: Callable[[], bool],
        resolve_market: Callable[[str | None], str],
        check_account: Callable[[EntitlementSnapshot], Awaitable[None]],
        clock: Callable[[], float], monotonic: Callable[[], float],
    ) -> None:
        self._cli = cli
        self._snapshot = snapshot
        self._enabled = enabled
        self._resolve_market = resolve_market
        self._check_account = check_account
        self._clock = clock
        self._monotonic = monotonic
        self._last: tuple[EntitlementSnapshot, float] | None = None
        self._lock = asyncio.Lock()
        self._refresh: asyncio.Future[Any] | None = None

    def remember(self, snap: EntitlementSnapshot) -> None:
        """Keep a parsed read, and its installable ids across restarts."""
        self._last = (snap, self._monotonic())
        owned = installable_ids(snap, self._clock())
        if owned != self._snapshot.owned():
            self._snapshot.update(owned=owned)

    async def entitlements(
        self, market: str | None, *, max_age_s: float, force: bool = False,
    ) -> EntitlementSnapshot | None:
        """The entitlement rows, read again when older than ``max_age_s``.

        One read at a time. A read that cannot run now (a game or a sync
        holds the sign-in, signed out, failed) answers with the last read,
        or None when there is none. Never raises for an expected failure.
        """
        if not self._enabled():
            return None
        async with self._lock:
            last = self._last
            if last is not None and not force and self._monotonic() - last[1] < max_age_s:
                return last[0]
            resolved = self._resolve_market(market)
            read = await self._cli.library(resolved)
            if read.status != "ok":
                logger.info("[XboxPcLibrary] entitlements not read for the DLC list: %s", read.status)
                return last[0] if last else None
            try:
                snap = parse_library_output(read.stdout, market=resolved, now=self._clock())
            except EnvelopeError as e:
                logger.warning("[XboxPcLibrary] unusable entitlements answer for the DLC list: %s", e.code)
                return last[0] if last else None
            await self._check_account(snap)
            self.remember(snap)
            return snap

    async def owned_ids(
        self, market: str | None, *, max_age_s: float, force: bool = False,
    ) -> list[str] | None:
        """Product ids the account can install. Never waits when anything is known.

        A read younger than ``max_age_s`` answers. An older one, or the ids
        kept across restarts, answer at once and a new read starts in the
        background for the next ask. Only with nothing known at all, or
        ``force``, does this wait for a read. None when there was never one.
        """
        if not self._enabled():
            return None
        last = self._last
        if last is not None and not force and self._monotonic() - last[1] < max_age_s:
            return installable_ids(last[0], self._clock())
        known = installable_ids(last[0], self._clock()) if last is not None else self._snapshot.owned()
        if known and not force:
            self._refresh_soon(market, max_age_s)
            return known
        snap = await self.entitlements(market, max_age_s=max_age_s, force=force)
        if snap is not None:
            return installable_ids(snap, self._clock())
        return known or None

    def _refresh_soon(self, market: str | None, max_age_s: float) -> None:
        """Read in the background, one read at a time."""
        if self._refresh is not None and not self._refresh.done():
            return
        self._refresh = asyncio.ensure_future(self.entitlements(market, max_age_s=max_age_s))
