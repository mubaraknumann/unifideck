"""auth.edge_browser.edge — EdgeBrowser façade class.

Microsoft Edge lifecycle for auth + xCloud. Shared infrastructure
for Unifideck's browser-based OAuth flows (Epic, GOG, Amazon,
Microsoft all launch the same Edge instance to sign in) and for
Microsoft's xCloud game streaming.

Three internal helpers are composed at construction time:

  - ``EdgeInstaller``       : flatpak install + detection + udev
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

import logging
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import launch as _launch
from . import process_ops
from .cdp_client import EdgeCDPClient
from .env import clean_env
from .installer import EdgeInstaller
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
_XCLOUD_CDP_OFFSET = 1
_STOREFRONT_CDP_OFFSET = 2

# Edge flags shared between auth and game launch
_BASE_FLAGS = [
    "--no-first-run",
    "--disable-translate",
    "--disable-infobars",
    "--disable-session-crashed-bubble",
    "--disable-features=TranslateUI",
    "--password-store=basic",
    "--disable-dev-shm-usage",
    "--disable-background-networking",
]


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
    """Manages the Microsoft Edge browser for auth and xCloud.

    One instance per plugin — the store coordinator (any of the four
    OAuth stores) injects a shared reference. The class is 100%
    Edge-specific: the flatpak app id, the native binary names, and
    the browser flags are all Edge's.

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
        # Composed installer — owns detection + flatpak install
        # + default-browser snapshot + controller permissions.
        # Extracted from this class to keep each concern cohesive.
        self._installer = EdgeInstaller(clean_env_fn=clean_env)
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

    def _list_cdp_targets(self) -> list[dict[str, Any]]:
        """Delegate to EdgeCDPClient."""
        return self._cdp.list_targets()

    # ── Per-flavour CDP ports ────────────────────────────────────────

    def xcloud_cdp_port(self) -> int:
        """The CDP port an xCloud kiosk window listens on."""
        return self.cdp_port + _XCLOUD_CDP_OFFSET

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

    # ── Controller permissions ───────────────────────────────────────

    @staticmethod
    def ensure_controller_permissions() -> bool:
        """Delegate to EdgeInstaller.ensure_controller_permissions.

        Kept as a @staticmethod for API compatibility: callers that
        invoke this without an EdgeBrowser instance still work by
        instantiating a one-shot installer here.
        """
        return EdgeInstaller(
            clean_env_fn=clean_env,
        ).ensure_controller_permissions()

    # ── Detection & install (delegated to EdgeInstaller) ─────────────

    def _flatpak_remote_names(self, scope: str) -> set[str]:
        """Delegate to EdgeInstaller."""
        return self._installer._flatpak_remote_names(scope)

    async def _ensure_user_flathub_remote(self) -> bool:
        """Delegate to EdgeInstaller."""
        return await self._installer._ensure_user_flathub_remote()

    def find_cmd(self) -> list[str] | None:
        """Delegate to EdgeInstaller."""
        return self._installer.find_cmd()

    @property
    def is_installed(self) -> bool:
        """Delegate to EdgeInstaller."""
        return self._installer.is_installed

    @staticmethod
    def _get_default_browser() -> str | None:
        """Delegate to EdgeInstaller (instantiates one-shot installer)."""
        return EdgeInstaller(clean_env_fn=clean_env)._get_default_browser()

    @staticmethod
    def _restore_default_browser(original: str | None) -> None:
        """Delegate to EdgeInstaller (instantiates one-shot installer)."""
        EdgeInstaller(
            clean_env_fn=clean_env,
        )._restore_default_browser(original)

    async def install(self) -> dict[str, Any]:
        """Delegate to EdgeInstaller."""
        return await self._installer.install()

    # ── Launch / kill ────────────────────────────────────────────────

    def launch_auth(self, auth_url: str) -> bool:
        """Launch the auth browser for OAuth — delegate to launch module."""
        return _launch.launch_auth(self, auth_url)

    def launch_storefront(self, url: str) -> bool:
        """Launch a browsable store window — delegate to launch module."""
        return _launch.launch_storefront(self, url)

    def launch_xcloud(self, xcloud_url: str) -> bool:
        """Launch Edge in kiosk mode — delegate to launch module."""
        return _launch.launch_xcloud(self, xcloud_url)

    def kill(self) -> None:
        """Gracefully terminate the auth browser process.

        Thin forward to ``process_ops.graceful_kill``; the
        lifecycle logic lives in its own module for testability.
        After the kill returns, the process handle is cleared
        and any stale profile state is cleaned up so the next
        launch starts from a known-good baseline.
        """
        process_ops.graceful_kill(self.process)
        self.process = None
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
