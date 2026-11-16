"""LunaPlayActivator: CDP play-trigger that clicks the title's play action,
reaches the shared ``/player`` URL, and confirms the ``<video>`` is playing.

Scaffolding stub — implemented by a later amazon-luna-streaming task. New;
no xCloud analogue (Luna has no per-title deep link).

Implemented for task 7.3 (Req 5.3, 5.4, 5.5, 11.3). After the detail URL
has loaded, :meth:`LunaPlayActivator.activate` triggers the title's play
action over CDP within 15s, then confirms within 30s that the browser
reached the shared ``/player`` URL and the ``<video>`` element is playing
(``readyState === 4 && !paused`` — the verified F7 state). Failure to reach
``/player`` in time yields an :class:`ActivationResult` whose reason
identifies the play-activation step so the launch flow (task 7.4) can map it
to the ``launch_failure`` state and record it in the launcher log; a
structural CDP failure propagates as an exception instead.

Design mirrors :mod:`unifideck.stores.luna.luna_platform_spoofer`:

* It is constructible from :class:`LunaConfig` **alone** (no
  :class:`LunaStore` dependency) so it runs in the launcher subprocess.
* The one I/O seam is the same :class:`BrowserCdpSession` abstraction the
  spoofer uses (reused here, not re-implemented), so ``activate`` resolves
  the browser websocket on ``browser_game_cdp_port()``, attaches to the page
  target in CDP flatten mode, and drives ``Runtime.evaluate`` with
  ``awaitPromise`` / ``returnByValue`` — the identical pattern.
* The **play-trigger** (locating and clicking the Luna play control on the
  detail page) is the Q2-swappable part. It is isolated behind the
  :class:`PlayTrigger` protocol and defaults to :class:`ClickPlayTrigger`
  (a ``Runtime.evaluate`` that finds and clicks the play control). A future
  per-title deep link can replace it by injecting a different strategy
  without touching the confirmation logic.
* The timed poll is driven by a module-level ``_monotonic`` clock and
  ``asyncio.sleep`` (the same injectable-clock pattern as
  ``microsoft_device_auth.py``), and all CDP I/O goes through the mockable
  :class:`BrowserCdpSession`, so :meth:`run_activate` is drivable against a
  mock session in tests without a browser.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlsplit

from unifideck.stores.luna.luna_cdp import attach_to_page_target
from unifideck.stores.luna.luna_platform_spoofer import (
    BrowserCdpSession,
    _AiohttpBrowserCdpSession,
)

if TYPE_CHECKING:
    from unifideck.auth.edge_browser.cdp_client import EdgeCDPClient
    from unifideck.stores.luna.luna_config import LunaConfig

logger = logging.getLogger(__name__)

#: The play action must be triggered within 15s of the detail page loading
#: (Req 5.3).
_PLAY_TRIGGER_TIMEOUT_S = 15.0

#: After triggering play, the browser must reach ``/player`` and the video
#: must be playing within 30s (Req 5.4, 5.5).
_PLAYER_CONFIRM_TIMEOUT_S = 30.0

#: How often the confirmation loop re-checks the player state. Small enough
#: to confirm promptly once the stream starts, large enough not to spin.
_CONFIRM_POLL_INTERVAL_S = 0.5

#: The name of the step reported on failure so the launch flow can map it to
#: ``launch_failure`` and the launcher log can identify where it broke
#: (Req 5.5, 11.3).
PLAY_ACTIVATION_STEP = "play-activation"

#: The poll's clock, exposed as a module attribute so tests can move time
#: without touching the event loop's own ``time.monotonic`` (mirrors
#: ``microsoft_device_auth.py``).
_monotonic = time.monotonic

#: JS evaluated to trigger the title's play action on the loaded detail
#: page: find the Luna play control and click it. Returns ``true`` when a
#: control was found and clicked, ``false`` otherwise, so the activator can
#: retry within the 15s window until the control is present. This is the
#: Q2-swappable seam (see :class:`ClickPlayTrigger`).
_PLAY_CLICK_EXPRESSION = r"""
(() => {
  const byTestId = document.querySelector(
    '[data-testid*="play" i], [data-test-id*="play" i]'
  );
  const candidates = byTestId
    ? [byTestId]
    : Array.from(document.querySelectorAll('button, a, [role="button"]'));
  const control = byTestId
    ? byTestId
    : candidates.find((el) => {
        const label = (
          (el.getAttribute && (
            el.getAttribute('aria-label') ||
            el.getAttribute('title') || '')) + ' ' + (el.textContent || '')
        ).trim().toLowerCase();
        return label.includes('play') || label.includes('spielen');
      });
  if (!control) {
    return false;
  }
  control.click();
  return true;
})()
""".strip()

#: JS evaluated to confirm the player is up: the location is on the shared
#: ``/player`` path **and** a ``<video>`` is playing (``readyState === 4 &&
#: !paused`` — the verified F7 state, Req 5.4). Returns an object so the
#: activator can distinguish "not on /player yet" from "on /player, video
#: not ready yet". ``player_path`` is substituted in from the config.
_PLAYER_CONFIRM_EXPRESSION_TEMPLATE = r"""
(() => {
  const onPlayer = window.location.pathname.indexOf(%(player_path)s) !== -1;
  const video = document.querySelector('video');
  const playing = !!video && video.readyState === 4 && !video.paused;
  return { onPlayer, playing };
})()
""".strip()


class LunaPlayActivationError(Exception):
    """A structural CDP failure that prevents play-activation entirely.

    Raised (not returned) when the activator cannot even run — no page
    target, no browser websocket, or an unreadable CDP evaluate result —
    so the launch flow (task 7.4) can tell "the title did not start"
    (an :class:`ActivationResult` failure, the normal launch-failure case)
    apart from "the activation machinery itself broke" (this exception).
    """


@dataclass(frozen=True)
class ActivationResult:
    """The outcome of a play-activation attempt.

    ``success`` is true only when the browser reached ``/player`` and the
    ``<video>`` was confirmed playing within the timeouts. On failure,
    ``step`` names the stage that did not complete (always
    :data:`PLAY_ACTIVATION_STEP` for the normal launch-failure case) and
    ``reason`` is a human-readable description for the launcher log
    (Req 5.5, 11.3). ``reached_player`` records whether the browser got to
    ``/player`` at all, to aid the log.
    """

    success: bool
    step: str = ""
    reason: str = ""
    reached_player: bool = False

    @classmethod
    def ok(cls) -> ActivationResult:
        """Build the success result (reached ``/player``, video playing)."""
        return cls(success=True, reached_player=True)

    @classmethod
    def failed(
        cls, reason: str, *, reached_player: bool = False,
    ) -> ActivationResult:
        """Build a launch-failure result naming the play-activation step."""
        return cls(
            success=False,
            step=PLAY_ACTIVATION_STEP,
            reason=reason,
            reached_player=reached_player,
        )


@dataclass(frozen=True)
class PlayerState:
    """The player state read from the browser in one confirmation probe."""

    on_player: bool
    playing: bool


class PlayTrigger(Protocol):
    """The Q2-swappable play-trigger strategy.

    Isolates *how* play is initiated from the activator's confirmation
    logic. The default :class:`ClickPlayTrigger` clicks the play control on
    the detail page; a future per-title deep link can be dropped in as an
    alternative implementation (e.g. a ``Page.navigate`` to a resolved
    player URL) without changing :meth:`LunaPlayActivator.run_activate`.
    """

    async def trigger(
        self,
        session: BrowserCdpSession,
        session_id: str,
        *,
        detail_url: str,
        player_url: str,
    ) -> bool:
        """Attempt to start the title; return ``True`` once initiated."""
        ...


class ClickPlayTrigger:
    """Default :class:`PlayTrigger`: click the play control on the detail page.

    Uses a single ``Runtime.evaluate`` (:data:`_PLAY_CLICK_EXPRESSION`) that
    locates and clicks the Luna play control. Returns the boolean the script
    reports: ``True`` when a control was found and clicked, ``False`` when
    the control is not present yet (so the activator keeps retrying within
    the 15s window).
    """

    async def trigger(
        self,
        session: BrowserCdpSession,
        session_id: str,
        *,
        detail_url: str,
        player_url: str,
    ) -> bool:
        """Click the play control; return whether a control was clicked."""
        result = await session.send(
            "Runtime.evaluate",
            {
                "expression": _PLAY_CLICK_EXPRESSION,
                "returnByValue": True,
            },
            session_id=session_id,
        )
        return _extract_bool(result)


def _extract_bool(result: dict[str, Any]) -> bool:
    """Extract a boolean value from a ``Runtime.evaluate`` result.

    ``result`` has the shape ``{"result": {"type": "boolean", "value":
    true}}``. A thrown evaluation carries ``exceptionDetails`` instead,
    treated here as a (recoverable) ``False`` so a transient script error
    on the still-loading detail page just prompts another retry rather than
    aborting the launch. Pure and deterministic.
    """
    if result.get("exceptionDetails"):
        return False
    inner = result.get("result")
    if not isinstance(inner, dict):
        return False
    return bool(inner.get("value"))


def build_player_confirm_expression(player_path: str) -> str:
    """Build the player-confirmation JS, embedding the configured path.

    Substitutes ``config.player_path`` (e.g. ``"/player"``) into
    :data:`_PLAYER_CONFIRM_EXPRESSION_TEMPLATE` via ``repr`` so the path is
    safely quoted. Pure given its input, so the confirmation script is
    unit-testable without a browser.
    """
    # repr() yields a safely single-quoted JS string literal for the path.
    return _PLAYER_CONFIRM_EXPRESSION_TEMPLATE % {"player_path": repr(player_path)}


def extract_player_state(result: dict[str, Any]) -> PlayerState:
    """Extract the :class:`PlayerState` from a confirmation evaluate result.

    ``result`` is the payload from evaluating
    :func:`build_player_confirm_expression` with ``returnByValue``; its
    shape is ``{"result": {"value": {"onPlayer": bool, "playing": bool}}}``.
    A thrown evaluation (``exceptionDetails``) or a missing/malformed value
    is treated as "not on player, not playing" so the poll keeps trying
    rather than aborting — the detail/player transition legitimately throws
    transiently while the page swaps. Pure and deterministic.
    """
    if result.get("exceptionDetails"):
        return PlayerState(on_player=False, playing=False)
    inner = result.get("result")
    value = inner.get("value") if isinstance(inner, dict) else None
    if not isinstance(value, dict):
        return PlayerState(on_player=False, playing=False)
    return PlayerState(
        on_player=bool(value.get("onPlayer")),
        playing=bool(value.get("playing")),
    )


def _player_path_from_url(player_url: str) -> str:
    """Return the path component of the Luna_Player_URL (e.g. ``/player``).

    The confirmation script checks ``window.location.pathname`` against this
    path, so only the path portion of the configured player URL is relevant.
    An input that is already a bare path is returned unchanged.
    """
    path = urlsplit(player_url).path
    return path or player_url


class LunaPlayActivator:
    """Trigger a Luna title's play action and confirm the stream started.

    Constructed from :class:`LunaConfig` alone (no :class:`LunaStore`
    dependency) so it runs in the launcher subprocess. :meth:`activate`
    resolves the browser websocket on ``browser_game_cdp_port()`` (via the
    reused :class:`BrowserCdpSession` abstraction from the spoofer), attaches
    to the loaded detail-page target, and delegates to :meth:`run_activate`.

    :meth:`run_activate` is the testable core: given a mock
    :class:`BrowserCdpSession`, it (1) triggers play via the injected
    :class:`PlayTrigger` within 15s (Req 5.3), then (2) polls until the
    browser reaches ``/player`` with the ``<video>`` playing within 30s
    (Req 5.4). Timeouts return an :class:`ActivationResult` failure naming
    the play-activation step (Req 5.5); a structural CDP failure raises
    :class:`LunaPlayActivationError`.
    """

    def __init__(
        self, config: LunaConfig, *, play_trigger: PlayTrigger | None = None,
    ) -> None:
        """Build the activator bound to a resolved :class:`LunaConfig`.

        ``play_trigger`` is the Q2-swappable strategy; it defaults to
        :class:`ClickPlayTrigger` (click the detail-page play control) and
        can be replaced by a deep-link strategy later.
        """
        self.config = config
        self._play_trigger: PlayTrigger = play_trigger or ClickPlayTrigger()

    async def activate(
        self, cdp: EdgeCDPClient, detail_url: str, player_url: str,
    ) -> ActivationResult:
        """Trigger play and confirm the stream, over a live CDP client.

        Resolves the browser websocket on ``browser_game_cdp_port()``, opens
        a :class:`BrowserCdpSession`, attaches to the loaded page target, and
        drives :meth:`run_activate`. Returns its :class:`ActivationResult`
        (the normal success / launch-failure case); a structural CDP failure
        propagates as :class:`LunaPlayActivationError`.
        """
        ws_url = cdp.get_browser_ws_url()
        if not ws_url:
            raise LunaPlayActivationError(
                "cannot activate play: no browser websocket URL on the "
                "browser-game CDP port",
            )
        session = _AiohttpBrowserCdpSession(ws_url)
        try:
            session_id = await self._attach_to_page_target(session)
            return await self.run_activate(
                session, session_id, detail_url, player_url,
            )
        finally:
            await session.close()

    async def run_activate(
        self,
        session: BrowserCdpSession,
        session_id: str,
        detail_url: str,
        player_url: str,
    ) -> ActivationResult:
        """Drive trigger + confirm over an arbitrary ``session`` (testable core).

        Separated from :meth:`activate` so it can be driven against a mock
        :class:`BrowserCdpSession`. Phase 1 triggers play within 15s
        (Req 5.3) via the injected :class:`PlayTrigger`; phase 2 polls until
        ``/player`` is reached and the ``<video>`` is playing within 30s
        (Req 5.4). On the 30s timeout it returns a launch-failure result
        naming the play-activation step (Req 5.5).
        """
        if not await self._trigger_play(session, session_id, detail_url, player_url):
            logger.warning(
                "[Luna] play activation failed: could not trigger the play "
                "control within %.0fs", _PLAY_TRIGGER_TIMEOUT_S,
            )
            return ActivationResult.failed(
                "could not trigger the title's play action within "
                f"{_PLAY_TRIGGER_TIMEOUT_S:.0f}s on {detail_url}",
            )
        return await self._confirm_player(session, session_id, player_url)

    async def _trigger_play(
        self,
        session: BrowserCdpSession,
        session_id: str,
        detail_url: str,
        player_url: str,
    ) -> bool:
        """Trigger play within the 15s window (Req 5.3).

        Retries the injected :class:`PlayTrigger` until it reports the action
        was initiated or the window elapses — the play control may not be in
        the DOM the instant the detail page's ``load`` fires. Returns whether
        play was triggered.
        """
        deadline = _monotonic() + _PLAY_TRIGGER_TIMEOUT_S
        while True:
            if await self._play_trigger.trigger(
                session, session_id,
                detail_url=detail_url, player_url=player_url,
            ):
                logger.info("[Luna] play action triggered on %s", detail_url)
                return True
            if _monotonic() >= deadline:
                return False
            await asyncio.sleep(_CONFIRM_POLL_INTERVAL_S)

    async def _confirm_player(
        self, session: BrowserCdpSession, session_id: str, player_url: str,
    ) -> ActivationResult:
        """Confirm ``/player`` + video playing within 30s (Req 5.4, 5.5).

        Polls the player-confirmation probe until the browser is on
        ``/player`` and the ``<video>`` is playing (``readyState === 4 &&
        !paused``), or the 30s window elapses. On timeout returns a
        launch-failure result naming the play-activation step and whether
        ``/player`` was ever reached (Req 5.5).
        """
        expression = build_player_confirm_expression(
            _player_path_from_url(player_url or self.config.player_path),
        )
        deadline = _monotonic() + _PLAYER_CONFIRM_TIMEOUT_S
        reached_player = False
        while True:
            state = await self._probe_player(session, session_id, expression)
            reached_player = reached_player or state.on_player
            if state.on_player and state.playing:
                logger.info(
                    "[Luna] play activation confirmed: reached player and "
                    "video is playing",
                )
                return ActivationResult.ok()
            if _monotonic() >= deadline:
                reason = (
                    "reached the player URL but the video did not start "
                    if reached_player
                    else "did not reach the player URL "
                ) + f"within {_PLAYER_CONFIRM_TIMEOUT_S:.0f}s of triggering play"
                logger.warning("[Luna] play activation failed: %s", reason)
                return ActivationResult.failed(
                    reason, reached_player=reached_player,
                )
            await asyncio.sleep(_CONFIRM_POLL_INTERVAL_S)

    async def _probe_player(
        self, session: BrowserCdpSession, session_id: str, expression: str,
    ) -> PlayerState:
        """Run one player-confirmation evaluate and extract the state."""
        result = await session.send(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True},
            session_id=session_id,
        )
        return extract_player_state(result)

    async def _attach_to_page_target(self, session: BrowserCdpSession) -> str:
        """Return a CDP session id attached to the active Luna page target.

        Mirrors :meth:`LunaPlatformSpoofer._attach_to_page_target`: asks the
        browser for its targets and attaches (flatten mode) to the first
        ``page`` target — the loaded Luna detail page the play-trigger and
        confirmation must run against. Raises :class:`LunaPlayActivationError`
        when no page target exists (a structural failure, not a launch
        failure).

        Delegates the shared CDP page-target-attach step to
        :func:`unifideck.stores.luna.luna_cdp.attach_to_page_target`.
        """
        return await attach_to_page_target(
            session,
            raise_no_target=lambda: LunaPlayActivationError(
                "cannot activate play: no page target found on the "
                "browser-game CDP port",
            ),
            raise_no_session=lambda: LunaPlayActivationError(
                "cannot activate play: failed to attach to the Luna page "
                "target",
            ),
        )
