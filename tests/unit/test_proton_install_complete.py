"""Unit tests for Proton install-completeness validation + selector skip.

Regression: the selector handed Steam's global-default compat tool to
umu with no check that the install was actually complete. A truncated /
half-extracted Proton (observed: an official Proton whose ``files/`` was
empty; and separately a broken auto-updated Proton-Experimental) makes
every ``umu-run`` operation hang, wedging the serial install queue.

``is_proton_install_complete`` gates the structural case (missing
payload); ``_resolve_logged`` uses it to skip a broken tier and fall
through to the plugin-managed GE-Proton. (A build that is structurally
complete but hangs at *runtime* is caught separately by the compat-step
timeout + warmup GE-retry.)
"""
from __future__ import annotations

import os
import stat

import pytest

from unifideck.launcher.proton.infrastructure import (
    ge_install_lock,
    ge_installer,
    ge_marker,
    proton_health,
    selector,
)
from unifideck.launcher.types.errors import ProtonUnavailableError

_VALID_MANIFEST = '"manifest"\n{\n  "commandline" "/proton run"\n}\n'


def _make_proton(
    root, *, exe=True, files=True, wine=True, version="1.0",
    manifest=_VALID_MANIFEST,
):
    """Build a Proton tool dir; return the ``proton`` script path.

    ``manifest`` writes ``toolmanifest.vdf``; pass ``None`` to omit the file
    or ``""`` to model the truncated-download case.
    """
    root.mkdir(parents=True, exist_ok=True)
    proton = root / "proton"
    proton.write_text("#!/usr/bin/env python3\n")
    if exe:
        proton.chmod(proton.stat().st_mode | stat.S_IXUSR)
    else:
        proton.chmod(proton.stat().st_mode & ~stat.S_IXUSR)
    if files:
        bindir = root / "files" / "bin"
        bindir.mkdir(parents=True, exist_ok=True)
        if wine:
            (bindir / "wine").write_text("")
    if version is not None:
        (root / "version").write_text(version)
    # Every real Proton ships a toolmanifest.vdf; umu parses it on every
    # launch, so a build without a usable one is not a usable build.
    if manifest is not None:
        (root / "toolmanifest.vdf").write_text(manifest)
    # Every official Steam Proton ships a zero-byte dist.lock — assert it
    # does NOT trip the check (it is a normal per-tool lock, not corruption).
    (root / "dist.lock").write_text("")
    return proton


def test_complete_install_passes(tmp_path):
    proton = _make_proton(tmp_path / "Proton - Experimental")
    assert ge_installer.is_proton_install_complete(proton) is True


def test_zero_byte_dist_lock_does_not_fail_a_complete_install(tmp_path):
    # Guards the real-world false-positive: all official Protons have a
    # 0-byte dist.lock; treating it as "mid-install" would reject them all.
    proton = _make_proton(tmp_path / "Proton 10.0")
    assert (proton.parent / "dist.lock").stat().st_size == 0
    assert ge_installer.is_proton_install_complete(proton) is True


def test_zero_byte_toolmanifest_fails(tmp_path):
    """The umu-launcher#706 crash shape.

    umu guards only ``toolmanifest.vdf.is_file()``, so a 0-byte file (what a
    truncated download or interrupted extract leaves) sails through, then
    ``vdf.load`` returns ``{}`` and ``["manifest"]`` raises an unhandled
    ``KeyError: 'manifest'`` — a bare traceback instead of a launch. Failing
    the completeness gate instead makes the selector fall through to a
    known-good Proton.
    """
    proton = _make_proton(tmp_path / "GE-Proton11-3", manifest="")
    assert (proton.parent / "toolmanifest.vdf").stat().st_size == 0
    assert ge_installer.is_proton_install_complete(proton) is False


def test_missing_toolmanifest_fails(tmp_path):
    proton = _make_proton(tmp_path / "GE-Proton11-3", manifest=None)
    assert ge_installer.is_proton_install_complete(proton) is False


def test_garbage_toolmanifest_fails(tmp_path):
    """Present and non-empty, but with no ``manifest`` block to read."""
    proton = _make_proton(tmp_path / "GE-Proton11-3", manifest="<html>404</html>")
    assert ge_installer.is_proton_install_complete(proton) is False


def test_non_executable_proton_script_fails(tmp_path):
    proton = _make_proton(tmp_path / "GE-Proton", exe=False)
    assert ge_installer.is_proton_install_complete(proton) is False


def test_missing_proton_script_fails(tmp_path):
    proton = _make_proton(tmp_path / "P")
    proton.unlink()
    assert ge_installer.is_proton_install_complete(proton) is False


def test_empty_files_dir_fails(tmp_path):
    # The observed "Proton 8.0" case: dir present but no payload.
    root = tmp_path / "Proton 8.0"
    root.mkdir()
    (root / "proton").write_text("x")
    os.chmod(root / "proton", 0o755)
    (root / "files").mkdir()  # empty
    (root / "version").write_text("1.0")
    assert ge_installer.is_proton_install_complete(root / "proton") is False


def test_missing_wine_loader_fails(tmp_path):
    proton = _make_proton(tmp_path / "Proton", wine=False)
    assert ge_installer.is_proton_install_complete(proton) is False


def test_empty_version_fails(tmp_path):
    proton = _make_proton(tmp_path / "Proton", version="")
    assert ge_installer.is_proton_install_complete(proton) is False


# ── selector: skip an incomplete tier, keep a complete one ──────────


