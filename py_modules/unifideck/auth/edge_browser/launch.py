"""auth.edge_browser.launch — Edge subprocess spawn helpers.

Deduplicates the ``launch_auth`` / ``launch_xcloud`` flows.
Both methods share ~35 LOC of identical logic:

  - Kill lingering instance, cleanup profile singleton state
  - Find the Edge command (flatpak or native)
  - mkdir profile dir
  - Build args with shared base flags
  - Open stderr log file (best-effort)
  - Popen with clean env + start_new_session=True (own process group)
  - Track PID, log metadata, handle exceptions

Extracted from ``edge_browser.py`` on 2026-04-18. Each public
entry point now builds its own arg suffix (kiosk vs app mode,
auth vs xcloud CDP port, class name for xdotool) and hands off
to ``_spawn_edge_process`` for the common spawn mechanics.

Process-group coupling with ``EdgeBrowser.kill`` (which calls
``os.killpg(pgid, SIGTERM)``) is preserved by ``start_new_session=True``,
which calls ``setsid()`` in the child after fork. This replaces
the legacy ``preexec_fn=os.setpgrp`` idiom — the new flag is
async-signal-safe and therefore safe with multi-threaded callers,
while ``preexec_fn`` is documented as unsafe with threads.

Functions take the ``EdgeBrowser`` instance as first argument.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from .env import clean_env

if TYPE_CHECKING:
    from .edge import EdgeBrowser

logger = logging.getLogger(__name__)


def _prepare_for_launch(browser: EdgeBrowser) -> list[str] | None:
    """Kill any lingering process, clean profile state, locate Edge.

    Common prelude to launch_auth and launch_xcloud. Returns the
    browser command list, or None if no compatible Edge was found.
    """
    browser.kill()
    browser.cleanup_stale_profile_state()
    cmd = browser.find_cmd()
    if not cmd:
        return None
    # Import PROFILE_DIR lazily — it lives in edge.py which imports
    # us, so top-level import would be circular.
    from .edge import PROFILE_DIR
    Path(PROFILE_DIR).mkdir(parents=True, exist_ok=True)
    return cmd


def _spawn_edge_process(
    browser: EdgeBrowser,
    args: list[str],
    log_mode: str,
    label: str,
    env: dict[str, str] | None = None,
) -> bool:
    """Open stderr log, spawn Popen, track PID, handle errors.

    Shared subprocess spawn with ``start_new_session=True`` so the
    ``os.killpg()`` in ``EdgeBrowser.kill()`` can terminate the
    whole process group cleanly.

    Args:
      browser: EdgeBrowser instance whose ``process`` attribute is
        updated with the spawned subprocess.
      args: Full command line to execute.
      log_mode: ``"w"`` for auth (fresh log), ``"a"`` for xcloud
        (append to same log).
      label: ``"Auth"`` or ``"xCloud"`` for log messages.
      env: Pre-computed clean environment. ``launch_auth`` passes the
        same env it used for display detection so the browser spawns
        with the DISPLAY we measured; defaults to a fresh ``clean_env``.

    Returns True on successful spawn, False on Popen error.
    """
    from .edge import LOG_FILE

    if env is None:
        env = clean_env()
    logger.info(
        "[Edge] Launching %s browser: %s...",
        label.lower(), " ".join(args[:7]),
    )
    logger.info(
        "[Edge] %s browser env DISPLAY=%s "
        "WAYLAND_DISPLAY=%s SteamAppId=%s",
        label, env.get("DISPLAY"), env.get("WAYLAND_DISPLAY"),
        env.get("SteamAppId"),
    )
    stderr_fh = None
    try:
        # Not a ``with`` block: the file descriptor is handed off
        # to ``subprocess.Popen[bytes]`` below and must outlive this
        # scope. We close it explicitly in the ``finally`` clause
        # of the Popen try-block.
        stderr_fh = Path(LOG_FILE).open(log_mode)
    except Exception as e:
        # The log file is a convenience (not essential).
        # Proceed with DEVNULL if we can't open it.
        logger.debug("[Edge] stderr log open failed: %s", e)
    try:
        browser.process = subprocess.Popen[bytes](
            args,
            stdout=subprocess.DEVNULL,
            stderr=(
                stderr_fh if stderr_fh else subprocess.DEVNULL
            ),
            env=env,
            # ``start_new_session=True`` replaces the legacy
            # ``preexec_fn=os.setpgrp`` idiom. The flag uses
            # ``setsid()`` in the child after fork, which is
            # async-signal-safe and therefore safe to use even
            # when other threads are running (preexec_fn is not —
            # see CPython docs, "Note on preexec_fn").
            start_new_session=True,
        )
        logger.info(
            "[Edge] %s browser PID: %s", label, browser.process.pid,
        )
        return True
    except Exception:
        logger.exception("[Edge] Failed to launch %s browser", label.lower())
        return False
    finally:
        if stderr_fh is not None:
            stderr_fh.close()


def launch_auth(browser: EdgeBrowser, auth_url: str) -> bool:
    """Launch the auth browser for OAuth with remote debugging.

    Opens the browser in fullscreen app mode with our CDP port.
    Reuses the shared Unifideck profile so xCloud sessions can keep
    their cookies, while cleaning up stale auth-browser state first.

    Returns:
      True if the browser was launched successfully.
    """
    cmd = _prepare_for_launch(browser)
    if not cmd:
        logger.warning("[Edge] No compatible browser found for auth")
        return False
    from .display import auth_window_flags
    from .edge import _BASE_FLAGS, PROFILE_DIR

    # Compute env once so display detection and the spawn see the same
    # DISPLAY, then size+scale the window to the active display (Deck
    # panel vs. external monitor) for a readable login.
    env = clean_env()
    window_flags = auth_window_flags(env)
    args = [*cmd, f"--app={auth_url}", "--class=unifideck-auth", f"--remote-debugging-port={browser.cdp_port}", f"--user-data-dir={PROFILE_DIR}", *_BASE_FLAGS, "--start-fullscreen", "--enable-touch-events", *window_flags, f"--lang={browser.locale_fn().split('-')[0]}"]
    return _spawn_edge_process(browser, args, log_mode="w", label="Auth", env=env)


def launch_storefront(browser: EdgeBrowser, url: str) -> bool:
    """Launch a browsable store window on ``url``.

    The third window flavour, alongside ``launch_auth`` (app mode,
    chrome-less) and ``launch_xcloud`` (kiosk). Every difference from
    ``launch_auth`` is deliberate:

      - **The URL is positional, not ``--app=``.** ``--app`` is
        precisely what removes the toolbar, and a shop needs Back,
        Forward and an address bar — a wrong tap in app mode
        dead-ends the user with no way back.
      - ``--class=unifideck-store`` so the xdotool classname matching
        used for the xCloud window can never grab this one.
      - Its own CDP port (``storefront_cdp_port()``, auth+2) so a
        store window and an auth or xCloud session are individually
        identifiable over CDP. The caller uses that to detect a
        collision *before* spawning — see
        ``launcher/flows/storefront.py``.
      - ``log_mode="a"`` so opening a shop never truncates the log of
        the sign-in that preceded it.

    Identical to ``launch_auth`` in the one respect that matters most:
    the same ``--user-data-dir={PROFILE_DIR}``. That shared profile is
    where every store's OAuth cookies live, and reusing it is the
    whole reason this function exists rather than a plain
    ``window.open``.

    Returns True if Edge was launched. Does not block.
    """
    cmd = _prepare_for_launch(browser)
    if not cmd:
        logger.warning("[Edge] No compatible browser found for storefront")
        return False
    from .display import auth_window_flags
    from .edge import _BASE_FLAGS, PROFILE_DIR

    env = clean_env()
    window_flags = auth_window_flags(env)
    args = [*cmd, "--class=unifideck-store", f"--remote-debugging-port={browser.storefront_cdp_port()}", f"--user-data-dir={PROFILE_DIR}", *_BASE_FLAGS, "--start-maximized", "--enable-touch-events", *window_flags, f"--lang={browser.locale_fn().split('-')[0]}", url]
    logger.info("[Edge] Launching storefront: %s", url[:80])
    return _spawn_edge_process(
        browser, args, log_mode="a", label="Storefront", env=env,
    )


def launch_xcloud(browser: EdgeBrowser, xcloud_url: str) -> bool:
    """Launch Edge in kiosk mode on an xCloud game streaming URL.

    Distinct from launch_auth in several ways:
      - Kiosk fullscreen (not app mode) — the user plays the game
        for hours, window chrome would be a distraction.
      - Different CDP port so the auth browser and the streaming
        session can coexist (the user may be logged in via auth
        flow while a game is streaming).
      - Different --class so xdotool can focus the right window.
      - Autoplay policy relaxed so the stream starts without
        requiring a click-to-play gesture.

    Shares the same profile as launch_auth so session cookies
    persist between OAuth and streaming — the whole point of using
    Edge specifically is that Microsoft's Xbox session cookies
    survive across flows.

    Args:
      xcloud_url: Target URL, typically
        ``https://www.xbox.com/play/launch/{productId}``. The launcher
        dispatcher passes this through from the game's work_dir slot
        in games.map.

    Returns:
      True if Edge was launched. False on missing browser or
      subprocess failure. The caller is expected to wait on the
      process via its own mechanism — this method does not block.
    """
    cmd = _prepare_for_launch(browser)
    if not cmd:
        logger.warning("[Edge] No compatible browser found for xCloud")
        return False
    from .display import auth_window_flags
    from .edge import _BASE_FLAGS, PROFILE_DIR

    # xCloud uses a distinct CDP port so the auth browser (on
    # browser.cdp_port, typically 9222) and the xCloud session
    # can coexist. Convention: auth=9222, xcloud=9223, storefront=9224.
    # The offsets live on EdgeBrowser so all three flavours agree.
    xcloud_cdp_port = browser.xcloud_cdp_port()
    # Size to the live display, like the other two window flavours.
    # This was pinned at 1024x720 @ 1.25 — the Deck's panel geometry —
    # which on a Steam Machine's TV is a small letterboxed window at
    # handheld scaling.
    env = clean_env()
    window_flags = auth_window_flags(env)
    args = [*cmd, "--kiosk", "--class=unifideck-xcloud", f"--remote-debugging-port={xcloud_cdp_port}", f"--user-data-dir={PROFILE_DIR}", *_BASE_FLAGS, "--autoplay-policy=no-user-gesture-required", *window_flags, f"--lang={browser.locale_fn().split('-')[0]}", xcloud_url]
    logger.info(
        "[Edge] Launching xCloud kiosk: %s", xcloud_url[:80],
    )
    return _spawn_edge_process(
        browser, args, log_mode="a", label="xCloud", env=env,
    )
