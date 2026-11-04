"""auth.edge_browser.env — Session env detection for Edge subprocess.

Extracted from edge_browser.py on 2026-04-18 to isolate the
4-stage graphical-session detection pipeline from the browser
launch concerns. Decky's backend often runs as a service without
the real gaming-mode display variables, so we scrape them from
gamescope-environment files and /proc/<PID>/environ of running
Steam processes.

The ``clean_env`` entry point returns an environment dict suitable
for subprocess.Popen[bytes] when spawning Edge. It strips PluginLoader's
LD_LIBRARY_PATH / LD_PRELOAD pollution, fills in session env from
4-stage discovery, and seeds Steam window env defaults so gaming
mode can surface the spawned window.
"""
from __future__ import annotations

import contextlib
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


_SESSION_ENV_KEYS = (
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "GAMESCOPE_WAYLAND_DISPLAY",
    "DBUS_SESSION_BUS_ADDRESS",
    "DESKTOP_SESSION",
    "XAUTHORITY",
    "XDG_RUNTIME_DIR",
    "XDG_SESSION_TYPE",
    "XDG_CURRENT_DESKTOP",
)


def _seed_from_own_env(result: dict[str, str]) -> None:
    """Step 1: seed result from the calling process's own env."""
    for key in _SESSION_ENV_KEYS:
        value = os.environ.get(key)
        if value:
            result[key] = value


def _read_gamescope_env_file(
    runtime_dir: str, result: dict[str, str],
) -> None:
    """Step 2: fill missing keys from gamescope-environment file.

    gamescope-session drops this file on startup with the real
    display variables. Missing keys are added to ``result`` in place.
    """
    gamescope_env = Path(runtime_dir) / "gamescope-environment"
    if not gamescope_env.exists():
        return
    with (
        contextlib.suppress(OSError),
        gamescope_env.open(
            encoding="utf-8", errors="replace",
        ) as f,
    ):
        for raw_line in f:
            line = raw_line.strip()
            if not line or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if (
                key in _SESSION_ENV_KEYS
                and key not in result
                and value
            ):
                result[key] = value


def _parse_proc_environ(
    pid: str, result: dict[str, str],
) -> bool:
    """Parse /proc/<pid>/environ and update ``result`` in place.

    Returns True if the scan found a usable DISPLAY/WAYLAND_DISPLAY,
    signalling the caller it can stop scanning further PIDs.
    """
    try:
        with Path(f"/proc/{pid}/environ").open("rb") as f:
            env_bytes = f.read()
    except (PermissionError, FileNotFoundError, OSError):
        return False
    for entry in env_bytes.split(b"\x00"):
        decoded = entry.decode("utf-8", errors="replace")
        if "=" not in decoded:
            continue
        key, value = decoded.split("=", 1)
        if (
            key in _SESSION_ENV_KEYS
            and key not in result
            and value
        ):
            result[key] = value
    return bool(
        result.get("DISPLAY") or result.get("WAYLAND_DISPLAY"),
    )


def _scan_steam_process_env(
    uid: int, result: dict[str, str],
) -> None:
    """Step 3: scan Steam/gamescope processes' /proc/PID/environ.

    Stops as soon as a PID yields DISPLAY or WAYLAND_DISPLAY.
    """
    try:
        for proc_name in (
            "steam", "gamescope-session", "gamescope",
        ):
            pids = subprocess.run(
                ["pgrep", "-u", str(uid), "-x", proc_name],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,  # pgrep rc=1 on "no match" is expected
            ).stdout.strip().split("\n")
            for raw_pid in pids:
                pid = raw_pid.strip()
                if not pid:
                    continue
                if _parse_proc_environ(pid, result):
                    logger.info(
                        "[Edge] Session env detected from "
                        "PID %s (%s): DISPLAY=%s "
                        "WAYLAND_DISPLAY=%s",
                        pid, proc_name,
                        result.get("DISPLAY"),
                        result.get("WAYLAND_DISPLAY"),
                    )
                    return
    except Exception as e:
        # pgrep missing, scheduling glitch — not fatal, caller
        # falls through to hardcoded fallbacks.
        logger.debug(
            "[Edge] Session env detection error: %s", e,
        )


def _fallback_display(result: dict[str, str]) -> None:
    """Default DISPLAY to ``:0`` when neither X nor Wayland is set."""
    if not result.get("DISPLAY") and not result.get("WAYLAND_DISPLAY"):
        result["DISPLAY"] = ":0"


def _fallback_runtime_dir(result: dict[str, str], runtime_dir: str) -> None:
    """Default XDG_RUNTIME_DIR to the standard ``/run/user/<uid>`` path."""
    if not result.get("XDG_RUNTIME_DIR"):
        result["XDG_RUNTIME_DIR"] = runtime_dir


