"""Unit tests for auth.edge_browser.display — dynamic auth-window sizing.

Covers the scale-factor math and the layered resolution detection
(xrandr → xdpyinfo → DRM sysfs → 1280x800 default) that makes the
store-login browser readable on the Deck and on external monitors.
"""
from __future__ import annotations

import subprocess

import pytest

from unifideck.auth.edge_browser import display as d

# ── compute_scale_factor ──────────────────────────────────────────

@pytest.mark.parametrize(
    ("width", "expected"),
    [
        (1280, 1.25),   # Steam Deck panel → matches xCloud comfort
        (1920, 1.88),   # 1080p external
        (2560, 2.5),    # 1440p external
        (3840, 3.0),    # 4K → capped
        (800, 1.0),     # tiny/odd → floored at 1:1
    ],
)
def test_compute_scale_factor(width, expected):
    assert d.compute_scale_factor(width) == expected


# ── detect_screen_size source priority ────────────────────────────

def test_detect_prefers_xrandr(monkeypatch):
    monkeypatch.setattr(d, "_from_xrandr", lambda env: (1920, 1080))
    monkeypatch.setattr(d, "_from_xdpyinfo", lambda env: (1280, 800))
    monkeypatch.setattr(d, "_from_drm", lambda: (640, 480))
    assert d.detect_screen_size({}) == (1920, 1080)


def test_detect_falls_through_to_xdpyinfo(monkeypatch):
    monkeypatch.setattr(d, "_from_xrandr", lambda env: None)
    monkeypatch.setattr(d, "_from_xdpyinfo", lambda env: (2560, 1440))
    monkeypatch.setattr(d, "_from_drm", lambda: (640, 480))
    assert d.detect_screen_size({}) == (2560, 1440)


def test_detect_falls_through_to_drm(monkeypatch):
    monkeypatch.setattr(d, "_from_xrandr", lambda env: None)
    monkeypatch.setattr(d, "_from_xdpyinfo", lambda env: None)
    monkeypatch.setattr(d, "_from_drm", lambda: (3840, 2160))
    assert d.detect_screen_size({}) == (3840, 2160)


def test_detect_defaults_when_all_fail(monkeypatch):
    monkeypatch.setattr(d, "_from_xrandr", lambda env: None)
    monkeypatch.setattr(d, "_from_xdpyinfo", lambda env: None)
    monkeypatch.setattr(d, "_from_drm", lambda: None)
    assert d.detect_screen_size({}) == (1280, 800)


# ── xrandr / xdpyinfo parsing ─────────────────────────────────────

def test_from_xrandr_parses_current(monkeypatch):
    out = (
        "Screen 0: minimum 320 x 200, current 1920 x 1080, "
        "maximum 16384 x 16384\n"
    )
    monkeypatch.setattr(d, "_run", lambda cmd, env: out)
    assert d._from_xrandr({}) == (1920, 1080)


def test_from_xdpyinfo_parses_dimensions(monkeypatch):
    out = "  dimensions:    1280x800 pixels (338x211 millimeters)\n"
    monkeypatch.setattr(d, "_run", lambda cmd, env: out)
    assert d._from_xdpyinfo({}) == (1280, 800)


def test_from_xrandr_none_when_command_missing(monkeypatch):
    monkeypatch.setattr(d, "_run", lambda cmd, env: None)
    assert d._from_xrandr({}) is None


def test_run_swallows_subprocess_errors(monkeypatch):
    def boom(*a, **k):
        raise OSError("xrandr not found")
    monkeypatch.setattr(subprocess, "run", boom)
    assert d._run(["xrandr"], {}) is None


# ── DRM sysfs detection ───────────────────────────────────────────

def test_from_drm_prefers_external(tmp_path, monkeypatch):
    drm = tmp_path / "drm"
    for name, status, mode in (
        ("card0-eDP-1", "connected", "1280x800"),
        ("card0-DP-1", "connected", "1920x1080"),
        ("card0-HDMI-A-1", "disconnected", "3840x2160"),
    ):
        conn = drm / name
        conn.mkdir(parents=True)
        (conn / "status").write_text(status + "\n")
        (conn / "modes").write_text(mode + "\n")
    monkeypatch.setattr(d, "_DRM_DIR", str(drm))

    # external DP (1920x1080) wins over internal eDP.
    assert d._from_drm() == (1920, 1080)


def test_from_drm_internal_when_no_external(tmp_path, monkeypatch):
    drm = tmp_path / "drm"
    conn = drm / "card0-eDP-1"
    conn.mkdir(parents=True)
    (conn / "status").write_text("connected\n")
    (conn / "modes").write_text("1280x800\n")
    monkeypatch.setattr(d, "_DRM_DIR", str(drm))

    assert d._from_drm() == (1280, 800)


# ── auth_window_flags integration ─────────────────────────────────

def test_auth_window_flags(monkeypatch):
    monkeypatch.setattr(d, "detect_screen_size", lambda env=None: (1920, 1080))
    flags = d.auth_window_flags({})
    # Scaled units: 1920/1.88 and 1080/1.88, one over, so the window covers
    # the screen where gamescope has no window manager to maximise it.
    assert "--window-size=1022,575" in flags
    assert "--window-position=0,0" in flags
    assert "--force-device-scale-factor=1.88" in flags
    assert "--device-scale-factor=1.88" in flags
