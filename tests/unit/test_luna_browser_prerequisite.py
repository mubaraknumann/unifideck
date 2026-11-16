"""Prerequisite install integration tests for Luna (installer mocked).

Drives :meth:`LunaStore.install_browser` — Luna's half of the Req 9
browser-prerequisite flow — through its real body
(:meth:`LunaStore._run_browser_install` → ``EdgeBrowser.install`` under the
600 s ceiling, then :meth:`LunaStore._resume`), mocking only the installer so
no download runs and no browser is spawned. The browser is the plugin's own
Chromium (``auth/edge_browser/chrome_installer.py``), a native binary, so
there is no sandbox override step to apply after the install.

Covers:

* **not-installed → needs_browser** (Req 9.1): :meth:`LunaStore.is_available`
  reports the prerequisite is missing when the browser is not installed;
* **install failure or >600 s → installation_failed** (Req 9.4/9.5): the
  installer's reason is surfaced in ``metadata["reason"]`` and the interrupted
  operation is left in its pre-install state (not resumed);
* **success within 600 s** (Req 9.2–9.4): the interrupted operation is resumed
  and its result/status is folded back into the prerequisite result.

_Requirements: 9.1, 9.2, 9.3, 9.4, 9.5_
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from unifideck.core.types import AuthResult, Result
from unifideck.stores.luna.luna_store import (
    BROWSER_INSTALL_BUDGET_SECONDS,
    INSTALLATION_FAILED,
    LUNA_STORE,
    STATUS_KEY,
    LunaStatus,
    LunaStore,
)


class _Edge:
    """Stand-in for ``EdgeBrowser``.

    ``is_installed`` is a plain attribute so a test can flip it; ``install``
    is an :class:`AsyncMock` returning the installer's ``{"success": ...}``
    dict so :meth:`LunaStore._run_browser_install` sees the real shape.
    """

    def __init__(
        self,
        *,
        installed: bool,
        install_result: dict[str, Any] | None = None,
        install_exc: BaseException | None = None,
    ) -> None:
        self.is_installed = installed
        if install_exc is not None:
            self.install = AsyncMock(side_effect=install_exc)
        else:
            self.install = AsyncMock(
                return_value=install_result or {"success": True},
            )


def _store(edge: _Edge) -> LunaStore:
    """Build a ``LunaStore`` wired to the fakes, bypassing ``__init__``."""
    store = LunaStore.__new__(LunaStore)
    store._edge = edge
    # is_available also consults the session; these install/resume tests
    # assume a usable session unless a test overrides it.
    store._session = SimpleNamespace(is_usable=lambda: True)
    return store


# ── not-installed → needs_browser (Req 9.1) ──────────────────────────


async def test_not_installed_reports_needs_browser() -> None:
    """No browser → ``is_available`` is False (needs_browser, Req 9.1)."""
    edge = _Edge(installed=False)
    store = _store(edge)

    assert await store.is_available() is False
    # No install was attempted by merely checking availability.
    edge.install.assert_not_awaited()


async def test_installed_reports_available() -> None:
    """An installed browser plus a usable session → ``is_available`` is True."""
    edge = _Edge(installed=True)
    store = _store(edge)
    assert await store.is_available() is True
    # Browser installed but NO usable session → not connected.
    store._session = SimpleNamespace(is_usable=lambda: False)
    assert await store.is_available() is False


# ── install failure / >600 s → installation_failed (Req 9.4, 9.5) ─────


async def test_install_failure_reports_installation_failed_with_reason() -> None:
    """A failed install → ``installation_failed`` carrying the reason (Req 9.5)."""
    edge = _Edge(
        installed=False,
        install_result={"success": False, "error": "download checksum mismatch"},
    )
    store = _store(edge)
    resume = AsyncMock()

    result = await store.install_browser(resume=resume)

    assert isinstance(result, Result)
    assert result.success is False
    assert result.store == LUNA_STORE
    assert result.error == INSTALLATION_FAILED
    assert result.error_code == INSTALLATION_FAILED
    assert result.metadata["reason"] == "download checksum mismatch"
    edge.install.assert_awaited_once()
    resume.assert_not_awaited()


async def test_install_timeout_reports_installation_failed() -> None:
    """An install exceeding 600 s → ``installation_failed`` (Req 9.4/9.5)."""
    edge = _Edge(installed=False, install_exc=TimeoutError())
    store = _store(edge)
    resume = AsyncMock()

    result = await store.install_browser(resume=resume)

    assert result.success is False
    assert result.error == INSTALLATION_FAILED
    assert str(int(BROWSER_INSTALL_BUDGET_SECONDS)) in result.metadata["reason"]
    resume.assert_not_awaited()


async def test_install_exception_reports_installation_failed() -> None:
    """An unexpected installer exception is reported, not raised."""
    edge = _Edge(installed=False, install_exc=OSError("disk full"))
    store = _store(edge)

    result = await store.install_browser()

    assert result.success is False
    assert result.error == INSTALLATION_FAILED
    assert "disk full" in result.metadata["reason"]


# ── success within 600 s → resume (Req 9.2–9.4) ───────────────────────


async def test_success_resumes_auth() -> None:
    """A successful install resumes the interrupted op and folds its status."""
    edge = _Edge(installed=False, install_result={"success": True})
    store = _store(edge)

    resumed_auth = AuthResult(
        success=True, store=LUNA_STORE, tokens_cached=True,
        metadata={STATUS_KEY: LunaStatus.CONNECTED},
    )
    resume = AsyncMock(return_value=resumed_auth)

    result = await store.install_browser(resume=resume)

    edge.install.assert_awaited_once()
    resume.assert_awaited_once()
    assert result.success is True
    assert result.store == LUNA_STORE
    assert result.metadata["resumed"] is True
    assert result.metadata[STATUS_KEY] == LunaStatus.CONNECTED


async def test_success_resumes_get_library_folding_list_result() -> None:
    """A resumed ``get_library`` list (even empty) folds to success."""
    edge = _Edge(installed=False, install_result={"success": True})
    store = _store(edge)
    resume = AsyncMock(return_value=[])

    result = await store.install_browser(resume=resume)

    resume.assert_awaited_once()
    assert result.success is True
    assert result.metadata["resumed"] is True


async def test_success_resumed_get_library_none_is_unsuccessful() -> None:
    """A resumed ``get_library`` returning None folds to an unsuccessful result."""
    edge = _Edge(installed=False, install_result={"success": True})
    store = _store(edge)

    result = await store.install_browser(resume=AsyncMock(return_value=None))

    assert result.success is False
    assert result.metadata["resumed"] is True


async def test_success_without_resume_returns_bare_success() -> None:
    """No interrupted operation → bare success with ``resumed=False``."""
    edge = _Edge(installed=False, install_result={"success": True})
    store = _store(edge)

    result = await store.install_browser(resume=None)

    assert result.success is True
    assert result.metadata["resumed"] is False


async def test_already_installed_skips_install_and_resumes() -> None:
    """An already-installed browser skips the install and resumes directly."""
    edge = _Edge(installed=True)
    store = _store(edge)
    resume = AsyncMock(return_value=[])

    result = await store.install_browser(resume=resume)

    edge.install.assert_not_awaited()
    resume.assert_awaited_once()
    assert result.success is True
    assert result.metadata["resumed"] is True
