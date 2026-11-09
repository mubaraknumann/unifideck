"""Startup audit of the GE-Proton trees in use, and the verified install path.

Three field bundles had the managed GE's Windows DLLs replaced while the tree
still passed every structural check, so nothing ever replaced it. These tests
pin the repair loop: which trees are audited, what triggers a download, that
a download is checked against GE's published SHA-512 and recorded as a
per-file manifest, that an identical tree is kept rather than swapped, and
that cleanup removes only our own download leftovers, never a Proton.
"""
from __future__ import annotations

import hashlib
import io
import os
import stat
import tarfile
import time

import pytest

from unifideck.launcher.proton.infrastructure import (
    ge_download,
    ge_install_lock,
    ge_installer,
    ge_marker,
    ge_repair,
    proton_integrity,
    selector,
)

_TAG = "GE-Proton11-11"
_MANIFEST = '"manifest"\n{\n  "commandline" "/proton run"\n}\n'
_BUILTIN = b"\0" * 0x40 + b"Wine builtin DLL" + b"\0" * 64


@pytest.fixture
def compat(tmp_path, monkeypatch):
    root = tmp_path / "compatibilitytools.d"
    root.mkdir()
    monkeypatch.setattr(ge_installer, "COMPAT_TOOLS_DIR", root)
    monkeypatch.setattr(ge_installer, "_SCAN_ROOTS", (str(root),))
    monkeypatch.setattr(ge_install_lock, "INSTALL_LOCK", tmp_path / "ge.lock")
    monkeypatch.setattr(ge_marker, "_MARKER", tmp_path / "latest.json")
    return root


_FILES = {
    "files/bin/wine": b"#!wine\n",
    "files/lib/wine/x86_64-windows/shell32.dll": _BUILTIN,
    "version": b"1 GE\n",
    "toolmanifest.vdf": _MANIFEST.encode(),
    "proton": b"#!/usr/bin/env python3\n",
}


def _write_tree(root):
    for rel, data in _FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (root / "proton").chmod(0o755)
    return root


