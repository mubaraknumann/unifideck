"""Regression: GE's arch-suffixed asset naming broke every GE-Proton install.

At GE-Proton11-4 GloriousEggroll renamed the x86_64 release asset from a
bare ``<tag>.tar.gz`` to ``<tag>-x86_64.tar.gz``, and the archive's
top-level directory changed with it — ``GE-Proton11-5-x86_64/`` rather
than ``GE-Proton11-5/``.

``_promote_extracted`` looked for ``staging/<tag>/proton``, so every
install failed its "extracted tree missing proton script" check *after*
a complete ~500 MB download, and ``_download_and_install``'s ``finally``
threw the staged tree away. Observed on-device as four full
download-and-discard cycles in one evening, with the plugin stuck on the
newest GE already on disk (11-3) and logging a GE-Proton install failure
on every start.

Two independent guards, both exercised here:

* the tree is located by *having a ``proton`` script*, not by its name;
* it is promoted as the bare ``<tag>``, because the marker file,
  ``installed_ge_proton_path`` and the selector all key off the tag — an
  arch-suffixed install dir would work once and then re-download forever.
"""
from __future__ import annotations

from pathlib import Path

from unifideck.launcher.proton.infrastructure import ge_installer

# The real GE-Proton11-5 asset list, as returned by the GitHub API.
_ASSETS_NEW = [
    {"name": "GE-Proton11-5-aarch64.sha512sum", "browser_download_url": "http://x/arm.sha"},
    {"name": "GE-Proton11-5-aarch64.tar.gz", "browser_download_url": "http://x/arm.tar.gz"},
    {"name": "GE-Proton11-5-x86_64.sha512sum", "browser_download_url": "http://x/x86.sha"},
    {"name": "GE-Proton11-5-x86_64.tar.gz", "browser_download_url": "http://x/x86.tar.gz"},
]


def _make_tree(root: Path, *, manifest: bool = True) -> Path:
    """Build a minimal extracted Proton tool dir at ``root``.

    Carries every piece ``is_proton_install_complete`` checks, so a
    promoted tree counts as installed; ``manifest=False`` models the
    truncated-download case.
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / "proton").write_text("#!/bin/sh\n")
    (root / "files" / "bin").mkdir(parents=True, exist_ok=True)
    (root / "files" / "bin" / "wine").write_text("#!/bin/sh\n")
    (root / "version").write_text("1\n")
    if manifest:
        (root / "toolmanifest.vdf").write_text('"manifest"\n{\n"commandline" "/proton"\n}\n')
    return root


# ── _select_tarball ───────────────────────────────────────────────

def test_select_tarball_picks_arch_suffixed_x86_asset():
    """GE-Proton11-4+ naming: the x86_64 build is the one we want."""
    assert ge_installer._select_tarball(_ASSETS_NEW, "GE-Proton11-5") == "http://x/x86.tar.gz"


def test_select_tarball_still_picks_legacy_bare_tag_asset():
    """GE-Proton11-3 and earlier shipped x86_64 as a bare ``<tag>.tar.gz``."""
    assets = [
        {"name": "GE-Proton11-3-aarch64.tar.gz", "browser_download_url": "http://x/arm.tar.gz"},
        {"name": "GE-Proton11-3.tar.gz", "browser_download_url": "http://x/x86.tar.gz"},
    ]
    assert ge_installer._select_tarball(assets, "GE-Proton11-3") == "http://x/x86.tar.gz"


def test_select_tarball_prefers_x86_over_an_unknown_arch():
    """The deny-list can't know every future arch — the positive match must win."""
    assets = [
        {"name": "GE-Proton12-1-riscv64.tar.gz", "browser_download_url": "http://x/riscv.tar.gz"},
        {"name": "GE-Proton12-1-x86_64.tar.gz", "browser_download_url": "http://x/x86.tar.gz"},
    ]
    assert ge_installer._select_tarball(assets, None) == "http://x/x86.tar.gz"


def test_select_tarball_none_when_no_tarball():
    assert ge_installer._select_tarball([{"name": "notes.txt", "browser_download_url": "http://x/n"}]) is None


