"""services/download/worker_warmup.py — post-install prefix setup + slot policy.

A game whose download has finished and that is only setting up its prefix no
longer holds a download slot. It stays in ``_running`` (so it is still shown,
still cancellable, and still blocks a duplicate ``add``), but its key is also
in ``_warming``, and :meth:`_download_slots_used` does not count it. The next
queued game therefore starts downloading while the previous one is set up.

Warmups themselves stay one at a time: :class:`~.warmup_runner.PrefixWarmupRunner`
owns a semaphore for that.

One exception is kept. A wrapper-store install (Ubisoft Connect, Battle.net)
runs its vendor client through umu/Proton, and a warmup touches the same shared
umu runtime and winetricks cache with no lock around them. Such an item waits at
the head of the queue until no warmup is running.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from unifideck.launcher.wrapper_stores import is_wrapper_store

from .worker_helpers import prefix_warmup_supported

if TYPE_CHECKING:
    from .models import DownloadItem

logger = logging.getLogger(__name__)


class _WarmupMixin:
    """Prefix warmup + download-slot accounting for the download worker."""

    _running: dict[str, DownloadItem]
    _queue: list[DownloadItem]
    _max_concurrent: int

    if TYPE_CHECKING:
        # Provided by ``_WorkerMixin``.
        async def _emit_queue_refresh(self, item: DownloadItem) -> None: ...

    def _warming_keys(self) -> set[str]:
        """Keys whose download is done and whose prefix is being set up.

        Looked up with ``getattr`` so the mixin stays usable on a host that
        never created the set.
        """
        warming = getattr(self, "_warming", None)
        if warming is None:
            warming = set()
            self._warming = warming
        return warming

    def _download_slots_used(self) -> int:
        """Running items that still occupy a download slot."""
        warming = self._warming_keys()
        return sum(1 for key in self._running if key not in warming)

    def _head_can_start(self) -> bool:
        """Whether the item at the head of the queue may start now.

        False only for a wrapper-store item while a warmup is running.
        """
        if not self._queue:
            return False
        head = self._queue[0]
        return not (self._warming_keys() and is_wrapper_store(head.store))

    def _warmup_applies(self, item: DownloadItem, result: Any = None) -> bool:
        """Whether this install gets an install-time prefix warmup.

        Store and depot eligibility lives in ``worker_helpers``. The rest is
        whether the host actually wired both hooks — the launcher-subset
        bootstrap wires neither.
        """
        if not prefix_warmup_supported(item, result):
            return False
        has_hook = callable(getattr(self, "_prefix_warmup", None))
        return has_hook and getattr(self, "_warmup_runner", None) is not None

    async def _run_prefix_warmup(self, item: DownloadItem, key: str) -> None:
        """Run install-time prefix setup, surfaced as a "preparing" phase.

        Callers must have checked :meth:`_warmup_applies`. Cancellation
        semantics belong to :class:`~.warmup_runner.PrefixWarmupRunner`: a
        cancel aimed at the warmup is absorbed there so the install still
        finalises, while an outer teardown propagates. Best-effort either way —
        a timeout or a failure is logged and the install completes, with the
        launch-time path as the fallback.

        While it runs, the key is in ``_warming``, which frees the download
        slot for the next queued game.
        """
        hook: Any = getattr(self, "_prefix_warmup", None)
        runner: Any = getattr(self, "_warmup_runner", None)
        item.download_phase = "preparing"
        warming = self._warming_keys()
        warming.add(key)
        try:
            await self._emit_queue_refresh(item)
            logger.info("[DownloadWorker] running prefix warmup for %s", key)
            outcome = await runner.run(key, lambda: hook(item))
            logger.info("[DownloadWorker] prefix warmup %s for %s", outcome, key)
        finally:
            warming.discard(key)
