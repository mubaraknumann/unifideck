"""auth.edge_browser.display — detect the active display + Edge scaling.

The store-login pages are desktop web UIs. Rendered at scale 1.0 on the
Steam Deck's 1280x800 panel the text is tiny (beta feedback: "spare my
eyesight"). The xCloud kiosk already scales its content (1.25x) for
readability; the auth browser did not.

This module makes the auth window's size + scale **dynamic**:

* ``--window-size`` is set to the detected display resolution so the
  fullscreen window lays out at the real size (no internal/external
  mismatch).
* ``--force-device-scale-factor`` targets a consistent ~1024px-wide
  *logical* viewport on any display — ~1.25x on the Deck (matching the
  xCloud kiosk the tester found comfortable), more on larger external
  monitors — so the login is readable everywhere.

Detection is best-effort with layered fallbacks (``xrandr`` →
``xdpyinfo`` → kernel DRM sysfs → 1280x800), run with the same env the
browser is spawned with so it sees the same DISPLAY.
"""
from __future__ import annotations

import logging
import re
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path

logger = logging.getLogger(__name__)

# The logical viewport width we target after scaling. 1280 / 1.25 =
# 1024 — i.e. the comfortable view the xCloud kiosk already uses.
_TARGET_LOGICAL_WIDTH = 1024
# Detection-failure fallback ONLY — never a target. It is the Deck's
# native panel because that is the safest small size: a window sized
# for a handheld is usable on a TV, where the reverse is not true.
# Every real path resolves the live display first (xrandr, xdpyinfo,
# then DRM sysfs, which already prefers an external DP/HDMI output).
_DEFAULT_SIZE = (1280, 800)
_MIN_SCALE = 1.0
_MAX_SCALE = 3.0
# DRM connector sysfs root (overridable in tests).
_DRM_DIR = "/sys/class/drm"

_XRANDR_CURRENT_RE = re.compile(r"current\s+(\d+)\s*x\s*(\d+)")
_XDPYINFO_DIM_RE = re.compile(r"dimensions:\s+(\d+)x(\d+)")
_MODE_RE = re.compile(r"(\d+)x(\d+)")


def compute_scale_factor(width: int) -> float:
    """Device-scale-factor for a display ``width`` px wide.

    Clamped to ``[1.0, 3.0]`` so a 4K external never zooms absurdly and
    a tiny/odd report never shrinks below 1:1.
    """
    raw = width / _TARGET_LOGICAL_WIDTH
    return max(_MIN_SCALE, min(_MAX_SCALE, round(raw, 2)))


def _run(cmd: list[str], env: Mapping[str, str] | None) -> str | None:
    """Run ``cmd`` and return stdout, or ``None`` on any failure."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, env=dict(env) if env else None,
            timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout


def _from_xrandr(env: Mapping[str, str] | None) -> tuple[int, int] | None:
    """Parse ``xrandr``'s ``Screen 0: ... current W x H`` line."""
    out = _run(["xrandr"], env)
    if not out:
        return None
    m = _XRANDR_CURRENT_RE.search(out)
    return (int(m.group(1)), int(m.group(2))) if m else None


def _from_xdpyinfo(env: Mapping[str, str] | None) -> tuple[int, int] | None:
    """Parse ``xdpyinfo``'s ``dimensions: WxH pixels`` line."""
    out = _run(["xdpyinfo"], env)
    if not out:
        return None
    m = _XDPYINFO_DIM_RE.search(out)
    return (int(m.group(1)), int(m.group(2))) if m else None


def _from_drm() -> tuple[int, int] | None:
    """Read the preferred mode of a connected output from DRM sysfs.

    Kernel-level, needs no DISPLAY. Prefers a connected external output
    (DP/HDMI) over the internal panel (eDP), matching "external display
    connected → use its resolution".
    """
    drm = Path(_DRM_DIR)
    if not drm.is_dir():
        return None
    candidates: list[tuple[bool, tuple[int, int]]] = []
    for conn in sorted(drm.glob("card*-*")):
        try:
            if (conn / "status").read_text().strip() != "connected":
                continue
            first_mode = (conn / "modes").read_text().splitlines()[0].strip()
        except (OSError, IndexError):
            continue
        m = _MODE_RE.fullmatch(first_mode)
        if not m:
            continue
        is_internal = "eDP" in conn.name or "LVDS" in conn.name
        candidates.append((is_internal, (int(m.group(1)), int(m.group(2)))))
    if not candidates:
        return None
    # False (external) sorts before True (internal).
    candidates.sort(key=lambda c: c[0])
    return candidates[0][1]


def detect_screen_size(env: Mapping[str, str] | None = None) -> tuple[int, int]:
    """Best-effort current display resolution; ``(1280, 800)`` on failure."""
    sources: tuple[Callable[[], tuple[int, int] | None], ...] = (
        lambda: _from_xrandr(env),
        lambda: _from_xdpyinfo(env),
        _from_drm,
    )
    for source in sources:
        size = source()
        if size and size[0] > 0 and size[1] > 0:
            logger.info("[Edge] detected display %dx%d", size[0], size[1])
            return size
    logger.info(
        "[Edge] display detection failed; defaulting to %dx%d",
        _DEFAULT_SIZE[0], _DEFAULT_SIZE[1],
    )
    return _DEFAULT_SIZE


def auth_window_flags(env: Mapping[str, str] | None = None) -> list[str]:
    """Edge flags sizing+scaling the auth window for the active display."""
    width, height = detect_screen_size(env)
    scale = compute_scale_factor(width)
    logger.info(
        "[Edge] auth window %dx%d @ scale=%.2f", width, height, scale,
    )
    return [
        f"--window-size={width},{height}",
        f"--force-device-scale-factor={scale}",
        f"--device-scale-factor={scale}",
    ]
