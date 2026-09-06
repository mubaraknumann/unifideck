"""Plugin-wide metrics collector — bus-driven counters + timers + gauges.

``MetricsCollector`` subscribes to a fixed set of bus events
and maintains three families of metrics:

* **Counters** — monotonically incrementing tallies
  (auth attempts/successes/failures, download
  queued/completed/failed). Wired declaratively in
  ``_subscribe_all``.
* **Timers**   — last observed duration in milliseconds for
  pair-events: auth start→complete, sync start→complete,
  download start→complete. ``_pending_timers`` holds the
  in-flight start times keyed by the operation; on
  completion the timer is finalised into ``_timers``.
* **Gauges**   — point-in-time floats (current sync game
  count, current store count).

Surfaced via ``get_plugin_metrics`` to the observability RPC
+ the QAM diagnostics tab. Reset via the dev-only ``reset``.

Note: this is the **plugin-level** metrics surface (counters
across all events). Per-handler latency and watchdog state
live in the ``event_bus/supervision`` package.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from unifideck.core.types import Events
from unifideck.event_bus.event_bus import EventBus
from unifideck.event_bus.event_bus_devex import auto_wire, subscribe

logger = logging.getLogger(__name__)

class MetricsCollector:
    """Bus-driven counters / timers / gauges aggregator."""

    def __init__(self, bus: EventBus) -> None:
        """Initialise state and subscribe to every observed event.

        Three dicts hold the metric state:

        * ``_counters`` — ``str → int``;
        * ``_gauges``   — ``str → float``;
        * ``_pending_timers`` — ``op_key → monotonic_start``
          for in-flight timers; finalised entries move to
          ``_timers``.

        ``_started_at`` captures process start (wall-clock)
        for the ``uptime_s`` field. Subscriptions happen in
        ``_subscribe_all`` so the constructor stays short.

        Args:
            bus: live event bus.
        """
        self._bus = bus
        self._counters: dict[str, int] = {}
        self._gauges: dict[str, float] = {}
        self._pending_timers: dict[str, float] = {}
        self._timers: dict[str, float] = {}
        self._started_at = time.time()
        self._subscribe_all()

    def _subscribe_all(self) -> None:
        """Wire bus subscriptions for every tracked event.

        Two paths used:

        1. **Counter lambdas** — declarative
           ``(event, counter_name)`` table; the lambda
           closure captures the counter name and forwards
           any kwargs (ignored).
        2. **Decorated handlers** — the ``_on_*`` methods
           carry ``@subscribe`` metadata, so a single
           ``auto_wire`` call registers all of them.

        ``auto_wire`` must run exactly **once**.
        ``EventBus.on`` allows duplicate registrations by
        design (it says so in its own docstring), so
        calling it once per counter row — as this method
        used to — subscribes every decorated handler seven
        times over and multiplies everything they record.

        Logs at INFO with both wiring counts once
        registration completes.
        """
        counter_events = [
            (Events.STORE_AUTH_STARTED, "auth_attempts"),
            (Events.STORE_AUTH_COMPLETE, "auth_successes"),
            (Events.STORE_AUTH_FAILED, "auth_failures"),
            (Events.SYNC_FAILED, "sync_failures"),
            (Events.DOWNLOAD_QUEUED, "download_queued"),
            (Events.DOWNLOAD_COMPLETE, "download_completed"),
            (Events.DOWNLOAD_FAILED, "download_failed"),
            # NOTE: no ``DOWNLOAD_CANCELLED`` row. That event already has a
            # ``@subscribe`` handler (``_on_download_cancelled``), and adding
            # a row here would register a *second* subscription for it —
            # ``test_every_handler_is_wired_exactly_once`` catches that. The
            # ``download_cancelled`` counter is incremented inside that
            # handler instead. Audit register item 4f.
        ]
        for event, name in counter_events:
            self._bus.on(event, lambda n=name, **kw: self._inc_counter(n))
        wired = auto_wire(self, self._bus)
        logger.info(
            "[MetricsCollector] wired (%d counter + %d decorated handlers)",
            len(counter_events),
            wired,
        )

    async def stop(self) -> None:
        """No-op shutdown hook.

        Kept on the API surface so the service container
        can call it uniformly on every collected service
        during plugin teardown. Subscriptions on the bus
        are cleared by the bus itself when it shuts down.
        """

    def get_plugin_metrics(self) -> dict[str, Any]:
        """Return a shallow snapshot of every metric family.

        Each family is copied (``dict(...)``) to decouple
        the caller from concurrent mutations. ``uptime_s``
        is computed inline from ``_started_at``.

        Returns:
            Dict with four keys: ``counters``, ``timers_ms``,
            ``gauges``, ``uptime_s``.
        """
        return {
            "counters": dict(self._counters),
            "timers_ms": dict(self._timers),
            "gauges": dict(self._gauges),
            "uptime_s": int(time.time() - self._started_at),
        }

    def reset(self) -> None:
        """Clear every metric family (dev / test only).

        Production code shouldn't need this — counters and
        timers are bounded by what fits in plugin lifetime.
        Useful in tests where the same collector instance
        is reused across cases.
        """
        self._counters.clear()
        self._gauges.clear()
        self._pending_timers.clear()
        self._timers.clear()

    def _inc_counter(self, name: str) -> None:
        """Bump ``_counters[name]`` by 1, creating it if absent.

        Args:
            name: counter identifier.
        """
        self._counters[name] = self._counters.get(name, 0) + 1

    @subscribe(Events.STORE_AUTH_STARTED)
    async def _on_auth_start(self, store: str = "", **kwargs: Any) -> None:
        """Stash the monotonic start time for an auth attempt.

        Keyed by ``"auth:<store>"`` so concurrent auth
        flows on different stores don't clobber each
        other.

        Args:
            store: store identifier from the event payload.
            **kwargs: ignored (other event fields).
        """
        self._pending_timers[f"auth:{store}"] = time.monotonic()

    @subscribe(Events.STORE_AUTH_COMPLETE)
    async def _on_auth_complete(self, store: str = "", **kwargs: Any) -> None:
        """Finalise the auth timer for ``store`` into ``auth_duration_ms``.

        Look up the pending start, compute elapsed, store
        as last-observed value. Missing pending entry
        (auth-failed or auth-completed without a matching
        start) is silently ignored.

        Args:
            store: store identifier.
            **kwargs: ignored.
        """
        self._complete_timer(f"auth:{store}", "auth_duration_ms")

    @subscribe(Events.SYNC_STARTED)
    async def _on_sync_start(self, **kwargs: Any) -> None:
        """Stash the monotonic start time for a sync.

        Single shared timer key (``"sync"``) — only one
        sync runs at a time across stores.

        Args:
            **kwargs: ignored.
        """
        self._pending_timers["sync"] = time.monotonic()

    @subscribe(Events.SYNC_COMPLETE)
    async def _on_sync_complete(self, **kwargs: Any) -> None:
        """Finalise the sync timer into ``sync_duration_ms``.

        Args:
            **kwargs: ignored.
        """
        self._complete_timer("sync", "sync_duration_ms")

    @subscribe(Events.DOWNLOAD_STARTED)
    async def _on_download_start(self, **kwargs: Any) -> None:
        """Stash the monotonic start time for a download.

        Keyed via :meth:`_download_key` so concurrent
        downloads don't conflict.

        ``setdefault``, not assignment: ``DOWNLOAD_STARTED``
        is emitted more than once per install by design.
        ``DownloadWorker`` re-emits it when the download
        finishes and prefix warmup begins, so the frontend
        refetches the queue and the row picks up its
        "preparing" phase. Assigning restarted the clock
        there, which made ``download_duration_ms`` measure
        only the warmup window instead of the install the
        user actually waited through.

        Safe only because every terminal event —
        ``COMPLETE``, ``FAILED`` **and** ``CANCELLED`` —
        pops the entry. Drop the cancel handler and a
        cancelled attempt's stale clock gets inherited by
        the retry of the same game.

        Args:
            **kwargs: the raw event payload.
        """
        self._pending_timers.setdefault(
            self._download_key(kwargs), time.monotonic(),
        )

    @subscribe(Events.DOWNLOAD_COMPLETE)
    async def _on_download_complete(self, **kwargs: Any) -> None:
        """Finalise the (store, game_id) download timer.

        Records into ``download_duration_ms``, spanning the
        first ``DOWNLOAD_STARTED`` through here — download
        plus prefix warmup, which is the whole time the
        queue row is occupied.

        Args:
            **kwargs: the raw event payload.
        """
        self._complete_timer(
            self._download_key(kwargs),
            "download_duration_ms",
        )

    @subscribe(Events.SYNC_COMPLETE)
    async def _on_sync_gauge(
        self,
        games: list[Any] | None = None,
        stores_synced: list[str] | None = None,
        **kw: Any,
    ) -> None:
        """Update the sync gauges from the SYNC_COMPLETE payload.

        Two gauges set from the post-sync state:

        * ``sync_games_total``   — count of games in the
          unified library;
        * ``sync_stores_count``  — count of stores that
          contributed.

        Both stored as ``float`` for uniform gauge typing
        (gauges may carry decimals like ratios; ints are
        the common case).

        Args:
            games: list of games (or None — gauge skipped).
            stores_synced: list of stores (or None — gauge
                skipped). Note: only set when ``games`` is
                also non-None, preserving the inner branch
                in the original logic.
            **kw: ignored.
        """
        if games is not None:
            self._gauges["sync_games_total"] = float(len(games))
            if stores_synced is not None:
                self._gauges["sync_stores_count"] = float(len(stores_synced))

    @subscribe(Events.STORE_AUTH_FAILED)
    async def _on_auth_failed(self, store: str = "", **kwargs: Any) -> None:
        """Discard the pending auth timer for ``store``.

        A failed attempt has no duration worth recording.
        Dropping the entry also stops a later
        ``STORE_AUTH_COMPLETE`` that arrives without its own
        start — the wrapper-store monitor emits one on token
        capture — from being measured against this failed
        attempt's clock.

        Args:
            store: store identifier.
            **kwargs: ignored.
        """
        self._pending_timers.pop(f"auth:{store}", None)

    @subscribe(Events.SYNC_FAILED)
    async def _on_sync_failed(self, **kwargs: Any) -> None:
        """Discard the pending sync timer.

        Args:
            **kwargs: ignored.
        """
        self._pending_timers.pop("sync", None)

    @subscribe(Events.DOWNLOAD_FAILED)
    async def _on_download_failed(self, **kwargs: Any) -> None:
        """Discard the pending download timer.

        The one cleanup that matters for more than accuracy:
        download keys carry a game id, so without this every
        failed or cancelled install would leave an entry in
        ``_pending_timers`` for the lifetime of the plugin
        process.

        Args:
            **kwargs: the raw event payload.
        """
        self._pending_timers.pop(self._download_key(kwargs), None)

    @subscribe(Events.DOWNLOAD_CANCELLED)
    async def _on_download_cancelled(self, **kwargs: Any) -> None:
        """Discard the pending download timer on cancellation.

        The third terminal event, and for a long time the
        missing one: only ``COMPLETE`` and ``FAILED`` popped
        the entry, so every cancelled install left a
        ``_pending_timers`` row keyed by store and game for
        the lifetime of the plugin process. It is also what
        makes ``_on_download_start``'s ``setdefault`` safe —
        see its docstring.

        Also increments ``download_cancelled``. The counter lives here
        rather than in ``counter_events`` because this event already has a
        subscription — a table row would make it two, which
        ``test_every_handler_is_wired_exactly_once`` rejects. Cancel-vs-fail
        is the distinction triage wants from a support bundle, and a user who
        cancels installs was previously invisible in ``plugin_metrics``.
        Audit register item 4f.

        Args:
            **kwargs: the raw event payload.
        """
        self._inc_counter("download_cancelled")
        self._pending_timers.pop(self._download_key(kwargs), None)

    @staticmethod
    def _download_key(payload: dict[str, Any]) -> str:
        """Build the ``_pending_timers`` key for a download event.

        Every live emitter is ``services/download/`` and passes
        the whole queue item as ``item`` — reading the ids out
        of it is the production path. Without it every download
        would share the key ``"dl::"`` and two concurrent ones
        would overwrite each other's start time.

        The top-level ``store``/``game_id`` read is kept as
        tolerance, not as a live contract: the Epic and Amazon
        installers used to emit their own store-shaped copy of
        each event (audit item #4) and a stray re-add should
        still land on the right key rather than on ``"dl::"``.

        Args:
            payload: the raw event kwargs.

        Returns:
            ``"dl:<store>:<game_id>"``.
        """
        item = payload.get("item")
        if not isinstance(item, dict):
            item = {}
        store = payload.get("store") or item.get("store") or ""
        game_id = payload.get("game_id") or item.get("game_id") or ""
        return f"dl:{store}:{game_id}"

    def _complete_timer(self, key: str, metric_name: str) -> None:
        """Compute elapsed time and record into ``_timers[metric_name]``.

        Pop-and-check: if no start was recorded for
        ``key``, silently return (missing pair). Otherwise
        the elapsed ms is the last-observed value for the
        metric (timers track most-recent, not average —
        the latency collector in event_bus does
        percentiles).

        Args:
            key: ``_pending_timers`` lookup key.
            metric_name: ``_timers`` key to write into.
        """
        started = self._pending_timers.pop(key, None)
        if started is None:
            return
        duration_ms = (time.monotonic() - started) * 1000
        self._timers[metric_name] = duration_ms
