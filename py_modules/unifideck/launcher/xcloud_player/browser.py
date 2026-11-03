"""The kiosk window the player page runs in: Chromium, or Edge as a fallback.

Chromium needs ``/run/udev`` to identify a controller through the Gamepad
API; its flatpak does not have it, which is why controllers only ever worked in
Edge (the plugin gave Edge that override). ``ensure_udev_access`` adds the same
override for Chromium. The window inherits Steam's game identity from the
launcher through ``clean_env`` (which keeps it), like the Edge kiosk does, so it
comes to the front in Gaming Mode.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from unifideck.auth.edge_browser.display import auth_window_flags
from unifideck.auth.edge_browser.env import clean_env

logger = logging.getLogger(__name__)

CHROMIUM_APP = "org.chromium.Chromium"


def profile_dir() -> Path:
    """The player window's own browser profile (resolved at call time)."""
    return Path("~/.local/share/unifideck/xcloud-player").expanduser()


def _override_path() -> Path:
    return Path(f"~/.local/share/flatpak/overrides/{CHROMIUM_APP}").expanduser()


_FLAGS = ["--kiosk", "--class=unifideck-browser-game", "--no-first-run",
          "--no-default-browser-check", "--password-store=basic",
          "--autoplay-policy=no-user-gesture-required", "--disable-translate",
          "--disable-features=TranslateUI", "--disable-session-crashed-bubble"]


def _flatpak_installed(app_id: str) -> bool:
    for scope in ("--user", "--system"):
        try:
            done = subprocess.run(["flatpak", "info", scope, app_id], capture_output=True,
                                  timeout=10, check=False, env=clean_env())
        except (OSError, subprocess.SubprocessError):
            return False
        if done.returncode == 0:
            return True
    return False


def ensure_udev_access() -> None:
    """Let Chromium's Gamepad API identify controllers. Idempotent, never raises."""
    try:
        override = _override_path()
        if override.is_file() and "/run/udev" in override.read_text():
            return
        subprocess.run(["flatpak", "--user", "override", "--filesystem=/run/udev:ro",
                        CHROMIUM_APP], capture_output=True, timeout=15, check=False,
                       env=clean_env())
        logger.info("[xcloud_player] gave %s /run/udev access for controllers", CHROMIUM_APP)
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("[xcloud_player] could not give Chromium controller access (%s); "
                       "the controller may not work in the stream", type(e).__name__)


def browser_cmd(edge_cmd: list[str] | None) -> tuple[list[str], str] | None:
    """``(command, name)``: Chromium when installed, else Edge, else None."""
    if _flatpak_installed(CHROMIUM_APP):
        ensure_udev_access()
        return ["flatpak", "run", CHROMIUM_APP], "chromium"
    if edge_cmd:
        return edge_cmd, "edge"
    return None


def launch(cmd: list[str], url: str) -> subprocess.Popen[bytes]:
    """Open ``url`` full screen in its own profile. Raises ``OSError``."""
    profile = profile_dir()
    profile.mkdir(parents=True, exist_ok=True)
    env = clean_env()
    args = [*cmd, f"--user-data-dir={profile}", *_FLAGS, *auth_window_flags(env),
            f"--app={url}"]
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            env=env, start_new_session=True)