# ── _promote_extracted ────────────────────────────────────────────

def test_promotes_arch_suffixed_tree_under_the_bare_tag(tmp_path, monkeypatch):
    """The actual bug: ``<tag>-x86_64/`` extracted, ``<tag>/`` expected."""
    compat = tmp_path / "compatibilitytools.d"
    compat.mkdir()
    monkeypatch.setattr(ge_installer, "COMPAT_TOOLS_DIR", compat)
    staging = tmp_path / "staging"
    _make_tree(staging / "GE-Proton11-5-x86_64")

    final = ge_installer._promote_extracted(staging, "GE-Proton11-5")

    assert final == compat / "GE-Proton11-5" / "proton"
    assert final.is_file()
    assert not (compat / "GE-Proton11-5-x86_64").exists(), "must not publish the arch-suffixed name"


def test_promoted_tree_satisfies_the_already_installed_check(tmp_path, monkeypatch):
    """Closes the re-download loop: the next start must find it by tag."""
    compat = tmp_path / "compatibilitytools.d"
    compat.mkdir()
    monkeypatch.setattr(ge_installer, "COMPAT_TOOLS_DIR", compat)
    monkeypatch.setattr(ge_installer, "_SCAN_ROOTS", (str(compat),))
    staging = tmp_path / "staging"
    _make_tree(staging / "GE-Proton11-5-x86_64")

    ge_installer._promote_extracted(staging, "GE-Proton11-5")

    assert ge_installer.is_valid_ge_install("GE-Proton11-5") is True


def test_promotes_legacy_tag_named_tree(tmp_path, monkeypatch):
    """Older archives unpack to ``<tag>/`` — still the common case on re-runs."""
    compat = tmp_path / "compatibilitytools.d"
    compat.mkdir()
    monkeypatch.setattr(ge_installer, "COMPAT_TOOLS_DIR", compat)
    staging = tmp_path / "staging"
    _make_tree(staging / "GE-Proton11-3")

    final = ge_installer._promote_extracted(staging, "GE-Proton11-3")

    assert final == compat / "GE-Proton11-3" / "proton"


def test_no_proton_script_anywhere_is_still_rejected(tmp_path, monkeypatch):
    """A junk/truncated extract must not be promoted."""
    compat = tmp_path / "compatibilitytools.d"
    compat.mkdir()
    monkeypatch.setattr(ge_installer, "COMPAT_TOOLS_DIR", compat)
    staging = tmp_path / "staging"
    (staging / "GE-Proton11-5-x86_64" / "files").mkdir(parents=True)

    assert ge_installer._promote_extracted(staging, "GE-Proton11-5") is None
    assert not any(compat.iterdir())


def test_tag_matching_tree_wins_over_sort_order(tmp_path, monkeypatch):
    """With several candidates the tag decides, never ``sorted()``."""
    compat = tmp_path / "compatibilitytools.d"
    compat.mkdir()
    monkeypatch.setattr(ge_installer, "COMPAT_TOOLS_DIR", compat)
    staging = tmp_path / "staging"
    _make_tree(staging / "AAA-decoy")
    _make_tree(staging / "GE-Proton11-5-x86_64")
    (staging / "GE-Proton11-5-x86_64" / "marker").write_text("real\n")

    ge_installer._promote_extracted(staging, "GE-Proton11-5")

    assert (compat / "GE-Proton11-5" / "marker").is_file()


def test_truncated_toolmanifest_still_rejected_under_new_naming(tmp_path, monkeypatch):
    """The manifest guard must survive the rename, not be bypassed by it."""
    compat = tmp_path / "compatibilitytools.d"
    compat.mkdir()
    monkeypatch.setattr(ge_installer, "COMPAT_TOOLS_DIR", compat)
    staging = tmp_path / "staging"
    _make_tree(staging / "GE-Proton11-5-x86_64", manifest=False)

    assert ge_installer._promote_extracted(staging, "GE-Proton11-5") is None
    assert not any(compat.iterdir())
