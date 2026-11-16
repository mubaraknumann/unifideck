"""The browser-level auto-attach spoofer orchestration (task 5.3, Req 14.1-14.13).

:class:`LunaPlatformSpoofer` installs a browser-level
``Target.setAutoAttach`` and applies the full platform override to every
target before releasing it from waiting-for-debugger, then :meth:`verify`
reads back the platform Luna sees. The I/O (the websocket auto-attach loop)
is kept out of the pure payload builders in
:mod:`unifideck.stores.luna.luna_platform_spoofer.payloads`, so
:meth:`run_attach` / :meth:`run_verify` are drivable against a mock
:class:`~unifideck.stores.luna.luna_platform_spoofer.session.BrowserCdpSession`.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from unifideck.stores.luna.luna_cdp import attach_to_page_target
from unifideck.stores.luna.luna_platform_spoofer.payloads import (
    _VERIFY_PLATFORM_EXPRESSION,
    LunaSpoofConfigError,
    SpoofIdentity,
    _is_domain_not_found,
    build_platform_override_script,
    build_set_user_agent_override_params,
    build_spoof_identity,
    derive_browser_version,
    extract_platform_from_evaluate,
)

if TYPE_CHECKING:
    from unifideck.auth.edge_browser.cdp_client import EdgeCDPClient
    from unifideck.stores.luna.luna_config import LunaConfig
    from unifideck.stores.luna.luna_platform_spoofer.session import (
        BrowserCdpSession,
        _AiohttpBrowserCdpSession,
    )

logger = logging.getLogger(__name__)


def _new_browser_session(ws_url: str) -> _AiohttpBrowserCdpSession:
    """Open the aiohttp browser CDP session for a websocket URL.

    Resolved through the package namespace rather than a direct import so a
    test that monkeypatches
    ``unifideck.stores.luna.luna_platform_spoofer._AiohttpBrowserCdpSession``
    (the historical seam, "patch at the module each imports it into") still
    swaps the session the spoofer opens.
    """
    import unifideck.stores.luna.luna_platform_spoofer as pkg

    session: _AiohttpBrowserCdpSession = pkg._AiohttpBrowserCdpSession(ws_url)
    return session


class LunaPlatformSpoofer:
    """Browser-level CDP OS-identity override for a whole Luna session.

    Constructed from :class:`LunaConfig` alone (no :class:`LunaStore`
    dependency) so it runs in the launcher subprocess. :meth:`attach`
    installs a browser-level ``Target.setAutoAttach`` with ``flatten`` and
    ``waitForDebuggerOnStart`` so **every** target — current, future, page
    or service worker — is caught, and applies the full platform override
    to each target **before** releasing it from waiting-for-debugger. A
    page-level-only override is lost on navigation to the game page (F3);
    browser-level auto-attach re-applies to the fresh target, which is why
    this must be browser-level.

    :meth:`verify` evaluates
    ``navigator.userAgentData.getHighEntropyValues(['platform'])`` on the
    active page after the detail page has loaded and **before** the
    play-click, and returns the reported platform string (Req 14.11). It
    does **not** decide the mismatch itself: the launch flow (task 7.4)
    compares the returned value to the configured
    ``spoof_uach_platform`` and maps a mismatch to
    ``platform_spoofing_failure`` (abort + log the reported value —
    Req 14.12, 11.4).
    """

    def __init__(self, config: LunaConfig) -> None:
        """Build the spoofer bound to a resolved :class:`LunaConfig`."""
        self.config = config
        self._identity: SpoofIdentity | None = None

    @property
    def identity(self) -> SpoofIdentity | None:
        """The runtime-derived identity, or ``None`` before :meth:`attach`."""
        return self._identity

    async def attach(self, cdp: EdgeCDPClient) -> None:
        """Install the browser-level override for the whole session (Req 14.1).

        Derives the real installed-browser version over ``cdp`` (Req 14.8,
        aborting via :class:`LunaSpoofConfigError` if undeterminable),
        resolves the browser websocket on ``browser_game_cdp_port()``, opens
        a :class:`BrowserCdpSession` over it and drives the auto-attach loop.

        Raises :class:`LunaSpoofConfigError` when the browser version cannot
        be determined (the launcher maps this to
        ``platform_spoofing_failure`` and aborts — Req 14.9, 14.13).
        """
        identity = build_spoof_identity(self.config, derive_browser_version(cdp))
        self._identity = identity
        ws_url = cdp.get_browser_ws_url()
        if not ws_url:
            raise LunaSpoofConfigError(
                "",
                "cannot attach platform spoofer: no browser websocket URL "
                "on the browser-game CDP port",
            )
        session = _new_browser_session(ws_url)
        try:
            await self.run_attach(session, identity)
        finally:
            await session.close()

    async def run_attach(
        self, session: BrowserCdpSession, identity: SpoofIdentity,
    ) -> None:
        """Drive the auto-attach loop over an arbitrary ``session`` (Req 14.1-14.7).

        Separated from :meth:`attach` so it can be driven against a mock
        :class:`BrowserCdpSession` (property 1 / task 5.4): the loop sends
        the browser-level ``Target.setAutoAttach`` and, for every attached
        target, applies the full override before resuming it. Carrying
        ``identity`` explicitly keeps this method free of any live-browser
        dependency.
        """
        await session.send(
            "Target.setAutoAttach",
            {
                "autoAttach": True,
                "flatten": True,
                "waitForDebuggerOnStart": True,
            },
        )
        async for session_id, target_info in session.attached_targets():
            await self._apply_override_to_target(
                session, session_id, target_info, identity,
            )

    async def verify(self, cdp: EdgeCDPClient) -> str:
        """Report the platform Luna sees on the loaded detail page (Req 14.11).

        Evaluates
        ``navigator.userAgentData.getHighEntropyValues(['platform'])`` on
        the active Luna page target — the signal Luna gates Linux on (F3) —
        and returns the reported platform string (e.g. ``"Windows"``). Must
        be called *after* the detail page has loaded and *before* the
        play-click.

        This method does **not** decide the mismatch: the launch flow
        (task 7.4) compares the returned value to
        ``config.spoof_uach_platform`` and maps a mismatch to
        ``platform_spoofing_failure`` (abort + log the reported value —
        Req 14.12, 11.4). It mirrors :meth:`attach`: resolve the browser
        websocket on ``browser_game_cdp_port()``, open a
        :class:`BrowserCdpSession`, and attach to the page target to run the
        evaluate against it.

        Raises :class:`LunaSpoofConfigError` when no page target or browser
        websocket is available, or when the probe cannot be read, so the
        launcher aborts rather than proceeding on an unverified session.
        """
        ws_url = cdp.get_browser_ws_url()
        if not ws_url:
            raise LunaSpoofConfigError(
                "",
                "cannot verify platform: no browser websocket URL on the "
                "browser-game CDP port",
            )
        session = _new_browser_session(ws_url)
        try:
            return await self.run_verify(session)
        finally:
            await session.close()

    async def run_verify(self, session: BrowserCdpSession) -> str:
        """Evaluate the platform probe over an arbitrary ``session`` (Req 14.11).

        Separated from :meth:`verify` so it can be driven against a mock
        :class:`BrowserCdpSession`: it attaches to the active page target,
        evaluates :data:`_VERIFY_PLATFORM_EXPRESSION` with ``awaitPromise``
        and ``returnByValue`` (``getHighEntropyValues`` is async), and hands
        the raw CDP result to the pure
        :func:`extract_platform_from_evaluate` for the value extraction.
        """
        page_session_id = await self._attach_to_page_target(session)
        result = await session.send(
            "Runtime.evaluate",
            {
                "expression": _VERIFY_PLATFORM_EXPRESSION,
                "awaitPromise": True,
                "returnByValue": True,
            },
            session_id=page_session_id,
        )
        platform = extract_platform_from_evaluate(result)
        logger.info("[Luna] platform verification reported %r", platform)
        return platform

    async def _attach_to_page_target(self, session: BrowserCdpSession) -> str:
        """Return a CDP session id attached to the active Luna page target.

        Asks the browser for its targets and attaches (flatten mode) to the
        first ``page`` target — the loaded Luna detail page the probe must
        run against. Raises :class:`LunaSpoofConfigError` when no page
        target exists so :meth:`verify` aborts the launch (Req 14.13).

        Delegates the shared CDP page-target-attach step to
        :func:`unifideck.stores.luna.luna_cdp.attach_to_page_target`, passing
        the spoofer's own :class:`LunaSpoofConfigError` for each failure.
        """
        return await attach_to_page_target(
            session,
            raise_no_target=lambda: LunaSpoofConfigError(
                "",
                "cannot verify platform: no page target found on the "
                "browser-game CDP port",
            ),
            raise_no_session=lambda: LunaSpoofConfigError(
                "",
                "cannot verify platform: failed to attach to the Luna page "
                "target",
            ),
        )

    async def _apply_override_to_target(
        self,
        session: BrowserCdpSession,
        session_id: str,
        target_info: dict[str, Any],
        identity: SpoofIdentity,
    ) -> None:
        """Apply the full override to one target, then resume it (Req 14.2-14.7).

        Ordering is the invariant property 1 asserts: the UA/UA-CH override
        **and** the pre-document script are applied *before*
        ``Runtime.runIfWaitingForDebugger`` releases the target. The
        pre-document script is additionally evaluated once for the current
        document so an already-loaded page is covered too.

        The ``Emulation.setUserAgentOverride`` is the core of the spoof and is
        broadly supported, so it is always applied to every target. The
        ``Page.*`` + current-document steps, however, are only meaningful on
        targets that expose the CDP ``Page`` domain: a ``background_page``,
        ``service_worker``, ``worker`` or ``other`` target can reject
        ``Page.enable`` with ``-32601`` ("'Page.enable' wasn't found"). That
        domain-not-found rejection must not abort the whole attach (it would
        fail the entire sign-in — the real on-device failure), so it is
        tolerated per-target: the UA override still stands and the target is
        still resumed. Any *other* error (or a failure of the always-applied
        steps) still propagates.
        """
        ua_params = build_set_user_agent_override_params(self.config, identity)
        target_type = str(target_info.get("type", "?"))

        await session.send(
            "Emulation.setUserAgentOverride", ua_params, session_id=session_id,
        )
        try:
            await self._apply_page_override(session, session_id)
            logger.info(
                "[Luna] platform override applied to %s target (session %s)",
                target_type, session_id,
            )
        except RuntimeError as exc:
            if not _is_domain_not_found(exc):
                raise
            logger.info(
                "[Luna] Page domain unavailable on %s target "
                "(session %s) — applied UA override only",
                target_type, session_id,
            )
        # Always resume: a target left paused in waiting-for-debugger (a
        # background_page in particular) can hang the whole browser.
        await session.send(
            "Runtime.runIfWaitingForDebugger", {}, session_id=session_id,
        )

    async def _apply_page_override(
        self, session: BrowserCdpSession, session_id: str,
    ) -> None:
        """Apply the ``Page.*`` + current-document override to one target.

        Enables the ``Page`` domain, installs the override script so it runs
        before any script of every future document
        (``addScriptToEvaluateOnNewDocument``), and evaluates it once for the
        already-loaded document. Raises :class:`RuntimeError` on any CDP
        error, including the ``-32601`` domain-not-found the caller tolerates
        for targets without the ``Page`` domain.
        """
        script_source = build_platform_override_script(self.config)
        await session.send("Page.enable", {}, session_id=session_id)
        await session.send(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": script_source},
            session_id=session_id,
        )
        await session.send(
            "Runtime.evaluate",
            {"expression": script_source},
            session_id=session_id,
        )
