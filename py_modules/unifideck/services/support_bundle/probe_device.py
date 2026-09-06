"""support_bundle/probe_device.py — What machine is this.

Hardware and OS identity. Every block here reads ``/sys``, ``/proc``,
``/etc`` or the environment; nothing shells out except one system
Python version check.

The single most valuable line in a bundle is ``product_name``: a Steam
Deck LCD reports ``Jupiter``, an OLED reports ``Galileo``, a Steam
Machine reports ``Fremont``, and anything else means the reporter is on
hardware we do not recognise. Half of "works for me" is a different
device class, and until now nothing in a bug report told us which one we
were looking at.

The raw fields are dumped here, but the *classification* comes from
``utils/device.py`` — this module reads the same ``DMI_PATH`` rather
than keeping its own copy of the path or the codename tables.
"""
from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from unifideck.utils.device import DMI_PATH, detect_device_type, device_override
from unifideck.utils.vulkan import as_dict, detect_32bit_vulkan

from . import procscan

logger = logging.getLogger(__name__)

_DMI_FIELDS = (
    "sys_vendor", "product_name", "product_family",
    "board_name", "bios_version", "bios_date",
)
_MEMINFO_KEYS = ("MemTotal", "MemAvailable", "SwapTotal")
_CERT_DIRS = ("/etc/ssl/certs", "/etc/pki/tls/certs")
# A clock this far from the build era means TLS will fail in ways that
# look like network errors. The plugin already disables certificate
# verification almost everywhere because of stale CA stores, so a
# skewed clock has to be visible in the bundle.
_MIN_PLAUSIBLE_YEAR = 2024


