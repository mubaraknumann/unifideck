"""LunaBrowser: a thin configuration object over the reused ``EdgeBrowser``
process layer.

This is **not** a reimplementation of ``EdgeBrowser``. It composes the
existing browser-game kiosk launch path (``auth/edge_browser/launch.py``),
``_spawn_edge_process`` (process-group kill via ``start_new_session=True``)
and ``clean_env()`` unchanged, and only *parameterises* them with
Luna-specific constants drawn from :class:`LunaConfig`:

- a **dedicated Luna browser-game profile dir** (keeps Luna's spoofed
  Amazon session isolated from other browser-game cookies; a config choice
  defaulting to ``WEB_GAME_PROFILE_DIR`` only when ``luna.profile_dir`` is
  unset — Req 12.8), resolved through
  :func:`luna_profile.resolve_profile_dir`;
- the browser-game **CDP flavour**, resolving to
  ``EdgeBrowser.browser_game_cdp_port()`` (an offset from ``cdp_port``,
  **not** a bespoke ``9224``) so the spoofer / play-activator / keyboard
  attach on the same port the kiosk window listens on;
- **kiosk + gamepad flags**, ``--autoplay-policy=no-user-gesture-required``
  and ``--lang`` (Req 5.7, 5.8, 6.1).

The browser binary is whatever ``EdgeBrowser`` resolves: the plugin's own
downloaded Chromium (see ``auth/edge_browser/chrome_installer.py``). It is a
native binary, so no sandbox filesystem override is needed for the profile
directory.

The spawn/kill/env mechanics are **reused, not copied**:
:func:`launch` builds the Luna arg suffix and hands off to the shared
``launch._spawn_edge_process``, exactly as the 0.7.7 browser-game kiosk
launch does, so Luna inherits ``start_new_session=True`` (process-group
kill), the shared stderr log, and ``clean_env()``.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from unifideck.auth.edge_browser import launch as _launch
from unifideck.auth.edge_browser.display import auth_window_flags
from unifideck.auth.edge_browser.edge import _BASE_FLAGS, EdgeBrowser
from unifideck.auth.edge_browser.env import clean_env
from unifideck.stores.luna.luna_profile import resolve_profile_dir

if TYPE_CHECKING:
    from unifideck.stores.luna.luna_config import LunaConfig

logger = logging.getLogger(__name__)

#: The autoplay relaxation the stream needs to start without a
#: click-to-play gesture, mirroring the 0.7.7 browser-game kiosk launch.
_AUTOPLAY_FLAG = "--autoplay-policy=no-user-gesture-required"

#: xdotool classname for the Luna kiosk window, distinct from auth /
#: storefront / generic browser-game so focus matching never grabs it.
_WINDOW_CLASS = "unifideck-luna"


class LunaGamepadFlagError(Exception):
    """A required gamepad browser flag is missing or malformed (Req 6.2).

    Raised before the browser is spawned so the launch aborts with the
    ``settings.json`` contents left unchanged and the offending flag named.
    ``flag`` is the malformed token (or ``None`` when the whole gamepad-flag
    set is missing/empty).
    """

    def __init__(self, flag: str | None, message: str) -> None:
        """Build the error naming the offending flag (``None`` when absent)."""
        super().__init__(message)
        self.flag = flag


def validate_gamepad_flags(flags: list[str]) -> None:
    """Reject a missing or malformed gamepad browser-flag set (Req 6.2).

    A browser flag is well-formed when it is a non-blank token beginning
    with ``--`` (optionally ``--key=value``). An empty set, or any entry
    that is blank or does not start with ``--``, aborts the launch via
    :class:`LunaGamepadFlagError` naming the offending flag, so the caller
    never spawns the browser and leaves ``settings.json`` untouched.
    """
    if not flags:
        raise LunaGamepadFlagError(
            None, "no gamepad browser flags configured (stores.luna.gamepad_flags)",
        )
    for flag in flags:
        if not flag or not flag.strip():
            raise LunaGamepadFlagError(
                flag, "malformed gamepad browser flag: empty value",
            )
        if not flag.startswith("--"):
            raise LunaGamepadFlagError(
                flag, f"malformed gamepad browser flag: {flag!r} (must start with '--')",
            )


class LunaBrowser:
    """Thin config/composition over ``EdgeBrowser`` for the Luna kiosk.

    Owns no spawn/kill/env logic of its own: it builds the Luna-specific
    kiosk arg set and delegates the actual process mechanics to the shared
    ``auth/edge_browser/launch.py`` helpers via the composed
    :class:`EdgeBrowser`.

    Attributes:
      config: The resolved :class:`LunaConfig` supplying the profile dir,
        gamepad/cloud flags and locale.
      edge: The composed :class:`EdgeBrowser` whose ``browser_game_cdp_port``
        and ``_spawn_edge_process`` plumbing is reused unchanged.
    """

    def __init__(
        self,
        config: LunaConfig,
        edge: EdgeBrowser | None = None,
    ) -> None:
        """Compose over an ``EdgeBrowser`` (one is built if not injected)."""
        self.config = config
        self.edge = edge or EdgeBrowser()

    # ── Resolution ───────────────────────────────────────────────────

    def profile_dir(self) -> str:
        """The dedicated Luna profile dir (defaults to ``WEB_GAME_PROFILE_DIR``)."""
        return resolve_profile_dir(self.config.profile_dir)

    def cdp_port(self) -> int:
        """The browser-game CDP port the Luna kiosk listens on.

        Resolves to ``EdgeBrowser.browser_game_cdp_port()`` (an offset from
        ``cdp_port``), **not** a bespoke ``9224``.
        """
        return self.edge.browser_game_cdp_port()

    def find_cmd(self) -> list[str] | None:
        """The browser command, or ``None`` until the browser is installed."""
        return self.edge.find_cmd()

    # ── Arg composition ──────────────────────────────────────────────

    def build_kiosk_args(
        self,
        cmd: list[str],
        url: str,
        env: dict[str, str] | None = None,
    ) -> list[str]:
        """Build the Luna kiosk command line for a resolved browser command.

        Mirrors the 0.7.7 browser-game kiosk launch (``launch_browser_game``)
        but with the Luna profile dir, the configured gamepad/cloud flags and
        the Luna window class. The arg set carries, in order: the resolved
        browser command, ``--kiosk``, the Luna window class, the
        ``--remote-debugging-port`` on :meth:`cdp_port`, the dedicated
        ``--user-data-dir``, the shared base flags, the configured
        ``cloud_browser_flags`` and ``gamepad_flags``, the autoplay
        relaxation, the display window flags, ``--lang`` and finally the URL.

        Args:
          cmd: The resolved browser command (see :meth:`find_cmd`).
          url: The Luna detail URL the kiosk opens on.
          env: Pre-computed clean env so display detection and the eventual
            spawn agree on ``DISPLAY``; a fresh ``clean_env()`` is used when
            omitted.

        Returns:
          The full argument list ready for ``_spawn_edge_process``.

        Raises:
          LunaGamepadFlagError: when the configured ``gamepad_flags`` set is
            missing or contains a malformed flag (Req 6.2); the launch
            aborts before any browser is spawned and the error names the
            offending flag.
        """
        validate_gamepad_flags(self.config.gamepad_flags)
        if env is None:
            env = clean_env()
        window_flags = auth_window_flags(env)
        lang = self.config_lang()
        return [
            *cmd,
            "--kiosk",
            f"--class={_WINDOW_CLASS}",
            f"--remote-debugging-port={self.cdp_port()}",
            f"--user-data-dir={self.profile_dir()}",
            *_BASE_FLAGS,
            *self.config.cloud_browser_flags,
            *self.config.gamepad_flags,
            _AUTOPLAY_FLAG,
            *window_flags,
            f"--lang={lang}",
            url,
        ]

    def config_lang(self) -> str:
        """The ``--lang`` value: the language part of the plugin locale."""
        return self.edge.locale_fn().split("-")[0]

    # ── Launch (reuses the shared spawn mechanics) ───────────────────

    def launch(self, url: str) -> bool:
        """Spawn the Luna kiosk browser on ``url``.

        Reuses the shared browser-game kiosk launch mechanics unchanged:
        kills any lingering instance, creates the dedicated profile dir,
        resolves the browser command from the shared ``EdgeBrowser``, builds the
        Luna arg set and hands off to the shared ``_spawn_edge_process``
        (hence ``start_new_session=True`` → process-group kill) with a
        scrubbed ``clean_env()``.

        Returns:
          ``True`` on a successful spawn, ``False`` when no supported
          browser was found or the subprocess failed to start.
        """
        self.edge.kill()
        self.edge.cleanup_stale_profile_state()
        cmd = self.find_cmd()
        if not cmd:
            logger.warning("[Luna] browser is not installed")
            return False
        profile_dir = self.profile_dir()
        Path(profile_dir).mkdir(parents=True, exist_ok=True)
        env = clean_env()
        args = self.build_kiosk_args(cmd, url, env)
        logger.info("[Luna] launching kiosk browser: %s", url[:80])
        return _launch._spawn_edge_process(
            self.edge, args, log_mode="a", label="Luna", env=env,
        )

    def kill(self) -> None:
        """Terminate the Luna kiosk browser (delegates to ``EdgeBrowser``)."""
        self.edge.kill()