def test_resolve_logged_skips_incomplete_install(tmp_path, monkeypatch):
    bad = _make_proton(tmp_path / "Broken", files=False)
    monkeypatch.setattr(selector, "resolve_proton_path", lambda tool: bad)

    tried: list[str] = []
    result = selector._resolve_logged("global-default", "proton_experimental", tried)

    assert result is None  # skipped → caller falls through to managed GE
    assert tried == ["global-default:proton_experimental"]


def test_resolve_logged_returns_complete_install(tmp_path, monkeypatch):
    good = _make_proton(tmp_path / "Proton - Experimental")
    monkeypatch.setattr(selector, "resolve_proton_path", lambda tool: good)

    tried: list[str] = []
    result = selector._resolve_logged("global-default", "proton_experimental", tried)

    assert result == good


def test_resolve_logged_none_when_tool_unresolved(monkeypatch):
    monkeypatch.setattr(selector, "resolve_proton_path", lambda tool: None)
    tried: list[str] = []
    assert selector._resolve_logged("saved", "nope", tried) is None


# ── reasons: a failed check names the missing piece ──────────────────


def test_problem_is_none_for_a_complete_install(tmp_path):
    proton = _make_proton(tmp_path / "GE-Proton11-7")
    assert ge_installer.proton_install_problem(proton) is None


@pytest.mark.parametrize(("kwargs", "needle"), [
    ({"manifest": None}, "toolmanifest.vdf"),
    ({"manifest": ""}, "toolmanifest.vdf"),
    ({"exe": False}, "not executable"),
    ({"wine": False}, "files/bin/wine"),
    ({"version": ""}, "version"),
])
def test_problem_names_the_missing_piece(tmp_path, kwargs, needle):
    proton = _make_proton(tmp_path / "GE-Proton11-7", **kwargs)
    problem = ge_installer.proton_install_problem(proton)
    assert problem is not None
    assert needle in problem


def test_spawn_check_error_names_the_problem(tmp_path):
    """The launch-time error says what is missing, not a guess at the cause."""
    proton = _make_proton(tmp_path / "GE-Proton11-7", manifest=None)
    with pytest.raises(ProtonUnavailableError, match=r"toolmanifest\.vdf"):
        proton_health.assert_proton_still_complete(
            {"PROTONPATH": str(proton.parent)},
        )


# ── the managed GE: a broken tag dir does not count as installed ──────
#
# Field report (v0.7.5): GE-Proton11-7 had an executable ``proton`` but no
# usable ``toolmanifest.vdf``. The presence-only "installed?" check said
# yes, so the default tier picked it on every launch, the plugin never
# re-downloaded it, and every game plus the Ubisoft login died with umu's
# ``KeyError: 'manifest'``.

_TAG = "GE-Proton11-7"


def _managed_root(tmp_path, monkeypatch):
    """Point the installer's scan roots and install target at ``tmp_path``."""
    root = tmp_path / "compatibilitytools.d"
    monkeypatch.setattr(ge_installer, "_SCAN_ROOTS", (str(root),))
    monkeypatch.setattr(ge_installer, "COMPAT_TOOLS_DIR", root)
    return root


def test_manifestless_ge_is_not_installed(tmp_path, monkeypatch):
    root = _managed_root(tmp_path, monkeypatch)
    _make_proton(root / _TAG, manifest=None)

    assert ge_installer.installed_ge_proton_path(_TAG) is None
    assert ge_installer.is_valid_ge_install(_TAG) is False


def test_complete_ge_is_installed(tmp_path, monkeypatch):
    root = _managed_root(tmp_path, monkeypatch)
    proton = _make_proton(root / _TAG)

    assert ge_installer.installed_ge_proton_path(_TAG) == proton


def test_ensure_latest_ge_redownloads_a_manifestless_install(tmp_path, monkeypatch):
    root = _managed_root(tmp_path, monkeypatch)
    _make_proton(root / _TAG, manifest=None)
    monkeypatch.setattr(ge_marker, "_MARKER", tmp_path / "latest.json")
    monkeypatch.setattr(ge_install_lock, "INSTALL_LOCK", tmp_path / "ge.lock")
    monkeypatch.setattr(
        ge_installer, "_fetch_latest_release", lambda timeout: {
            "tag_name": _TAG,
            "assets": [{"name": f"{_TAG}-x86_64.tar.gz", "browser_download_url": "u"}],
        },
    )
    fresh = tmp_path / "fresh" / "proton"
    downloads: list[tuple[str, str]] = []

    def _fake_install(tag, url, _cb):
        downloads.append((tag, url))
        return fresh

    monkeypatch.setattr(ge_installer, "_download_and_install", _fake_install)

    assert ge_installer.ensure_latest_ge() == (fresh, _TAG)
    assert downloads == [(_TAG, "u")]


def test_default_tier_skips_a_manifestless_cached_ge(tmp_path, monkeypatch):
    """Offline with a broken cached GE: fall back to Experimental, not the GE."""
    root = _managed_root(tmp_path, monkeypatch)
    _make_proton(root / _TAG, manifest=None)
    experimental = _make_proton(tmp_path / "Proton - Experimental")
    monkeypatch.setattr(selector.external_ge, "find_external_ge_proton", lambda *a, **k: None)
    monkeypatch.setattr(selector.ge_marker, "read_cached_latest_tag", lambda: _TAG)
    monkeypatch.setattr(selector.ge_installer, "ensure_latest_ge", lambda **_k: None)
    monkeypatch.setattr(
        selector, "resolve_proton_path",
        lambda tool: experimental if tool == "proton_experimental" else None,
    )

    assert selector._default_latest_ge([]) == (experimental, "proton_experimental")
