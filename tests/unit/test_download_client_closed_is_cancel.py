"""Closing the vendor client before installing is a cancel, not a failure.

Ubisoft Connect and Battle.net installs are clicks inside the vendor's own
window. When the user closes that window without installing, the wrapper
install watcher now ends the install within seconds (``CLIENT_CLOSED``) and
the queue reports it as cancelled: no "Unexpected error" toast, no red
"Failed" row. A genuine failure (the client never started, say) still fails.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from unifideck.services.download.worker import _WorkerMixin
from unifideck.stores.shared.wrapper_install import CLIENT_CLOSED


def _run(result: SimpleNamespace) -> SimpleNamespace:
    worker = SimpleNamespace(
        _registry=SimpleNamespace(get_store=MagicMock(return_value=object())),
        _begin_install=AsyncMock(),
        _dispatch_install=AsyncMock(return_value=result),
        _on_install_success=AsyncMock(),
        _mark_cancelled=AsyncMock(),
        _emit_failure=AsyncMock(),
    )
    item = SimpleNamespace(store="ubisoft", game_id="80")
    asyncio.run(_WorkerMixin._execute_install(worker, item, "ubisoft:80"))  # type: ignore[arg-type]
    return worker


@pytest.mark.parametrize(
    "result",
    [
        SimpleNamespace(success=False, error=CLIENT_CLOSED, error_code=None),
        SimpleNamespace(success=False, error="closed early", error_code=CLIENT_CLOSED),
    ],
    ids=["ubisoft-error", "battlenet-error-code"],
)
def test_closing_the_client_is_reported_as_a_cancel(result: SimpleNamespace):
    worker = _run(result)
    worker._mark_cancelled.assert_awaited_once()
    worker._emit_failure.assert_not_awaited()


def test_an_install_that_was_never_detected_still_fails():
    worker = _run(SimpleNamespace(success=False, error="no_install_detected", error_code=None))
    worker._emit_failure.assert_awaited_once()
    worker._mark_cancelled.assert_not_awaited()
