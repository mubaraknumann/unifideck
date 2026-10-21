"""Xbox PC titles in the Microsoft library: ``XboxPcLibrary.compose``.

``MicrosoftStore.get_library`` reads xCloud as before, then hands its rows
to :meth:`XboxPcLibrary.compose`, which adds the PC titles and the install
state. The CLI read (:meth:`XboxPcLibrary.start_read`) starts at the same
time as the xCloud read, and the whole PC refresh has a hard budget of
``REFRESH_BUDGET_S`` inside the 120 s per-store sync timeout.

A list from ``get_library`` is authoritative: reconcile deletes every
Microsoft shortcut missing from it. So the PC rows are dropped only when
the feature is turned off or after an explicit Xodus sign-out. Every other
failure serves the last good titles:

========================================  ===================================
Read result                               Titles used
========================================  ===================================
clean                                     fresh
partial (skipped rows or unknown plans)   fresh, plus last good for the gaps
failed, signed out, runtime missing,      last good; ``SYNC_SKIPPED`` once
budget spent, or a 0-row anomaly          per reason
account changed                           fresh only; ``SYNC_SKIPPED`` once
========================================  ===================================

With the feature off there is no CLI call, and the last good titles are
restricted to installed games, so those stay playable and uninstallable.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from unifideck.core.types import Events, Game, GameTag
from unifideck.launcher.xbox_pc.install_records import read_install_record
from unifideck.stores.microsoft.microsoft_catalog import fetch_products_detailed
from unifideck.stores.shared.install_status import merge_install_status
from unifideck.stores.shared.installed_path import install_path_from_record
from unifideck.utils.locale import get_unifideck_locale

from .entitlements import EntitlementSnapshot, EnvelopeError, access_of, parse_library_output
from .installed import InstalledState, is_verified, load_installed
from .package_plan import FetchDetailed, fetch_plan_products
from .rows import (
    META_KEY,
    PcTitle,
    StickyIds,
    classify,
    dedupe_titles,
    installed_floor,
    installed_overlay,
    merge_with_xcloud,
    row_product_ids,
    summarize_roots,
)
from .snapshot import XboxPcSnapshot

if TYPE_CHECKING:
    from unifideck.config import ConfigManager
    from unifideck.event_bus.event_bus import EventBus

logger = logging.getLogger(__name__)

#: The PC refresh's share of the 120 s per-store sync timeout.
REFRESH_BUDGET_S = 75.0
#: Plan reads stop this much before the budget, so the ids they did not
#: reach count as unknown (last good plan reused) instead of failing the
#: whole refresh.
PLAN_MARGIN_S = 5.0
DEFAULT_MARKET = "US"
_MARKET_RE = re.compile(r"^[A-Z]{2}\Z")

LIST_KEPT = "xbox_pc_list_kept"
SIGNIN_NEEDED = "xbox_pc_signin_needed"
ACCOUNT_CHANGED = "xbox_pc_account_changed"
INSTALLED_UNREADABLE = "xbox_pc_installed_unreadable"
#: Emitted by the store when an account with no Game Pass still lists PC rows.
PC_ONLY = "xbox_pc_only"
#: Every ``SYNC_SKIPPED`` reason of the Xbox PC list. Each needs a row in
#: ``SYNC_SKIPPED_KEYS`` (``boot-event-listener.tsx``); a test reads this.
SYNC_SKIPPED_REASONS = (LIST_KEPT, SIGNIN_NEEDED, ACCOUNT_CHANGED, INSTALLED_UNREADABLE, PC_ONLY)


class LibraryReadLike(Protocol):
    """``xodus.cli.LibraryRead``: ``status`` is ok, signed_out, not_ready,
    failed or busy; ``stdout`` holds the JSON line on ok (it has the email:
    never log it)."""

    status: str
    stdout: str
    tail: str


class XodusCliLike(Protocol):
    async def library(self, market: str) -> LibraryReadLike: ...


class XboxPcLike(Protocol):
    def enabled(self) -> bool: ...

    def set_library_counts(self, counts: dict[str, Any]) -> None: ...


@dataclass(frozen=True)
class _Outcome:
    """One refresh. ``kept`` means the last good titles stay as they are."""

    titles: list[PcTitle]
    kept: bool = False
    reason: str | None = None
    snap: EntitlementSnapshot | None = None
    clean: bool = False


def _kept(reason: str | None) -> _Outcome:
    return _Outcome(titles=[], kept=True, reason=reason)


def _cancel(task: asyncio.Future[Any] | None) -> None:
    if task is not None and not task.done():
        task.cancel()


class XboxPcLibrary:
    """The PC half of the Microsoft library."""

    def __init__(
        self, config: ConfigManager | None, xbox_pc: XboxPcLike, cli: XodusCliLike, *,
        cache: Any, bus: EventBus,
        fetch_products: FetchDetailed = fetch_products_detailed,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._xbox_pc = xbox_pc
        self._cli = cli
        self._bus = bus
        self._fetch = fetch_products
        self._clock = clock
        self._monotonic = monotonic
        self._snapshot = XboxPcSnapshot(cache)
        self._by_sgid: dict[str, dict[str, Any]] = {}
        self._stale = True

    # ── market ───────────────────────────────────────────────────────────
    def fallback_market(self) -> str:
        """The market without an xCloud session: the last read, then the
        region of the Unifideck locale, then ``US``."""
        snap_market = self._snapshot.library().get("market")
        parts = (get_unifideck_locale(self._config) or "").replace("_", "-").split("-")
        region = parts[-1].upper() if len(parts) > 1 else ""
        for candidate in (snap_market, region):
            if isinstance(candidate, str) and _MARKET_RE.match(candidate):
                return candidate
        return DEFAULT_MARKET

    def resolve_market(self, market: str | None) -> str:
        """``market`` (the xCloud session's) when valid, else the fallback."""
        upper = market.upper() if isinstance(market, str) else ""
        return upper if _MARKET_RE.match(upper) else self.fallback_market()

    # ── read ─────────────────────────────────────────────────────────────
    def start_read(self, market: str | None) -> asyncio.Task[Any] | None:
        """Start the CLI read for ``market``, or None when the feature is off.

        The store starts it next to the xCloud read and passes it back to
        :meth:`compose` as ``pending_read``.
        """
        if not self._xbox_pc.enabled():
            return None
        resolved = self.resolve_market(market)
        return asyncio.ensure_future(self._cli.library(resolved))

    def now(self) -> float:
        """The clock the refresh budget is measured on."""
        return self._monotonic()

    async def compose(
        self, xcloud: list[Game], *, market: str | None,
        pending_read: asyncio.Task[Any] | None = None,
        started: float | None = None,
    ) -> list[Game]:
        """``xcloud`` with the PC titles and install state added.

        ``pending_read`` is the task from :meth:`start_read`; compose starts
        its own when it is None. A pending read is always finished or
        cancelled before this returns. ``started`` (from :meth:`now`) is
        when the store's library read began. The budget counts from there,
        so the xCloud reads that come first cannot push the whole sync
        past the per-store timeout.
        """
        started = self._monotonic() if started is None else started
        resolved = self.resolve_market(market)
        try:
            installed = await self._load_installed()
            if self._xbox_pc.enabled():
                titles = await self._refresh_titles(resolved, pending_read, started)
            else:
                _cancel(pending_read)
                titles = self._installed_titles(installed)
            rows = self._assemble(xcloud, titles, installed, resolved)
        finally:
            _cancel(pending_read)
        self._xbox_pc.set_library_counts(self.status_counts())
        return rows

    async def _load_installed(self) -> InstalledState:
        """The install records. Unreadable files are reported, never touched."""
        state = await asyncio.to_thread(load_installed)
        if not state.unreadable:
            self._snapshot.drop_notices(INSTALLED_UNREADABLE)
            return state
        logger.error(
            "[XboxPcLibrary] %d Xbox PC install record(s) could not be read and "
            "were left as they are; their games show as not installed: %s",
            len(state.unreadable), ", ".join(str(p) for p in state.unreadable),
        )
        await self._notify(INSTALLED_UNREADABLE)
        return state

    def _installed_titles(self, installed: InstalledState) -> list[PcTitle]:
        """The last good titles of installed games only (feature off)."""
        keep = set(installed.floor_records())
        return [t for t in self._snapshot.titles() if {t.entitlement_id, t.target_id} & keep]

    async def _refresh_titles(
        self, market: str, pending_read: asyncio.Task[Any] | None, started: float,
    ) -> list[PcTitle]:
        """Fresh titles within the budget, or the last good ones."""
        deadline = started + REFRESH_BUDGET_S
        task = pending_read or self.start_read(market)
        try:
            async with asyncio.timeout(max(0.0, deadline - self._monotonic())):
                outcome = await self._read_titles(task, market, deadline)
        except TimeoutError:
            logger.warning(
                "[XboxPcLibrary] the Xbox PC list did not refresh within %.0f s; "
                "the last good list is kept", REFRESH_BUDGET_S,
            )
            outcome = _kept(LIST_KEPT)
        return await self._apply(outcome, market)

    async def _read_titles(
        self, task: asyncio.Task[Any] | None, market: str, deadline: float,
    ) -> _Outcome:
        """Read, parse and classify one ``library`` answer."""
        if task is None:
            return _kept(None)
        try:
            read = await task
        except Exception as e:
            logger.warning("[XboxPcLibrary] the Xbox PC list read failed: %s", type(e).__name__)
            return _kept(LIST_KEPT)
        if read.status != "ok":
            logger.info("[XboxPcLibrary] Xbox PC list not read: %s", read.status)
            # A running game holds the credentials: expected, so no toast.
            return _kept({"busy": None, "signed_out": SIGNIN_NEEDED}.get(read.status, LIST_KEPT))
        try:
            snap = parse_library_output(read.stdout, market=market, now=self._clock())
        except EnvelopeError as e:
            logger.warning("[XboxPcLibrary] unusable Xbox PC list answer: %s", e.code)
            return _kept(LIST_KEPT)
        await self._check_account(snap)
        summaries, unknown_roots = await self._plans_for(snap, market, deadline)
        titles, unknown = classify(snap, summaries, unknown_roots, self._clock())
        return self._judge(snap, titles, unknown)

    async def _check_account(self, snap: EntitlementSnapshot) -> None:
        """A different account's titles are never shown: clear them, once."""
        previous = self._snapshot.library().get("account_hash")
        if not previous or previous == snap.account_hash:
            return
        logger.warning("[XboxPcLibrary] the Xodus account changed; its old Xbox PC list was cleared")
        self._snapshot.clear()
        self._by_sgid = {}
        await self._emit(ACCOUNT_CHANGED)

    async def _plans_for(
        self, snap: EntitlementSnapshot, market: str, deadline: float,
    ) -> tuple[dict[str, dict[str, Any]], set[str]]:
        """Plan summaries of every entitlement with access, cached for 24 h."""
        now = self._clock()
        roots = [pid for pid in sorted(snap.entries) if access_of(pid, snap.entries, now)]
        cached = self._snapshot.plans(market, now)
        missing = [r for r in roots if r not in cached]
        found = await fetch_plan_products(
            missing, market, fetch=self._fetch, deadline=deadline - PLAN_MARGIN_S,
            clock=self._monotonic,
        )
        fresh, unknown = summarize_roots(missing, found)
        self._snapshot.save_plans(market, fresh, now)
        return {**cached, **fresh}, unknown

    def _judge(self, snap: EntitlementSnapshot, titles: list[PcTitle], unknown: set[str]) -> _Outcome:
        """Clean, partial, or a 0-row anomaly that keeps the last good list."""
        last = self._snapshot.titles()
        if not snap.entries and last:
            logger.warning("[XboxPcLibrary] the Xbox PC list came back empty; the last good list is kept")
            return _kept(LIST_KEPT)
        if snap.skipped == 0 and not unknown:
            return _Outcome(titles=titles, snap=snap, clean=True)
        fresh_ents = {t.entitlement_id for t in titles}
        gaps = [t for t in last if t.entitlement_id in unknown
                or (snap.skipped > 0 and t.entitlement_id not in snap.entries)]
        logger.warning(
            "[XboxPcLibrary] partial Xbox PC list: %d row(s) skipped, %d plan(s) unknown; "
            "%d last good title(s) kept for them", snap.skipped, len(unknown), len(gaps),
        )
        merged = titles + [t for t in gaps if t.entitlement_id not in fresh_ents]
        return _Outcome(titles=dedupe_titles(merged), snap=snap)

    async def _apply(self, outcome: _Outcome, market: str) -> list[PcTitle]:
        """Store a fresh outcome, or report a kept one. Returns the titles."""
        if outcome.kept or outcome.snap is None:
            self._stale = True
            if outcome.reason:
                await self._notify(outcome.reason)
            return self._snapshot.titles()
        self._stale = not outcome.clean
        self._snapshot.update(
            account_hash=outcome.snap.account_hash, market=market,
            checked_at=outcome.snap.checked_at, clean=outcome.clean,
            skipped=outcome.snap.skipped, titles=[t.to_meta(market) for t in outcome.titles],
        )
        self._snapshot.drop_notices(LIST_KEPT, SIGNIN_NEEDED)
        return outcome.titles

    # ── assemble ─────────────────────────────────────────────────────────
    def _assemble(
        self, xcloud: list[Game], titles: list[PcTitle], installed: InstalledState, market: str,
    ) -> list[Game]:
        """Merge, keep installed rows, overlay install state, remember ids."""
        floor = installed.floor_records()
        sticky = StickyIds.from_sources(self._snapshot.ids(), floor.values())
        last = {t.entitlement_id: t for t in self._snapshot.titles()}
        rows = merge_with_xcloud(xcloud, titles, sticky, market)
        rows = installed_floor(rows, floor, sticky, last, market)
        rows = merge_install_status(
            rows, installed_overlay(rows, installed.verified),
            path_key="installRoot", exe_key="exe",
        )
        self._remember(rows)
        return rows

    def _remember(self, rows: list[Game]) -> None:
        """Keep each PC row's ids and meta for the next sync and ``title_for``."""
        ids = self._snapshot.ids()
        by_sgid: dict[str, dict[str, Any]] = {}
        for game in rows:
            meta = game.metadata.get(META_KEY)
            if GameTag.XBOX_PC not in game.tags or not isinstance(meta, dict):
                continue
            by_sgid[game.store_game_id] = meta
            for pid in row_product_ids(game) - {game.store_game_id.upper()}:
                ids[pid] = game.store_game_id
            ids[game.store_game_id.upper()] = game.store_game_id
        self._by_sgid = by_sgid
        if ids != self._snapshot.ids():
            self._snapshot.update(ids=ids)

    # ── queries ──────────────────────────────────────────────────────────
    def title_for(self, store_game_id: str) -> dict[str, Any] | None:
        """The ``metadata["xbox_pc"]`` dict of a row, or None.

        Backed by the snapshot, so it works after a restart before the first
        sync.
        """
        meta = self._by_sgid.get(store_game_id)
        if meta is not None:
            return dict(meta)
        sticky = StickyIds(self._snapshot.ids())
        market = str(self._snapshot.library().get("market") or DEFAULT_MARKET)
        key = store_game_id.upper()
        for title in self._snapshot.titles():
            if sticky.lookup(title.entitlement_id, title.target_id) == store_game_id or key in (
                title.entitlement_id, title.target_id,
            ):
                return title.to_meta(market)
        return None

    def record_for(self, store_game_id: str) -> dict[str, Any] | None:
        """The install record of a row, by its product, target or own id.

        Blocking (one small file read per candidate): call it in a thread.
        """
        meta = self.title_for(store_game_id) or {}
        for pid in (meta.get("product_id"), meta.get("target_id"), store_game_id.upper()):
            record = read_install_record(pid) if isinstance(pid, str) else None
            if record is not None:
                return record
        return None

    def installed_exe(self, store_game_id: str) -> str | None:
        """The record's absolute exe when the game passes the disk check.

        ``MicrosoftStore.find_installed_exe`` returns this, so the download
        worker never guesses an exe. Blocking: call it in a thread.
        """
        record = self.record_for(store_game_id)
        return str(record["exe"]) if record is not None and is_verified(record) else None

    def installed_path(self, store_game_id: str) -> str | None:
        """The record's ``installRoot``, or None. Blocking: call it in a thread."""
        return install_path_from_record(self.record_for(store_game_id), key="installRoot")

    def size_for(self, store_game_id: str) -> int | None:
        """The planned download size in bytes, before install, or None."""
        size = (self.title_for(store_game_id) or {}).get("size_bytes")
        return size if isinstance(size, int) and not isinstance(size, bool) and size > 0 else None

    def status_counts(self) -> dict[str, Any]:
        """The ``status.library`` shape: counts of the last good list.

        ``installable`` counts titles with a package to install, ``choice``
        those that need a package choice (not installable in v1).
        """
        titles = self._snapshot.titles()
        data = self._snapshot.library()
        return {
            "installable": sum(1 for t in titles if t.state in ("ready", "choice") and t.content_id),
            "not_supported": sum(1 for t in titles if t.state == "not_supported"),
            "choice": sum(1 for t in titles if t.state == "choice" and not t.content_id),
            "skipped": int(data.get("skipped") or 0),
            "stale": self._stale or not data.get("clean"),
            "last_read": data.get("checked_at"),
        }

    def clear(self) -> None:
        """Forget the PC list (explicit Xodus sign-out)."""
        self._snapshot.clear()
        self._by_sgid = {}

    # ── notices ──────────────────────────────────────────────────────────
    async def notify_once(self, reason: str) -> None:
        """``SYNC_SKIPPED(reason)`` once per snapshot, for the store's own
        reasons (``PC_ONLY``), so a sync does not repeat the toast."""
        await self._notify(reason)

    async def _notify(self, reason: str) -> None:
        """Emit ``SYNC_SKIPPED(reason)`` once, until a good read resets it."""
        if self._snapshot.has_notice(reason):
            return
        self._snapshot.add_notice(reason)
        await self._emit(reason)

    async def _emit(self, reason: str) -> None:
        await self._bus.emit(Events.SYNC_SKIPPED, store="microsoft", reason=reason)


def has_pc_rows(rows: list[Game] | None) -> bool:
    """Whether ``rows`` holds any PC row (the store's ``xbox_pc_only`` check)."""
    return any(GameTag.XBOX_PC in g.tags for g in rows or [])
