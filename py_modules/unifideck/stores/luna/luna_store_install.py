"""Install lifecycle: streaming-only refusals + the browser prerequisite.

Luna titles are cloud streams, so the three install hooks the ``StoreBase`` ABC
requires are refused (Req 13.6); the one real install here is the
streaming-browser prerequisite (Req 9): the plugin's own Chromium download. Split out of the former single
``luna_store.py`` as a mixin that
:class:`~unifideck.stores.luna.luna_store.LunaStore` composes.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, TypeVar

from unifideck.core.types import InstallResult, Result
from unifideck.stores.luna.luna_store_base import _LunaStoreState
from unifideck.stores.luna.luna_store_constants import (
    BROWSER_INSTALL_BUDGET_SECONDS,
    INSTALLATION_FAILED,
    LUNA_STORE,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

T = TypeVar("T")


class _LunaInstallMixin(_LunaStoreState):
    """Streaming-only install refusals + the Req 9 browser prerequisite."""

    # ── Install lifecycle: refused, not faked (streaming-only, Req 13.6) ─
    #
    # Luna titles are cloud streams: nothing is downloaded, nothing lives on
    # disk, and the stream is always current. The three install hooks the ABC
    # requires therefore cannot be satisfied and say so with a visible
    # failure rather than a phantom ``success=True`` (the Microsoft store's
    # reasoning applies verbatim).
    _NOT_SUPPORTED = "not_supported"

    async def install_game(self, game_id: str, **kwargs: Any) -> InstallResult:
        """Refuse: Luna titles stream, so there is nothing to install."""
        logger.warning(
            "[LunaStore] refusing install for game_id=%s — Luna streams and "
            "has no local install", game_id,
        )
        return InstallResult(
            success=False, store=LUNA_STORE, game_id=game_id,
            install_path=None, error=self._NOT_SUPPORTED,
            error_code=self._NOT_SUPPORTED,
        )

    async def uninstall_game(self, game_id: str, **kwargs: Any) -> Result:
        """Refuse: nothing was installed, so nothing can be reclaimed."""
        return Result(
            success=False, store=LUNA_STORE,
            error=self._NOT_SUPPORTED, error_code=self._NOT_SUPPORTED,
        )

    async def update_game(self, game_id: str, **kwargs: Any) -> InstallResult:
        """Refuse: the stream is always current, so there is no update."""
        return InstallResult(
            success=False, store=LUNA_STORE, game_id=game_id,
            error=self._NOT_SUPPORTED, error_code=self._NOT_SUPPORTED,
        )

    async def check_for_updates(self) -> list[str]:
        """Streaming-only: there is never a local update to apply."""
        return []

    async def get_game_size(self, game_id: str) -> int | None:
        """Streaming-only: nothing is on disk, so the size is unknown."""
        return None

    # ── Prerequisite install (shared Chromium installer) ────────────────

    def is_browser_installed(self) -> bool:
        """Whether a supported streaming browser is installed (Req 9.1)."""
        return bool(self._edge.is_installed)

    async def install_browser(
        self,
        resume: Callable[[], Awaitable[T]] | None = None,
    ) -> Result:
        """Install the streaming browser and resume what was blocked.

        This is Luna's half of the Req 9 prerequisite flow. When
        :meth:`is_available` reported ``needs_browser`` (Req 9.1), the
        frontend offers the install and calls this to run it. The install
        itself is the shared one (``EdgeBrowser.install`` → the pinned
        Chromium download in ``chrome_installer.py``); Luna only adds the
        600 s ceiling and resuming the operation the missing browser
        interrupted.

        Outcomes (design error table, Req 9.4–9.5):

        * **install failure or >600s** → ``Result(success=False,
          error=installation_failed)`` carrying the installer's reason in
          ``metadata["reason"]``; the interrupted operation is not resumed
          and can be retried (Req 9.5).
        * **success within 600s** → if a ``resume`` callable was handed in,
          the interrupted Luna operation is run and its result surfaced, so
          the user does not have to restart it. ``metadata["resumed"]``
          records whether a resume ran.

        Args:
          resume: Optional zero-arg coroutine factory for the operation the
            missing browser interrupted (e.g. ``self.start_auth`` or
            ``self.get_library``). Run only after a successful install.
        """
        # Already present → nothing to install; resume straight away so a
        # racing second caller still gets its interrupted operation finished.
        if self._edge.is_installed:
            logger.info(
                "[LunaStore] install_browser: browser already installed",
            )
            return await self._resume(resume)

        reason = await self._run_browser_install()
        if reason is not None:
            logger.warning(
                "[LunaStore] install_browser failed: %s", reason,
            )
            return Result(
                success=False, store=LUNA_STORE, error=INSTALLATION_FAILED,
                error_code=INSTALLATION_FAILED, metadata={"reason": reason},
            )

        logger.info("[LunaStore] install_browser succeeded")
        return await self._resume(resume)

    async def _run_browser_install(self) -> str | None:
        """Run the shared browser install under the 600s ceiling (Req 9.5).

        Returns ``None`` on success, or a human-readable failure reason
        string. A timeout past :data:`BROWSER_INSTALL_BUDGET_SECONDS` is
        reported as a reason so the caller maps it to ``installation-failed``.
        """
        try:
            raw = await asyncio.wait_for(
                self._edge.install(),
                timeout=BROWSER_INSTALL_BUDGET_SECONDS,
            )
        except TimeoutError:
            return (
                f"browser install did not complete within "
                f"{int(BROWSER_INSTALL_BUDGET_SECONDS)}s"
            )
        except Exception as exc:  # installer never raises, but be safe
            logger.warning("[LunaStore] browser install error: %s", exc)
            return f"{type(exc).__name__}: {exc}"
        if raw.get("success"):
            return None
        # Surface the installer's reason verbatim so the frontend can show
        # the specific message.
        return str(raw.get("error") or "browser install failed")

    async def _resume(
        self, resume: Callable[[], Awaitable[T]] | None,
    ) -> Result:
        """Run the operation the missing browser blocked, if there is one."""
        if resume is None:
            return Result(success=True, store=LUNA_STORE, metadata={"resumed": False})
        logger.info("[LunaStore] resuming the interrupted Luna operation")
        resumed = await resume()
        return self._result_from_resumed(resumed)

    @staticmethod
    def _result_from_resumed(resumed: Any) -> Result:
        """Fold the resumed operation's outcome into a prerequisite result.

        ``resume`` may be any Luna operation: a :class:`Result` /
        :class:`AuthResult` (``start_auth``), a ``list[Game] | None``
        (``get_library``), or something else. The success and any carried
        ``metadata`` (e.g. the resulting :class:`LunaStatus`) are propagated so
        the frontend sees the resumed operation's state, with
        ``metadata["resumed"]`` flagging that a resume ran.
        """
        if isinstance(resumed, Result):
            resumed.metadata.setdefault("resumed", True)
            return resumed
        # ``get_library``-style resume: a list (even empty) means the browser
        # is now usable and the catalog was read; ``None`` means it still
        # could not be read.
        success = resumed is not None
        return Result(
            success=success, store=LUNA_STORE,
            metadata={"resumed": True},
        )