def _read_text(path: Path, limit: int = 65536) -> str:
    """Read a small system file, returning "" on any failure."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return handle.read(limit)
    except OSError:
        return ""


def _read_line(path: Path) -> str:
    """Read and strip a single-value ``/sys`` file."""
    return _read_text(path, 512).strip()


def _parse_env_file(text: str) -> dict[str, str]:
    """Parse ``KEY="value"`` lines (os-release format)."""
    parsed: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        parsed[key.strip()] = value.strip().strip('"').strip("'")
    return parsed


def device_block() -> dict[str, Any]:
    """DMI identity: vendor, model, board, BIOS.

    ``device_type`` is the *derived* class (deck/machine/other) rather
    than another raw field, so a bundle states what the hardware is
    instead of leaving a reader to recognise a codename.

    ``device_type_forced`` is set when a developer override is in play.
    Without it a bundle would report a device class the raw DMI fields
    right above it contradict, and the reader would trust the summary.
    """
    forced = device_override()
    if not DMI_PATH.is_dir():
        block: dict[str, Any] = {
            "available": False, "note": "no DMI (VM, container, or CI)",
        }
        if forced is not None:
            block["device_type"] = forced.value
            block["device_type_forced"] = True
        return block
    values: dict[str, Any] = {"available": True}
    for field in _DMI_FIELDS:
        values[field] = _read_line(DMI_PATH / field) or "unknown"
    values["device_type"] = detect_device_type().value
    if forced is not None:
        values["device_type_forced"] = True
    # Valve really does export these mixed-case; upper-casing them
    # would read nothing.
    values["steam_deck_env"] = os.environ.get("SteamDeck", "")  # noqa: SIM112
    values["gamepad_ui_env"] = os.environ.get("SteamGamepadUI", "")  # noqa: SIM112
    return values


def os_block() -> dict[str, Any]:
    """Distro identity plus immutable-root detection."""
    release = _parse_env_file(_read_text(Path("/etc/os-release")))
    steamos = _parse_env_file(_read_text(Path("/etc/steamos-release")))
    usr_writable = os.access("/usr", os.W_OK)
    return {
        "id": release.get("ID", "unknown"),
        "id_like": release.get("ID_LIKE", ""),
        "variant_id": release.get("VARIANT_ID", ""),
        "version_id": release.get("VERSION_ID", ""),
        "build_id": release.get("BUILD_ID", ""),
        "pretty_name": release.get("PRETTY_NAME", "unknown"),
        "steamos_release": steamos or None,
        "usr_writable": usr_writable,
        "immutable_root": not usr_writable,
        "steamos_readonly_tool": shutil.which("steamos-readonly") or "",
        "rpm_ostree": shutil.which("rpm-ostree") or "",
    }


def kernel_block() -> dict[str, Any]:
    """Kernel identity and ntsync availability.

    ``ntsync`` earns its own field: Protons built against it behave
    differently from those without it, and an install that hangs during
    first-run setup is one of the failures where that distinction is
    the whole answer.
    """
    info = platform.uname()
    return {
        "release": info.release,
        "version": info.version,
        "machine": info.machine,
        "ntsync_device": Path("/dev/ntsync").exists(),
        "uptime_seconds": _uptime(),
    }


def _uptime() -> float | None:
    """System uptime in seconds, from ``/proc/uptime``."""
    raw = _read_line(Path("/proc/uptime"))
    if not raw:
        return None
    try:
        return float(raw.split()[0])
    except (ValueError, IndexError):
        return None


def cpu_block() -> dict[str, Any]:
    """CPU model and core count, without dumping all of cpuinfo."""
    model = ""
    threads = 0
    for line in _read_text(Path("/proc/cpuinfo")).splitlines():
        if line.startswith("processor"):
            threads += 1
        elif not model and line.startswith("model name"):
            model = line.partition(":")[2].strip()
    return {
        "model": model or "unknown",
        "threads": threads or os.cpu_count() or 0,
    }


def gpu_block() -> dict[str, Any]:
    """DRM driver and PCI id per card, displays, and the Vulkan ICD inventory.

    The ICD list earns its place the hard way: a Battle.net install was
    refused for want of a 32-bit Vulkan driver on a machine that had one,
    and answering "did it really?" meant cross-reading Steam's shader log
    because this bundle recorded nothing about Vulkan at all.
    """
    cards: list[dict[str, Any]] = []
    drm = Path("/sys/class/drm")
    vulkan = as_dict(detect_32bit_vulkan())
    if not drm.is_dir():
        return {"cards": cards, "displays": [], "vulkan": vulkan}
    try:
        entries = sorted(drm.iterdir())
    except OSError:
        return {"cards": cards, "displays": [], "vulkan": vulkan}
    for entry in entries:
        if entry.name.startswith("card") and "-" not in entry.name:
            cards.append(_card_info(entry))
    return {"cards": cards, "displays": _displays(entries), "vulkan": vulkan}


def _card_info(entry: Path) -> dict[str, Any]:
    """Driver and PCI id for one DRM card."""
    uevent = _parse_env_file(_read_text(entry / "device" / "uevent"))
    return {
        "card": entry.name,
        "driver": uevent.get("DRIVER", "unknown"),
        "pci_id": uevent.get("PCI_ID", ""),
    }


def _displays(entries: list[Path]) -> list[dict[str, Any]]:
    """Connected outputs and their current mode.

    The OAuth sign-in window is sized and scaled per display, and
    wrong-sized or offscreen login windows have been a real report, so
    the active mode is worth the two file reads.
    """
    found: list[dict[str, Any]] = []
    for entry in entries:
        if "-" not in entry.name:
            continue
        status = _read_line(entry / "status")
        if status != "connected":
            continue
        modes = _read_text(entry / "modes", 256).split()
        found.append({
            "output": entry.name,
            "current_mode": modes[0] if modes else "",
        })
    return found


def memory_block() -> dict[str, Any]:
    """Total, available and swap memory in kB."""
    values: dict[str, Any] = {}
    for line in _read_text(Path("/proc/meminfo")).splitlines():
        key, _, rest = line.partition(":")
        if key in _MEMINFO_KEYS:
            values[key] = rest.strip()
    return values


def session_block() -> dict[str, Any]:
    """Desktop vs Gaming Mode, and the session transport.

    Mode matters more than it looks: sign-in windows, controller
    layouts and the Quick Access popup all behave differently in
    Gaming Mode, so a report that does not say which mode it came from
    starts with a guess.
    """
    gamescope = procscan.is_running("gamescope")
    return {
        "session_type": os.environ.get("XDG_SESSION_TYPE", ""),
        "current_desktop": os.environ.get("XDG_CURRENT_DESKTOP", ""),
        "has_display": bool(os.environ.get("DISPLAY")),
        "has_wayland_display": bool(os.environ.get("WAYLAND_DISPLAY")),
        "has_xdg_runtime_dir": bool(os.environ.get("XDG_RUNTIME_DIR")),
        "gamescope_env": bool(os.environ.get("GAMESCOPE_WAYLAND_DISPLAY")),
        "gamescope_running": gamescope,
        "mode": "gaming" if gamescope else "desktop",
    }


def python_block() -> dict[str, Any]:
    """Both interpreters, side by side.

    The backend runs Decky's bundled Python while the game launcher
    runs the system one. They are different versions on every distro
    we support, and conflating them is its own class of bug, so both
    are recorded explicitly rather than left to inference.
    """
    return {
        "backend_version": sys.version.replace("\n", " "),
        "backend_executable": sys.executable,
        "backend_sys_path_head": sys.path[:5],
        "system_python3": _system_python_version(),
    }


def _system_python_version() -> str:
    """Ask the system ``python3`` for its version."""
    binary = shutil.which("python3")
    if binary is None:
        return "not found"
    try:
        done = subprocess.run(
            [binary, "--version"],
            capture_output=True, text=True, timeout=2, check=False,
        )
    except (OSError, subprocess.SubprocessError) as err:
        return f"probe failed: {err}"
    return (done.stdout or done.stderr).strip() or "unknown"


def locale_block(ui_locale: str) -> dict[str, Any]:
    """System locale plus the plugin's configured UI language."""
    return {
        "lang": os.environ.get("LANG", ""),
        "lc_all": os.environ.get("LC_ALL", ""),
        "language": os.environ.get("LANGUAGE", ""),
        "plugin_ui_locale": ui_locale,
    }


def time_block() -> dict[str, Any]:
    """Clock state and CA-store freshness.

    Grouped together because they fail together: a wrong clock and an
    ancient certificate bundle both surface as TLS errors that look
    like network problems.
    """
    now = time.time()
    year = time.localtime(now).tm_year
    certs = _cert_store_state()
    return {
        "local_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
        "utc_time": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now)),
        "timezone": "/".join(time.tzname),
        "epoch": now,
        "clock_plausible": year >= _MIN_PLAUSIBLE_YEAR,
        "ca_store": certs,
    }


def _cert_store_state() -> dict[str, Any]:
    """Age and size of the system CA bundle."""
    for raw in _CERT_DIRS:
        path = Path(raw)
        if not path.is_dir():
            continue
        try:
            count = sum(1 for _ in path.iterdir())
            mtime = path.stat().st_mtime
        except OSError:
            continue
        return {
            "path": raw,
            "entries": count,
            "modified": time.strftime("%Y-%m-%d", time.localtime(mtime)),
            "age_days": round((time.time() - mtime) / 86400, 1),
        }
    return {"path": "", "entries": 0, "note": "no CA directory found"}
