"""Error handling, retry/recovery, and the Steam Input layout request.

The network/API retry + recovery loop (Req 11.1/11.2/11.5/11.6) and the
gamepad Steam Input layout request (Req 6.5/6.6) for the Luna store, split out
of the former single ``luna_store.py`` as a mixin that
:class:`~unifideck.stores.luna.luna_store.LunaStore` composes.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, TypeVar

from unifideck.core.types import Events
from unifideck.stores.luna.luna_store_base import _LunaStoreState
from unifideck.stores.luna.luna_store_constants import (
    _STEAM_CDP_PORT,
    LUNA_STORE,
    MAX_ATTEMPTS,
    MAX_RETRIES,
    STEAM_INPUT_LAYOUT_BUDGET_SECONDS,
    LunaOperationError,
    LunaStatus,
)


def _retry_backoff_seconds() -> float:
    """The current retry backoff, read from the package namespace.

    Resolved at call time (not import time) so a test that monkeypatches
    ``unifideck.stores.luna.luna_store._RETRY_BACKOFF_SECONDS`` — the historical
    seam, kept re-exported from the package ``__init__`` — can shrink the retry
    budget to zero without reaching into this module.
    """
    import unifideck.stores.luna.luna_store as pkg

    return pkg._RETRY_BACKOFF_SECONDS

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

T = TypeVar("T")


class _LunaRecoveryMixin(_LunaStoreState):
    """Steam Input layout request + the retry/recovery loop (Req 6.5, 11.x)."""

    # ── Steam Input layout request (Req 6.5, 6.6) ───────────────────────

    async def request_steam_input_layout(
        self, shortcut_appid: int, *, steam_port: int = _STEAM_CDP_PORT,
    ) -> bool:
        """Request a gamepad Steam Input layout for a Luna shortcut (Req 6.5).

        Called once a Luna Browser_Game_Entry's shortcut becomes the active
        launch target. Reuses ``refresh_steam_controller_layout`` — the same
        controller-popup bounce the xCloud path uses — to push the gamepad
        layout onto the shortcut, and bounds the whole request to 5s so a
        hung Steam CDP never blocks past the requirement (Req 6.5).

        Returns ``True`` when the layout bounce completed, ``False`` when it
        did not apply within the 5s budget (``refresh_steam_controller_layout``
        returned falsy, raised, or timed out). On a non-apply the caller
        surfaces the manual layout-selection guidance (Req 6.6); the method
        itself only reports whether automatic application completed.
        """
        if shortcut_appid <= 0:
            logger.warning(
                "[LunaStore] steam-input layout request skipped: invalid "
                "appid %r", shortcut_appid,
            )
            return False
        from unifideck.launcher.cdp.steam_controller_popup import (
            refresh_steam_controller_layout,
        )

        try:
            applied = await asyncio.wait_for(
                refresh_steam_controller_layout(
                    steam_port,
                    shortcut_appid,
                    # Fire immediately (no pre-delay): the 5s budget is the
                    # requirement window, not a thing to spend waiting.
                    delay=0.0,
                    dwell=2.5,
                ),
                timeout=STEAM_INPUT_LAYOUT_BUDGET_SECONDS,
            )
        except TimeoutError:
            logger.warning(
                "[LunaStore] gamepad Steam Input layout not applied within "
                "%.0fs for appid %s — surfacing manual layout steps (Req 6.6)",
                STEAM_INPUT_LAYOUT_BUDGET_SECONDS, shortcut_appid,
            )
            return False
        except Exception as exc:  # best-effort layout request
            logger.warning(
                "[LunaStore] gamepad Steam Input layout request failed for "
                "appid %s: %s — surfacing manual layout steps (Req 6.6)",
                shortcut_appid, exc,
            )
            return False

        if not applied:
            logger.warning(
                "[LunaStore] gamepad Steam Input layout did not apply for "
                "appid %s — surfacing manual layout steps (Req 6.6)",
                shortcut_appid,
            )
        else:
            logger.info(
                "[LunaStore] gamepad Steam Input layout requested for appid %s",
                shortcut_appid,
            )
        return bool(applied)

    # ── Error handling + recovery (Req 11.1, 11.2, 11.5, 11.6) ──────────

    async def _with_retry(
        self, operation: str, action: Callable[[], Awaitable[T]],
    ) -> T:
        """Run a network/API ``action`` with retry + a recovery indicator.

        Retries ``action`` up to 3 times after the first attempt (Req 11.5).
        While retrying, a ``recovery_in_progress`` indicator is emitted so the
        frontend can show recovery is underway (Req 11.6). Each failure is
        logged within 1s with a timestamp, the operation name, and an error
        description (Req 11.2). When every attempt fails, the last known
        stable connection state is restored and a retry action is exposed
        before :class:`LunaOperationError` is raised (Req 11.1).

        ``action`` signals a recoverable failure by raising; a successful
        attempt's return value is passed straight through.
        """
        last_error: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                result = await action()
            except Exception as exc:  # uniform retry boundary
                last_error = exc
                self._log_operation_failure(operation, attempt, exc)
                if attempt <= MAX_RETRIES:
                    # Still retrying: show the recovery indicator and back off
                    # briefly before the next attempt (Req 11.6).
                    await self._emit_recovery_in_progress(operation, attempt)
                    await asyncio.sleep(_retry_backoff_seconds())
                    continue
                # Retry budget exhausted: restore the last stable state and
                # expose a retry action (Req 11.1), then surface the failure.
                await self._restore_stable_state(operation)
                raise LunaOperationError(
                    f"{operation} failed after {MAX_ATTEMPTS} attempt(s): "
                    f"{type(exc).__name__}: {exc}",
                ) from exc
            else:
                return result
        # Unreachable: the loop either returns or raises. Keeps mypy happy.
        raise LunaOperationError(  # pragma: no cover - defensive
            f"{operation} failed: {last_error}",
        )

    def _log_operation_failure(
        self, operation: str, attempt: int, exc: Exception,
    ) -> None:
        """Log one network/API failure with a timestamp + op + description.

        Req 11.2 requires the record within 1s of the failure; the log call
        is synchronous and happens immediately on the failing attempt, so the
        line is written well inside the budget. The timestamp is emitted
        explicitly (not only via the log formatter) so the requirement holds
        regardless of how the launcher log is formatted.
        """
        logger.warning(
            "[LunaStore] %s | operation=%s attempt=%d/%d failed: %s: %s",
            time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
            operation, attempt, MAX_ATTEMPTS,
            type(exc).__name__, exc,
        )

    async def _emit_recovery_in_progress(
        self, operation: str, attempt: int,
    ) -> None:
        """Signal the frontend that a recovery retry is underway (Req 11.6)."""
        try:
            await self._bus.emit(
                Events.SYNC_SKIPPED, store=LUNA_STORE,
                reason=LunaStatus.RECOVERY_IN_PROGRESS,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(
                "[LunaStore] recovery-indicator emit failed (%s op=%s "
                "attempt=%d): %s", LunaStatus.RECOVERY_IN_PROGRESS,
                operation, attempt, exc,
            )

    async def _restore_stable_state(self, operation: str) -> None:
        """Restore + expose the last known stable state and a retry action.

        Req 11.1: after a failed operation, the store returns to the last
        connection state it was known to be in and exposes a retry action.
        The stable status is already tracked in ``_last_stable_status`` (set
        by every terminal, non-recovery outcome); here it is re-surfaced to
        the frontend so the panel leaves the recovery indicator and offers
        the retry.
        """
        logger.info(
            "[LunaStore] %s failed — restoring stable state %r with a retry "
            "action", operation, self._last_stable_status,
        )
        # ``SYNC_SKIPPED`` is the live, frontend-rendered status channel the
        # store already uses (see Microsoft's precedent): re-emitting the last
        # stable status here leaves the recovery indicator and lets the panel
        # offer the retry action, without inventing a second status channel.
        try:
            await self._bus.emit(
                Events.SYNC_SKIPPED, store=LUNA_STORE,
                reason=self._last_stable_status,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(
                "[LunaStore] stable-state restore emit failed: %s", exc,
            )
