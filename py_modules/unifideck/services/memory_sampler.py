"""services/memory_sampler.py — periodic self-memory sampling for bundles.

Two users reported this backend reaching roughly 22 GB of ``VmData``
while the plugin sat idle, and neither support bundle could show it. The
bundle described the machine's memory but never this process's, and even
that would not have been enough: a single reading taken at capture time
cannot tell "it has always been this size" apart from "it is growing
12 MB a second".

So this service records one row a minute into a bounded ring, and the
bundle ships the whole ring. A reporter's capture then carries a growth
*curve*, which is what actually identifies the mechanism:

* ``VmData`` climbing while ``VmRSS`` stays flat and ``VmSwap`` grows is
  allocator fragmentation or cold retained pages, not a working set.
* ``tasks`` climbing is an asyncio task leak.
* ``threads`` climbing is a thread leak.
* ``ctx_switches`` climbing steeply between two samples means the loop
  was spinning, which separates a busy loop from an idle-but-fat process.

Sampling is deliberately cheap: one small ``/proc`` read plus two counter
lookups. The expensive analysis (a ``gc`` type histogram, ``tracemalloc``
top allocators) runs once, at capture time, from the observability RPC
mixin — not on this timer.

Everything recorded is a count or a size. No titles, ids or paths, which
is what keeps a bundle safe to paste in public.
"""
from __future__ import annotations

import asyncio
import gc
import logging
import time
from collections import deque
from typing import TYPE_CHECKING, Any

from unifideck.utils.proc_status import read_status_numeric

if TYPE_CHECKING:
    from unifideck.config import ConfigManager

logger = logging.getLogger(__name__)

# One sample a minute for twelve hours. Each row is a dozen small ints, so
# the whole ring costs a few hundred kB — negligible next to what it is
# there to measure, and long enough to cover the "20-30 minutes" in which
# the reported growth happens several times over.
DEFAULT_INTERVAL_SECONDS = 60
DEFAULT_MAX_SAMPLES = 720
# Floor, for the same reason AccountService has one: a zero here would
# turn the loop into a spin that allocates while claiming to diagnose
# allocation.
MIN_INTERVAL_SECONDS = 10


class MemorySamplerService:
    """Records this process's memory footprint on a timer."""

    def __init__(self, config: ConfigManager | None = None) -> None:
        """Size the ring and read the interval, then wait for ``start``."""
        self._config = config
        self._interval = DEFAULT_INTERVAL_SECONDS
        max_samples = DEFAULT_MAX_SAMPLES
        if config is not None:
            self._interval = max(
                MIN_INTERVAL_SECONDS,
                config.get_int(
                    "diagnostics.memory_sample_interval_seconds",
                    DEFAULT_INTERVAL_SECONDS,
                ),
            )
            max_samples = max(
                1,
                config.get_int(
                    "diagnostics.memory_sample_max", DEFAULT_MAX_SAMPLES,
                ),
            )
        self._samples: deque[dict[str, Any]] = deque(maxlen=max_samples)
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Take a boot sample, then begin the loop. Idempotent."""
        if self._task is not None:
            return
        self._maybe_start_tracemalloc()
        self._sample()
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "[MemorySampler] started (every %ds, keeping %d samples)",
            self._interval,
            self._samples.maxlen,
        )

    def _maybe_start_tracemalloc(self) -> None:
        """Begin allocation tracing when the user opted in.

        Off by default: tracing roughly doubles allocation cost, which is
        not something to impose on every Deck to diagnose two of them. It
        has to start here, at boot, because ``tracemalloc`` can only
        report allocations made after it was switched on — turning it on
        at capture time would show nothing.
        """
        if self._config is None:
            return
        if not self._config.get_bool("diagnostics.tracemalloc", False):
            return
        import tracemalloc

        if tracemalloc.is_tracing():
            return
        frames = max(
            1, self._config.get_int("diagnostics.tracemalloc_frames", 5),
        )
        tracemalloc.start(frames)
        logger.info(
            "[MemorySampler] tracemalloc tracing enabled (%d frames) — "
            "this slows allocation and is meant for diagnosis only",
            frames,
        )

    async def stop(self) -> None:
        """Cancel the loop and await its exit."""
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    def snapshot(self) -> list[dict[str, Any]]:
        """Return every retained sample, oldest first, for the bundle."""
        return list(self._samples)

    async def _loop(self) -> None:
        """Sample forever. A failed sample must never end the series."""
        while True:
            try:
                await asyncio.sleep(self._interval)
                self._sample()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("[MemorySampler] sample failed: %s", e)

    def _sample(self) -> None:
        """Append one row. Cheap enough to run on the event loop."""
        row: dict[str, Any] = {"t": round(time.time(), 1)}
        row.update(read_status_numeric())
        try:
            row["tasks"] = len(asyncio.all_tasks())
        except RuntimeError:
            # No running loop (the boot sample can land before one exists).
            pass
        # Generation counts only. ``len(gc.get_objects())`` would walk the
        # whole heap, which on the machines this is meant to diagnose is
        # exactly the multi-GB thing we must not touch every minute.
        row["gc_gen"] = list(gc.get_count())
        self._samples.append(row)
