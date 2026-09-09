"""services/download/warmup_runner.py — cancellable post-install prefix setup.

The install-time prefix warmup (:func:`prefix_warmup.warmup_install_prefix`)
runs *after* the store installer has already succeeded: the bytes are on disk
and the ``.unifideck-id`` marker is written. Cancelling it must therefore mean
"skip the setup", never "undo the install".

It used to be awaited directly inside the worker's ``_on_install_success``,
ahead of every piece of finalisation, which made that distinction impossible to
express. ``CancelledError`` is a ``BaseException``, so a cancel slipped past
``_run_prefix_warmup``'s ``except Exception`` and surfaced in ``_run_install``'s
``except asyncio.CancelledError``, which marked the item **cancelled**: no
``DOWNLOAD_COMPLETE``, no shortcut flip, no ``games.map`` entry, for a game
sitting fully installed on disk. Measured on a 5.5 GB GOG install.

Giving the warmup its own task and its own cancellation channel fixes that.
``DownloadService.cancel`` calls :meth:`PrefixWarmupRunner.cancel`, which
targets the warmup task alone; the awaiting worker coroutine absorbs the
resulting ``CancelledError`` and carries on finalising. An outer cancellation
(plugin shutdown, the install task itself being killed) is *not* absorbed —
:meth:`run` only swallows a cancel it was told about, and re-raises otherwise.

Abandoning a warmup is cheap and safe: every step is idempotent and the launch
path re-runs the whole sequence (``ensure_prefix_initialized`` sits at Phase 1.5
of ``orchestrator.launch_windows``, ahead of the cloud sync-down), so the only
cost is a slower first launch.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Coroutine
from typing import Any

logger = logging.getLogger(__name__)

# Ceiling on one warmup. Generous: a first-ever setup downloads the Steam Linux
# Runtime and installs ~9 winetricks verbs (measured ~100 s on this Deck, and
# the runtime download can dominate on a cold cache). Past this the install is
# finalised anyway and the prefix is built at first launch instead.
PREFIX_WARMUP_TIMEOUT_SEC: float = 600.0

# Outcome strings returned by :meth:`PrefixWarmupRunner.run`, for the caller to
# log. Deliberately not an enum: they exist only to be interpolated into one
# log line.
OUTCOME_COMPLETE = "completed"
OUTCOME_ABANDONED = "abandoned by cancel"
OUTCOME_TIMEOUT = "timed out"
OUTCOME_FAILED = "failed"


class PrefixWarmupRunner:
    """Runs prefix warmups one at a time, each individually cancellable.

    Serialised by design. The warmup reuses the launch path's prefix machinery,
    which touches process-wide and cross-game state: the shared umu runtime
    under ``~/.local/share/umu`` (whose retry ladder can ``rmtree`` the whole
    cache) and the shared winetricks download cache. Neither is protected by a
    lock, and today the only thing keeping warmups apart is that the download
    queue runs one install at a time. Owning the semaphore here keeps that
    guarantee even if the queue is ever widened or the warmup is detached from
    the install slot.
    """

    def __init__(self, timeout: float = PREFIX_WARMUP_TIMEOUT_SEC) -> None:
        """Store the per-warmup timeout and init empty task bookkeeping."""
        self._timeout = timeout
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        # Keys whose warmup was cancelled *through this class* rather than by
        # the surrounding task being torn down. Only these are absorbed.
        self._abandoned: set[str] = set()
        self._slot = asyncio.Semaphore(1)

    def is_running(self, key: str) -> bool:
        """Whether a warmup is currently in flight for ``key``."""
        task = self._tasks.get(key)
        return task is not None and not task.done()

    def cancel(self, key: str) -> bool:
        """Abandon the warmup for ``key``. True when one was running.

        Records the key first so :meth:`run` can tell this cancel apart from an
        outer teardown, then cancels the task. Returns immediately: the unwind
        can sit in an ``umu-run`` subprocess with its own kill budget, and no
        caller needs to wait for it.
        """
        task = self._tasks.get(key)
        if task is None or task.done():
            return False
        self._abandoned.add(key)
        task.cancel()
        return True

    async def run(
        self,
        key: str,
        make_coro: Callable[[], Coroutine[Any, Any, None]],
    ) -> str:
        """Run one warmup to completion, returning an outcome string.

        ``make_coro`` is a factory rather than a coroutine so nothing is
        created unless it is going to be awaited (an un-awaited coroutine
        raises a RuntimeWarning, which the test suite runs with ``-W error``).

        Never raises for a warmup-level problem: a timeout or an exception is
        logged and reported, because the install must complete either way.
        Re-raises ``CancelledError`` only when this runner was not the one that
        asked, so shutdown still works.
        """
        async with self._slot:
            task = asyncio.create_task(make_coro())
            self._tasks[key] = task
            try:
                await asyncio.wait_for(task, self._timeout)
                return OUTCOME_COMPLETE
            except asyncio.CancelledError:
                if not self._claim_abandoned(key):
                    raise
                await self._settle(task)
                return OUTCOME_ABANDONED
            except TimeoutError:
                # ``wait_for`` already cancelled the task; drain its unwind so
                # the prefix is not still being written after we return.
                await self._settle(task)
                logger.warning(
                    "[warmup] %s timed out after %ds — completing the install; "
                    "the prefix is built at first launch instead",
                    key, int(self._timeout),
                )
                return OUTCOME_TIMEOUT
            except Exception:
                logger.exception("[warmup] %s failed (install still completes)", key)
                return OUTCOME_FAILED
            finally:
                self._tasks.pop(key, None)
                self._abandoned.discard(key)

    def _claim_abandoned(self, key: str) -> bool:
        """Whether ``key``'s cancel came from :meth:`cancel`, consuming the flag."""
        if key not in self._abandoned:
            return False
        self._abandoned.discard(key)
        return True

    @staticmethod
    async def _settle(task: asyncio.Task[Any]) -> None:
        """Await a cancelled warmup's unwind, swallowing whatever it raises."""
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