def _fallback_dbus(result: dict[str, str], runtime_dir: str) -> None:
    """Point DBUS_SESSION_BUS_ADDRESS at the standard runtime-dir socket.

    Only sets when the socket actually exists on disk — gamescope
    sessions sometimes start before user-mode dbus is up.
    """
    if (
        "DBUS_SESSION_BUS_ADDRESS" not in result
        and Path(f"{runtime_dir}/bus").exists()
    ):
        result["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={runtime_dir}/bus"


def _fallback_xauthority(
    result: dict[str, str], home: str, runtime_dir: str,
) -> None:
    """Find an XAUTHORITY file, runtime-dir first then home.

    Steam's gaming-mode injects per-session files under
    ``/run/user/<uid>/xauth_*`` ; if none exist we fall back to
    the user's ``~/.Xauthority``.
    """
    if "XAUTHORITY" in result:
        return
    xauth_files = [str(p) for p in Path(runtime_dir).glob("xauth_*")]
    if xauth_files:
        result["XAUTHORITY"] = xauth_files[0]
        return
    home_xauth = Path(home) / ".Xauthority"
    if home_xauth.exists():
        result["XAUTHORITY"] = str(home_xauth)


def _fallback_wayland_from_gamescope(result: dict[str, str]) -> None:
    """Promote GAMESCOPE_WAYLAND_DISPLAY to WAYLAND_DISPLAY when usable.

    Only when WAYLAND_DISPLAY isn't already set, the gamescope-
    specific variant is set, the runtime dir is known, AND the
    socket actually exists. The exists check guards against
    stale env from a previous session.
    """
    if (
        result.get("WAYLAND_DISPLAY")
        or not result.get("GAMESCOPE_WAYLAND_DISPLAY")
        or not result.get("XDG_RUNTIME_DIR")
    ):
        return
    socket = (
        Path(result["XDG_RUNTIME_DIR"])
        / result["GAMESCOPE_WAYLAND_DISPLAY"]
    )
    if socket.exists():
        result["WAYLAND_DISPLAY"] = result["GAMESCOPE_WAYLAND_DISPLAY"]


def _apply_fallbacks(
    uid: int, home: str, runtime_dir: str, result: dict[str, str],
) -> None:
    """Step 4: fill remaining gaps with hardcoded fallbacks.

    Handles gamescope sessions that don't expose env through any
    of the previous discovery mechanisms. Each individual
    fallback is its own private helper so adding/removing one
    is a one-line change and the cognitive complexity of this
    function stays flat (a list of calls).

    Order matters: ``_fallback_runtime_dir`` runs before
    ``_fallback_dbus`` and ``_fallback_xauthority`` because
    those two depend on the runtime dir being set.
    """
    _fallback_display(result)
    _fallback_runtime_dir(result, runtime_dir)
    _fallback_dbus(result, runtime_dir)
    _fallback_xauthority(result, home, runtime_dir)
    _fallback_wayland_from_gamescope(result)


def _detect_session_env(uid: int, home: str) -> dict[str, str]:
    """Detect the active graphical session env for Steam / gamescope.

    Decky's backend often runs as a service without the real
    gaming-mode display variables. Four-stage discovery:

        1. Seed from our own env
        2. Read gamescope-environment file
        3. Scan running Steam/gamescope /proc/PID/environ
        4. Apply hardcoded fallbacks for missing keys
    """
    result: dict[str, str] = {}
    runtime_dir = f"/run/user/{uid}"

    _seed_from_own_env(result)
    _read_gamescope_env_file(runtime_dir, result)
    _scan_steam_process_env(uid, result)
    _apply_fallbacks(uid, home, runtime_dir, result)
    return result


#: Gaming Mode exports ``GTK_IM_MODULE=Steam``. The browser is a host process
#: (not a flatpak since 0.7.7), so GTK loads Steam's ``im-steam.so`` and every
#: on-screen keyboard key arrives twice: once as the key event, once committed
#: by the input method (reported on device 2026-11-04). The Edge flatpak never
#: had that module and typed once; without these variables the browser does
#: the same. ``XMODIFIERS`` goes too, or Chrome finds the same method over XIM.
_INPUT_METHOD_KEYS = ("GTK_IM_MODULE", "QT_IM_MODULE", "XMODIFIERS")


def clean_env() -> dict[str, Any]:
    """Return a clean environment for launching the plugin's browser.

    - Strips ``LD_LIBRARY_PATH`` / ``LD_PRELOAD``.
    - Detects the real Steam/gamescope session env when Decky lacks it.
    - Seeds Steam window env defaults so gaming mode can surface the window.
    - Clears ``GTK_MODULES`` to suppress canberra-gtk-module warnings.
    - Drops the input-method variables (see ``_INPUT_METHOD_KEYS``).
    """
    home = str(Path.home())
    uid = Path(home).stat().st_uid
    env = {
        k: v for k, v in os.environ.items()
        if k not in ("LD_LIBRARY_PATH", "LD_PRELOAD")
    }
    env.update(_detect_session_env(uid, home))
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{uid}")
    env.setdefault("SteamGameId", "0")
    env.setdefault("STEAM_COMPAT_APP_ID", "0")
    env.setdefault("SteamAppId", "0")
    env["GTK_MODULES"] = ""
    for key in _INPUT_METHOD_KEYS:
        env.pop(key, None)
    return env
