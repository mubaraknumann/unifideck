"""The kiosk window the player page runs in: the plugin's own browser.

The browser is the pinned Chrome build ``auth/edge_browser/chrome_installer``
downloads, the same one every other plugin window uses. It is not sandboxed by
flatpak, so its Gamepad API reads ``/run/udev`` and identifies controllers
directly (the Chromium flatpak could not, which is why controllers only ever
worked in the Edge flatpak, to which the plugin had granted that access). The
window inherits Steam's game identity from the launcher through ``clean_env``,
so it comes to the front in Gaming Mode.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from unifideck.auth.edge_browser.display import auth_window_flags
from unifideck.auth.edge_browser.env import clean_env


def profile_dir() -> Path:
    """The player window's own browser profile (resolved at call time)."""
    return Path("~/.local/share/unifideck/xcloud-player").expanduser()


# --disable-infobars hides the "Chrome for Testing is only for automated
# testing" ribbon the build shows above every page. --ozone-platform=x11: see
# ``edge._BASE_FLAGS`` (a native Wayland window stays hidden in Gaming Mode).
_FLAGS = ["--ozone-platform=x11", "--kiosk", "--class=unifideck-browser-game", "--no-first-run",
          "--no-default-browser-check", "--password-store=basic", "--disable-infobars",
          "--autoplay-policy=no-user-gesture-required", "--disable-translate",
          "--disable-features=TranslateUI", "--disable-session-crashed-bubble"]


def browser_cmd(cmd: list[str] | None) -> tuple[list[str], str] | None:
    """``(command, name)`` for the plugin's browser, or None if not installed."""
    return (cmd, "chrome") if cmd else None


def launch(cmd: list[str], url: str) -> subprocess.Popen[bytes]:
    """Open ``url`` full screen in its own profile. Raises ``OSError``."""
    profile = profile_dir()
    profile.mkdir(parents=True, exist_ok=True)
    env = clean_env()
    args = [*cmd, f"--user-data-dir={profile}", *_FLAGS, *auth_window_flags(env),
            f"--app={url}"]
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            env=env, start_new_session=True)
