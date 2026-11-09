"""auth.edge_browser.edge — EdgeBrowser façade class.

Microsoft Edge lifecycle for auth + xCloud. Shared infrastructure
for Unifideck's browser-based OAuth flows (Epic, GOG, Amazon,
Microsoft all launch the same Edge instance to sign in) and for
Microsoft's xCloud game streaming.

Three internal helpers are composed at construction time:

  - ``ChromeInstaller``     : download + verify the plugin's own browser
  - ``EdgeCDPClient``       : /json/list, navigate, close targets
  - ``EdgeProfileManager``  : profile dir, singleton, cookies

Plus two sibling modules used as function collections:

  - ``env.clean_env()``              : scrubbed subprocess env
  - ``launch.launch_auth/xcloud``    : subprocess spawn helpers

Historically this file was named microsoft_chromium.py and its
class was ChromiumBrowser because earlier versions targeted a
generic Chromium-based browser. The current implementation is
100% Edge-specific (it's the only browser with native Steam Deck
controller support in xCloud), so the names have been updated
for truthfulness.

Because this file is shared across all OAuth stores, not just
Microsoft, it lives under auth/ rather than stores/microsoft/.
The shell launcher's _generic_auth_handler, its Microsoft auth
branch, and its xCloud kiosk branch all point at the same
edge-auth profile directory managed by this module.

Usage::

    from unifideck.auth.edge_browser import EdgeBrowser
    browser = EdgeBrowser(cdp_port=9222, locale_fn=get_locale)
    if not browser.is_installed:
        await browser.install()
    browser.launch_auth(oauth_url)

Reference: Technical Document v1.0 — Section 3.1.5
(Infrastructure services) and Section 8 (Security).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import launch as _launch
from . import process_ops
from .cdp_client import EdgeCDPClient, close_browser_blocking
from .chrome_installer import NETWORK_FAILURE, ChromeInstaller
from .profile import EdgeProfileManager

logger = logging.getLogger(__name__)

# ── Profile paths ───────────────────────────────────────────────────────
# New canonical locations. The module renames the legacy
# chromium-auth directory on first construction if it finds it,
# so existing users don't lose their OAuth cookies during the
# rename migration.
PROFILE_DIR = str(Path(
    "~/.local/share/unifideck/edge-auth",
).expanduser())
LOG_FILE = str(Path(
    "~/.local/share/unifideck/edge-auth.log",
).expanduser())

#: HTML5 web games (itch.io) get their own profile. Their pages and scripts
#: are written by game creators, so they must not run where the Microsoft /
#: Xbox, itch.io and store sign-in sessions live. Only xCloud streams, which
#: need the Xbox session cookies, open in ``PROFILE_DIR``.
WEB_GAME_PROFILE_DIR = str(Path(
    "~/.local/share/unifideck/edge-webgames",
).expanduser())

# Legacy paths, used only for the one-shot migration. The
# _migrate_legacy_profile() call moves the old directory into the
# new name at first use and logs the action. After migration
# these constants serve no purpose and are only kept as a
# reference for the next few release cycles.
_LEGACY_PROFILE_DIR = str(Path(
    "~/.local/share/unifideck/chromium-auth",
).expanduser())
_LEGACY_LOG_FILE = str(Path(
    "~/.local/share/unifideck/chromium-auth.log",
).expanduser())

# Domains whose cookies are cleared on logout
_MS_COOKIE_DOMAINS = (
    "%xbox.com%", "%microsoft.com%", "%live.com%", "%microsoftonline.com%",
)

# CDP port offsets from ``cdp_port``, one per window flavour. Chromium
# refuses a second process on the same ``--user-data-dir`` (it hands the
# URL to the running instance over SingletonSocket and exits), and all
# three flavours share one profile by design — so the port is what tells
# a caller WHICH window is currently alive before it tries to spawn.
_BROWSER_GAME_CDP_OFFSET = 1
#: Waits between the boot-time download attempts (seconds).
_BACKGROUND_INSTALL_RETRY_S = (15, 30, 60, 120)
_STOREFRONT_CDP_OFFSET = 2

# Edge flags shared between auth and game launch
_BASE_FLAGS = [
    # X11 always (XWayland): with a Wayland display in its environment Chrome
    # opens a native Wayland window, which gamescope cannot tie to the Steam
    # app and the window tagger cannot see, so it stays hidden behind Gaming
    # Mode (measured 2026-11-04). The Edge flatpak only ever ran on X11.
    "--ozone-platform=x11",
    "--no-first-run",
    "--disable-translate",
    "--disable-infobars",
    "--disable-session-crashed-bubble",
    "--disable-features=TranslateUI",
    "--password-store=basic",
    "--disable-dev-shm-usage",
    "--disable-background-networking",
]


def _make_web_game_profile_manager() -> EdgeProfileManager:
    """An EdgeProfileManager for the isolated web-game profile.

    Only its singleton cleanup is used: there is no legacy directory to
    migrate and no cookie set that logout has to clear.
    """
    return EdgeProfileManager(
        profile_dir=WEB_GAME_PROFILE_DIR,
        log_file=LOG_FILE,
        legacy_profile_dir=WEB_GAME_PROFILE_DIR,
        legacy_log_file=LOG_FILE,
        cookie_domain_patterns=(),
    )


def _make_profile_manager() -> EdgeProfileManager:
    """Build an EdgeProfileManager bound to the canonical path constants.

    Used by EdgeBrowser's @staticmethod delegations (has_xbox_session,
    clear_cookies, clear_profile_data) which have no access to a
    self._profile instance. The factory wires the module-level path
    constants into a freshly built EdgeProfileManager.
    """
    return EdgeProfileManager(
        profile_dir=PROFILE_DIR,
        log_file=LOG_FILE,
        legacy_profile_dir=_LEGACY_PROFILE_DIR,
        legacy_log_file=_LEGACY_LOG_FILE,
        cookie_domain_patterns=_MS_COOKIE_DOMAINS,
    )


class EdgeBrowser:
    """Manages the plugin's browser for sign-ins, stores and browser games.

    One instance per plugin — the store coordinator (any of the four
    OAuth stores) injects a shared reference. The browser is the
    plugin's own Chromium build (``chrome_installer``); the class kept its
    name from the Edge flatpak it replaced.

    Attributes:
      cdp_port: CDP remote debugging port. Auth flows use 9222;
        xCloud kiosk mode uses 9223 via port+1.
      locale_fn: Callable returning the BCP-47 locale string.
        Used to pass --lang to the browser.
      process: The subprocess.Popen[bytes] handle, or None when the browser
        isn't running.

    """

    def __init__(
        self,
        cdp_port: int = 9222,
        locale_fn: Callable[[], str] | None = None,
    ):
        self.cdp_port = cdp_port
        self.locale_fn = locale_fn or (lambda: "en-US")
        self.process: subprocess.Popen[bytes] | None = None
        #: CDP port of ``process`` (set at spawn), for a clean ``kill``.
        self.process_cdp_port: int | None = None
        self._install_task: asyncio.Task[None] | None = None
        # Composed installer — the plugin's own Chromium build
        # (``chrome_installer``), which replaced the Edge flatpak.
        self._installer = ChromeInstaller()
        # Composed CDP client — owns /json/version, /json/list,
        # navigation and target-close traffic over the debugging port.
        self._cdp = EdgeCDPClient(cdp_port=cdp_port)
        # Composed profile manager — owns legacy migration,
        # singleton lock cleanup, cookie inspection, and profile wipe.
        self._profile = _make_profile_manager()
        # Run the one-shot profile directory migration on first
        # instantiation. Cheap (stat + maybe one rename) and
        # idempotent — subsequent constructions are no-ops.
        self._migrate_legacy_profile()

    # ── Profile (delegated to EdgeProfileManager) ────────────────────

    def _migrate_legacy_profile(self) -> None:
        """Delegate to EdgeProfileManager."""
        self._profile.migrate_legacy_profile()

    def is_running(self) -> bool:
        """Return True when the auth browser instance is still alive."""
        if self.process is not None and self.process.poll() is None:
            return True
        return self._get_browser_ws_url() is not None

    def _singleton_paths(self) -> list[str]:
        """Delegate to EdgeProfileManager."""
        return self._profile._singleton_paths()

    def _has_stale_singleton_socket(self) -> bool:
        """Delegate to EdgeProfileManager."""
        return self._profile._has_stale_singleton_socket()

    def cleanup_stale_profile_state(self) -> None:
        """Delegate to EdgeProfileManager."""
        self._profile.cleanup_stale_state()

    # ── CDP (delegated to EdgeCDPClient) ─────────────────────────────

    def _get_browser_ws_url(self) -> str | None:
        """Delegate to EdgeCDPClient."""
        return self._cdp.get_browser_ws_url()

    def list_targets(self) -> list[dict[str, Any]]:
        """The auth window's CDP targets (delegates to EdgeCDPClient)."""
        return self._cdp.list_targets()

    # ── Per-flavour CDP ports ────────────────────────────────────────

    def browser_game_cdp_port(self) -> int:
        """The CDP port a browser-game kiosk window (xCloud, HTML5) listens on."""
        return self.cdp_port + _BROWSER_GAME_CDP_OFFSET

    def storefront_cdp_port(self) -> int:
        """The CDP port a storefront window listens on."""
        return self.cdp_port + _STOREFRONT_CDP_OFFSET

    @staticmethod
    def cdp_alive(port: int) -> bool:
        """True when *some* Edge is answering CDP on ``port``.

        ``is_running()`` cannot answer this: it reports on this
        instance's own ``self.process`` plus the auth port only, and
        the process handle is ``None`` in a freshly bootstrapped
        launcher subprocess. This probes a specific port, so it sees a
        window spawned by a different process — which is exactly the
        collision the storefront flow has to detect.
        """
        return EdgeCDPClient(cdp_port=port).probe_cdp()

    @staticmethod
    async def navigate_on_port(port: int, url: str) -> bool:
        """Point the Edge answering on ``port`` at ``url``."""
        return await EdgeCDPClient(cdp_port=port).navigate_tab(url)

    @staticmethod
    async def close_targets_on_port(port: int, *, log_prefix: str) -> bool:
        """Close every CDP target on ``port``."""
        return await EdgeCDPClient(cdp_port=port).close_all_targets(
            log_prefix=log_prefix,
        )

    async def navigate_tab(
        self,
        url: str,
        timeout: float = 15.0,  # noqa: ASYNC109 — timeout is API value passed to underlying lib (urllib/aiohttp/subprocess), not an asyncio.timeout() wrapper
    ) -> bool:
        """Delegate to EdgeCDPClient."""
        return await self._cdp.navigate_tab(url, timeout=timeout)

    async def _close_all_cdp_targets(
        self, *, log_prefix: str,
    ) -> bool:
        """Delegate to EdgeCDPClient."""
        return await self._cdp.close_all_targets(log_prefix=log_prefix)

    async def prepare_auth_launch(self) -> None:
        """Close any lingering CDP auth browser and clear broken lock files."""
        await self._close_all_cdp_targets(log_prefix="lingering auth")
        self.cleanup_stale_profile_state()

    async def close_auth_browser(self) -> bool:
        """Close the live Microsoft auth browser after OAuth succeeds."""
        closed = await self._close_all_cdp_targets(log_prefix="auth")
        if closed:
            self.cleanup_stale_profile_state()
        return closed

    # ── Detection & install (delegated to ChromeInstaller) ───────────

    def find_cmd(self) -> list[str] | None:
        """The browser command, or None until it is installed."""
        return self._installer.find_cmd()

    @property
    def is_installed(self) -> bool:
        """True once the plugin's browser is downloaded and verified."""
        return self._installer.is_installed

    async def install(self) -> dict[str, Any]:
        """Download and verify the plugin's browser."""
        return await self._installer.install()

    async def start(self) -> None:
        """Startup hook: fetch the browser in the background when it is missing.

        Upgraders from the Edge flatpak have no browser yet; without this their
        first stream would stop on "browser required", since only a store
        sign-in offered the download. Non-blocking, like the GE-Proton fetch.
        """
        if self.is_installed:
            return
        self._install_task = asyncio.create_task(self._background_install(),
                                                 name="browser-install")

    async def _background_install(self) -> None:
        # The plugin starts before the network is up on a cold boot, so the
        # first attempt can fail on name resolution: retry with backoff.
        # Only a network failure is retried: a bad checksum or a build that
        # does not run would fail the same way again.
        result: dict[str, Any] = {}
        for delay in _BACKGROUND_INSTALL_RETRY_S:
            result = await self.install()
            if result.get("success") or self.is_installed:
                return
            if result.get("error") != NETWORK_FAILURE:
                break
            logger.info("[Browser] background download failed (%s); retrying in %d s",
                        result.get("error"), delay)
            await asyncio.sleep(delay)
        else:
            result = await self.install()
        if not result.get("success"):
            logger.warning("[Browser] background download failed (%s); sign-ins, store "
                           "windows and streams offer it again", result.get("error"))

    # ── Launch / kill ────────────────────────────────────────────────

    def launch_auth(self, auth_url: str) -> bool:
        """Launch the auth browser for OAuth — delegate to launch module."""
        return _launch.launch_auth(self, auth_url)

    def launch_storefront(self, url: str) -> bool:
        """Launch a browsable store window — delegate to launch module."""
        return _launch.launch_storefront(self, url)

    def launch_browser_game(self, url: str, *, kind: str) -> bool:
        """Launch Edge in kiosk mode on a browser game: delegate to launch module."""
        return _launch.launch_browser_game(self, url, kind=kind)

    def kill(self) -> None:
        """Close the browser this instance launched, cleanly when it can.

        ``Browser.close`` on the window's own CDP port first, because a
        signal loses the cookies of a sign-in made in the last ~30 s; only
        then ``process_ops.graceful_kill`` (SIGTERM, then SIGKILL). After it
        returns, the process handle is cleared and stale profile state is
        cleaned up so the next launch starts from a known-good baseline.
        """
        proc = self.process
        if proc is not None and proc.poll() is None and self.process_cdp_port is not None:
            # Browser.close first: a signal loses the cookies of a sign-in
            # made in the last ~30 s (``cdp_client.close_browser``).
            if close_browser_blocking(self.process_cdp_port):
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=10)
        if proc is not None and proc.poll() is None:
            process_ops.graceful_kill(proc)
        self.process = None
        self.process_cdp_port = None
        self.cleanup_stale_profile_state()

    # ── Cookie management (static, via one-shot profile manager) ─────

    @staticmethod
    def has_xbox_session() -> bool:
        """Delegate to EdgeProfileManager (one-shot instance)."""
        return _make_profile_manager().has_xbox_session()

    @staticmethod
    def clear_cookies() -> None:
        """Delegate to EdgeProfileManager (one-shot instance)."""
        _make_profile_manager().clear_cookies()

    @staticmethod
    def clear_profile_data() -> None:
        """Delegate to EdgeProfileManager (one-shot instance)."""
        _make_profile_manager().clear_profile_data()

    def clear_store_cookies(self, domain: str) -> None:
        """Clear cookies for `domain` from the shared Edge profile.

        Instance method so the profile directory bound at
        construction is reused — no one-shot factory call.
        Called before each OAuth flow so stale sessions don't
        bypass the login form.
        """
        self._profile.clear_cookies_for_domain(domain)

    # ── CDP helpers ──────────────────────────────────────────────────

    async def wait_and_check_crash(self) -> bool:
        """Wait for the auth browser to start, return False if it crashed.

        Thin forward to ``process_ops.wait_and_check_crash``; the
        polling logic lives in its own module for testability.
        Injects ``self._cdp.probe_cdp`` as the readiness probe
        so the shared helper stays CDP-client-agnostic. Clears
        ``self.process`` on a detected crash so subsequent
        methods see the browser as stopped.
        """
        result = await process_ops.wait_and_check_crash(
            self.process, self._cdp.probe_cdp, LOG_FILE,
        )
        if not result:
            self.process = None
        return result