def _tarball(path, top):
    """A GE-shaped release tarball whose single top dir is ``top``."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel, data in _FILES.items():
            info = tarfile.TarInfo(f"{top}/{rel}")
            info.size = len(data)
            info.mode = 0o755 if rel == "proton" else 0o644
            tar.addfile(info, io.BytesIO(data))
    path.write_bytes(buf.getvalue())
    return hashlib.sha512(buf.getvalue()).hexdigest()


# ── which trees are audited ──────────────────────────────────────────

def test_in_use_set_unions_every_source_and_keeps_only_ge_dirs(compat, monkeypatch):
    for name in (_TAG, "GE-Proton11-9", "GE-Proton10-31", "GE-Proton9-1", "UMU-Proton-9.0-4e"):
        _write_tree(compat / name)
    monkeypatch.setattr(ge_marker, "read_cached_latest_tag", lambda: _TAG)
    monkeypatch.setattr(selector, "saved_proton_tools", lambda: {"GE-Proton11-9"})
    monkeypatch.setattr(selector, "get_unifideck_proton_tool", lambda: "UMU-Proton-9.0-4e")
    monkeypatch.setattr(
        selector, "steam_mapped_tools", lambda: {"GE-Proton10-31", "proton_experimental"},
    )

    assert ge_repair.ge_trees_in_use() == ["GE-Proton10-31", "GE-Proton11-11", "GE-Proton11-9"]


def test_in_use_set_skips_a_tag_that_is_not_on_disk(compat, monkeypatch):
    monkeypatch.setattr(ge_marker, "read_cached_latest_tag", lambda: _TAG)
    monkeypatch.setattr(selector, "saved_proton_tools", set)
    monkeypatch.setattr(selector, "get_unifideck_proton_tool", lambda: None)
    monkeypatch.setattr(selector, "steam_mapped_tools", set)

    assert ge_repair.ge_trees_in_use() == []


# ── audit decisions ──────────────────────────────────────────────────

@pytest.fixture
def installs(monkeypatch):
    """Record reinstall requests instead of downloading."""
    calls = []

    def _install(tag, release, _cb=None):
        calls.append((tag, release))
        return ge_installer.COMPAT_TOOLS_DIR / tag / "proton"

    monkeypatch.setattr(ge_installer, "fetch_release", lambda tag: {"tag_name": tag})
    monkeypatch.setattr(ge_installer, "install_release_unlocked", _install)
    return calls


def _with_manifest(root):
    manifest = proton_integrity.build_manifest(root, _TAG, "abc")
    proton_integrity.write_manifest(root, manifest)
    return root


def test_intact_tree_with_manifest_downloads_nothing(compat, installs):
    _with_manifest(_write_tree(compat / _TAG))

    assert ge_repair.audit_ge(_TAG) == ge_repair.AuditResult(_TAG, "ok")
    assert installs == []


def test_tree_without_manifest_is_verified_once(compat, installs):
    _write_tree(compat / _TAG)

    assert ge_repair.audit_ge(_TAG).status == "verified"
    assert installs == [(_TAG, {"tag_name": _TAG})]


def test_damaged_tree_is_announced_then_reinstalled(compat, installs):
    root = _with_manifest(_write_tree(compat / _TAG))
    (root / "files/bin/wine").unlink()
    announced = []

    result = ge_repair.audit_ge(_TAG, on_repair=lambda t, p: announced.append((t, p)))

    assert result == ge_repair.AuditResult(_TAG, "repaired", "files/bin/wine: missing")
    assert announced == [(_TAG, "files/bin/wine: missing")]
    assert len(installs) == 1


def test_marked_tree_is_reinstalled(compat, installs):
    root = _write_tree(compat / _TAG)
    proton_integrity.mark_damaged(root, "shell32.dll replaced")

    assert ge_repair.audit_ge(_TAG).status == "repaired"
    assert len(installs) == 1


def test_offline_leaves_the_tree_untouched(compat, monkeypatch):
    root = _write_tree(compat / _TAG)
    monkeypatch.setattr(ge_installer, "fetch_release", lambda tag: None)

    assert ge_repair.audit_ge(_TAG).status == "unverified"
    proton_integrity.mark_damaged(root, "x")
    assert ge_repair.audit_ge(_TAG) == ge_repair.AuditResult(_TAG, "failed", "x")
    assert (root / "files/bin/wine").is_file()


def test_absent_tree_is_not_audited(compat, installs):
    assert ge_repair.audit_ge(_TAG).status == "ok"
    assert installs == []


# ── verified install ────────────────────────────────────────────────

@pytest.fixture
def release(tmp_path, monkeypatch):
    """Serve a GE-shaped tarball through the real staging pipeline."""
    src = tmp_path / "release.tar.gz"
    digest = _tarball(src, f"{_TAG}-x86_64")
    published = {"value": digest}

    def _copy(_url, dest, _cb, attempts=3):
        dest.write_bytes(src.read_bytes())
        return True

    monkeypatch.setattr(ge_download, "download_with_retry", _copy)
    monkeypatch.setattr(
        ge_download, "_fetch_published_sha512", lambda _url: published["value"],
    )
    return published


def test_install_records_a_manifest_from_the_verified_tarball(compat, release):
    script = ge_installer._download_and_install(_TAG, "https://x/r.tar.gz", None)

    assert script == compat / _TAG / "proton"
    manifest = proton_integrity.load_manifest(compat / _TAG)
    assert manifest["tarball_sha512"] == release["value"]
    assert set(manifest["files"]) == set(_FILES)
    assert proton_integrity.deep_problem(compat / _TAG) is None
    assert sorted(p.name for p in compat.iterdir()) == [_TAG], "no tarball or staging left"


def test_checksum_mismatch_installs_nothing_and_leaves_nothing(compat, release):
    release["value"] = "0" * 128

    assert ge_installer._download_and_install(_TAG, "https://x/r.tar.gz", None) is None
    assert list(compat.iterdir()) == []


def test_unreachable_checksum_refuses_the_install(compat, release):
    release["value"] = None

    assert ge_installer._download_and_install(_TAG, "https://x/r.tar.gz", None) is None


def test_release_without_checksum_still_installs(compat, release):
    release["value"] = ""

    assert ge_installer._download_and_install(_TAG, "https://x/r.tar.gz", None) is not None


def test_identical_live_tree_is_kept_not_swapped(compat, release):
    live = _write_tree(compat / _TAG)
    proton_integrity.mark_damaged(live, "suspected")
    inode = os.stat(live).st_ino

    ge_installer._download_and_install(_TAG, "https://x/r.tar.gz", None)

    assert os.stat(compat / _TAG).st_ino == inode
    assert proton_integrity.load_manifest(live) is not None
    assert proton_integrity.damaged_marker(live) is None


def test_damaged_live_tree_is_swapped(compat, release):
    live = _write_tree(compat / _TAG)
    (live / "files/lib/wine/x86_64-windows/shell32.dll").write_bytes(b"MZ")
    inode = os.stat(live).st_ino

    script = ge_installer._download_and_install(_TAG, "https://x/r.tar.gz", None)

    assert os.stat(compat / _TAG).st_ino != inode
    assert script.stat().st_mode & stat.S_IXUSR
    assert proton_integrity.deep_problem(compat / _TAG) is None


def test_checksum_url_pairs_with_both_asset_namings():
    assert ge_download.checksum_url("https://x/GE-Proton11-11-x86_64.tar.gz") == (
        "https://x/GE-Proton11-11-x86_64.sha512sum"
    )
    assert ge_download.checksum_url("https://x/GE-Proton9-1.tar.gz") == (
        "https://x/GE-Proton9-1.sha512sum"
    )


# ── cleanup ──────────────────────────────────────────────────────────

def test_sweep_removes_only_stale_download_leftovers(compat):
    old = time.time() - 2 * ge_repair.STALE_AFTER_SECONDS
    stale_dl = compat / f".{_TAG}.dl-abc123"
    stale_old = compat / f".{_TAG}.old-4242"
    fresh_dl = compat / f".{_TAG}.dl-new999"
    unused_ge = compat / "GE-Proton9-1"
    for path in (stale_dl, stale_old, fresh_dl, unused_ge):
        path.mkdir()
        (path / "payload").write_bytes(b"x" * 10)
    for path in (stale_dl, stale_old, unused_ge):
        os.utime(path, (old, old))

    removed = ge_repair.sweep_stale_staging()

    assert sorted(removed) == sorted([stale_dl, stale_old])
    assert fresh_dl.is_dir(), "a download younger than the cutoff may be live"
    assert unused_ge.is_dir(), "an unused Proton is the user's to keep"


# ── ProtonService wiring ─────────────────────────────────────────────

async def test_startup_audits_each_tree_in_use_then_sweeps(monkeypatch):
    from unittest.mock import MagicMock

    from unifideck.services.proton_service import ProtonService

    svc = ProtonService(MagicMock())
    toasts = []

    async def _toast(title, body, version):
        toasts.append((title, body, version))

    def _audit(tag, on_repair=None):
        if tag == "GE-Proton11-9":
            on_repair(tag, "shell32.dll replaced")
            return ge_repair.AuditResult(tag, "repaired", "shell32.dll replaced")
        return ge_repair.AuditResult(tag, "ok")

    swept = []
    monkeypatch.setattr(svc, "_emit_proton_toast", _toast)
    monkeypatch.setattr(ge_repair, "ge_trees_in_use", lambda: [_TAG, "GE-Proton11-9"])
    monkeypatch.setattr(ge_repair, "audit_ge", _audit)
    monkeypatch.setattr(ge_repair, "sweep_stale_staging", lambda: swept.append(True))

    await svc._audit_in_use_ges()

    assert toasts == [
        ("toasts.header.proton", "toasts.launcher.attemptingInstall", "GE-Proton11-9"),
        ("toasts.header.proton", "toasts.launcher.protonReadyBody", "GE-Proton11-9"),
    ]
    assert swept == [True]


async def test_startup_runs_the_audit_after_the_latest_install(monkeypatch):
    from unittest.mock import MagicMock

    from unifideck.services.proton_service import ProtonService

    svc = ProtonService(MagicMock())
    order = []

    async def _latest():
        order.append("latest")

    async def _audit():
        order.append("audit")

    monkeypatch.setattr(svc, "_ensure_latest_ge", _latest)
    monkeypatch.setattr(svc, "_audit_in_use_ges", _audit)

    await svc._maintain_ge()

    assert order == ["latest", "audit"]


def test_compat_tool_names_reads_every_mapping_row():
    from unifideck.utils import vdf_compat

    content = """"InstallConfigStore" { "Software" { "Valve" { "Steam" {
        "CompatToolMapping" {
            "0" { "name" "proton_experimental" "config" "" }
            "2176494352" { "name" "GE-Proton10-31" "config" "" }
            "570" { "name" "" "config" "" }
        }
        "Other" { "name" "not-a-tool" }
    } } } }"""

    assert vdf_compat.compat_tool_names(content) == {"proton_experimental", "GE-Proton10-31"}
    assert vdf_compat.compat_tool_names("") == set()
