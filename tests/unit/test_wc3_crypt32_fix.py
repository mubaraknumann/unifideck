"""Warcraft III: Reforged gets a patched Proton, and the real one stays intact.

The bug is upstream Wine's: Reforged 3.0's ``ClientSdk.dll`` passes an
88-byte ``CERT_CHAIN_ENGINE_CONFIG`` and Wine 11.0's ``crypt32`` rejects
anything that is not 64 or 80 bytes, so the game reports Blizzard's valid
TLS as *"check your VPN"*.

**The first fix attempt is why these tests are shaped this way.** Dropping
the rebuilt DLL beside the game exe with ``crypt32=n,b`` — the
``ICU_NATIVE_DLLS`` pattern — loaded it and then killed the game on
launch::

    ACCESS_VIOLATION (Failed to read address 0x40) CRYPT32.dll <- ClientSdk.dll

Wine's ``crypt32`` is a split PE/unix DLL and only gets its ``crypt32.so``
half wired up when loaded as *builtin* from Proton's own directory. So the
patched DLL has to go inside a Proton build, and the tests that matter most
are the ones proving we do that without damaging the build we copied from:

* **The source Proton is never modified.** The variant is a hardlink tree,
  so writing onto a file in place would write *through* to the real
  GE-Proton and replace ``crypt32`` for every game and prefix on the device.
* **A mismatched Proton is refused, not guessed at.** The bundled DLL pairs
  with one build's ``crypt32.so``; pairing it with another's is exactly the
  class of mistake that produced the access violation above.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from unifideck.launcher.proton.fixes import wc3_crypt32

_SUPPORTED = "GE-Proton11-6"
_STOCK_64 = b"stock 64-bit crypt32"
_STOCK_32 = b"stock 32-bit crypt32"
_PATCHED_64 = b"patched 64-bit crypt32"
_PATCHED_32 = b"patched 32-bit crypt32"


def _make_proton(root: Path, build: str, *, fixed: bool = False) -> Path:
    """A minimal Proton tree: the launch script, version file and DLLs.

    ``fixed=True`` mimics a Proton whose own crypt32 carries the upstream
    backport, as GE-Proton11-7 does.
    """
    proton = root / build
    (proton / "files" / "lib" / "wine" / "x86_64-windows").mkdir(parents=True)
    (proton / "files" / "lib" / "wine" / "i386-windows").mkdir(parents=True)
    (proton / "files" / "lib" / "wine" / "x86_64-unix").mkdir(parents=True)
    (proton / "proton").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    (proton / "version").write_text(f"1787951532 {build}\n", encoding="utf-8")
    wine = proton / "files" / "lib" / "wine"
    stock_64 = _STOCK_64 + (b" dwExclusiveFlags %lx" if fixed else b"")
    (wine / "x86_64-windows" / "crypt32.dll").write_bytes(stock_64)
    (wine / "i386-windows" / "crypt32.dll").write_bytes(_STOCK_32)
    # The unix half that makes an app-directory override impossible.
    (wine / "x86_64-unix" / "crypt32.so").write_bytes(b"unix half")
    # An ordinary large-ish sibling, to prove hardlink sharing.
    (wine / "x86_64-windows" / "kernel32.dll").write_bytes(b"kernel32" * 100)
    return proton


@pytest.fixture
def plugin_dir(tmp_path: Path) -> Path:
    root = tmp_path / "plugin"
    for arch, payload in (
        ("x86_64-windows", _PATCHED_64),
        ("i386-windows", _PATCHED_32),
    ):
        target = root / "bin" / "stubs" / "wc3fix" / arch
        target.mkdir(parents=True)
        (target / "crypt32.dll").write_bytes(payload)
    return root


@pytest.fixture
def variants_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "variants"
    monkeypatch.setattr(wc3_crypt32, "variants_root", lambda: root)
    return root


@pytest.fixture
def proton(tmp_path: Path) -> Path:
    return _make_proton(tmp_path / "tools", _SUPPORTED)


class TestAffectedTitle:
    def test_w3_is_affected(self) -> None:
        assert wc3_crypt32.is_affected_title("W3")

    @pytest.mark.parametrize("family", ["W1R", "W2R", "D3", "WoW", "Fen", ""])
    def test_other_families_are_not(self, family: str) -> None:
        assert not wc3_crypt32.is_affected_title(family)

    def test_unresolved_family_is_not(self) -> None:
        assert not wc3_crypt32.is_affected_title(None)


class TestAlreadyFixedProton:
    """The workaround must retire itself once Proton ships the fix.

    Measured 2026-09-17: GE-Proton11-6 has no such crypt32, GE-Proton11-7
    does. Probing the DLL rather than an allowlist is what lets a Proton
    bump switch this off with no code change.
    """

    def test_an_unfixed_proton_is_detected(self, proton: Path) -> None:
        assert not wc3_crypt32.proton_has_crypt32_fix(proton)

    def test_a_fixed_proton_is_detected(self, tmp_path: Path) -> None:
        fixed = _make_proton(tmp_path / "t", "GE-Proton11-7", fixed=True)
        assert wc3_crypt32.proton_has_crypt32_fix(fixed)

    def test_a_missing_dll_is_not_mistaken_for_fixed(self, tmp_path: Path) -> None:
        """Absent must read as "needs the fix", never as "already fine"."""
        bare = tmp_path / "bare"
        bare.mkdir()
        assert not wc3_crypt32.proton_has_crypt32_fix(bare)

    def test_our_own_bundled_dll_carries_the_marker(self) -> None:
        """Guards the probe against a bundled-DLL swap that lacks it.

        If a future rebuild dropped the trace strings, the probe would call
        our own patched Proton unfixed and rebuild it on every launch.
        """
        from tests.unit._repo_root import find_repo_file

        bundled = find_repo_file("bin/stubs/wc3fix/x86_64-windows/crypt32.dll")
        assert bundled is not None, "bundled patched crypt32.dll is missing"
        assert b"dwExclusiveFlags" in bundled.read_bytes()


class TestProtonDetection:
    def test_reads_the_build_name_from_the_version_file(self, proton: Path) -> None:
        assert wc3_crypt32.proton_build_name(proton) == _SUPPORTED

    def test_supported_build_is_recognised(self, proton: Path) -> None:
        assert wc3_crypt32.is_supported_proton(proton)

    @pytest.mark.parametrize(
        "build", ["GE-Proton11-5", "proton-experimental", "GE-Proton12-1"],
    )
    def test_other_builds_are_not(self, tmp_path: Path, build: str) -> None:
        assert not wc3_crypt32.is_supported_proton(_make_proton(tmp_path / "t", build))

    def test_a_renamed_directory_still_reads_its_real_build(
        self, tmp_path: Path,
    ) -> None:
        """The version file is authoritative; the folder name is not."""
        built = _make_proton(tmp_path / "t", _SUPPORTED)
        renamed = built.rename(tmp_path / "t" / "my-proton")
        assert wc3_crypt32.is_supported_proton(renamed)

    def test_a_directory_with_no_version_file_is_not(self, tmp_path: Path) -> None:
        bare = tmp_path / "bare"
        bare.mkdir()
        assert not wc3_crypt32.is_supported_proton(bare)


@pytest.mark.usefixtures("variants_root")
class TestEnsurePatchedProton:
    def test_refuses_an_unsupported_build(
        self, plugin_dir: Path, tmp_path: Path,
    ) -> None:
        other = _make_proton(tmp_path / "t", "GE-Proton11-5")
        assert wc3_crypt32.ensure_patched_proton(plugin_dir, other) is None

    def test_refuses_when_nothing_is_bundled(
        self, tmp_path: Path, proton: Path,
    ) -> None:
        assert wc3_crypt32.ensure_patched_proton(tmp_path / "empty", proton) is None

    def test_builds_a_variant_with_the_patched_dlls(
        self, plugin_dir: Path, proton: Path,
    ) -> None:
        variant = wc3_crypt32.ensure_patched_proton(plugin_dir, proton)
        assert variant is not None
        wine = variant / "files" / "lib" / "wine"
        assert (wine / "x86_64-windows" / "crypt32.dll").read_bytes() == _PATCHED_64
        assert (wine / "i386-windows" / "crypt32.dll").read_bytes() == _PATCHED_32

    def test_leaves_the_source_proton_untouched(
        self, plugin_dir: Path, proton: Path,
    ) -> None:
        """The whole point of unlink-before-write."""
        wc3_crypt32.ensure_patched_proton(plugin_dir, proton)
        wine = proton / "files" / "lib" / "wine"
        assert (wine / "x86_64-windows" / "crypt32.dll").read_bytes() == _STOCK_64
        assert (wine / "i386-windows" / "crypt32.dll").read_bytes() == _STOCK_32

    def test_shares_inodes_for_everything_it_did_not_patch(
        self, plugin_dir: Path, proton: Path,
    ) -> None:
        """A hardlink tree, not a 1.5 GB copy."""
        variant = wc3_crypt32.ensure_patched_proton(plugin_dir, proton)
        assert variant is not None
        shared = variant / "files" / "lib" / "wine" / "x86_64-windows" / "kernel32.dll"
        original = proton / "files" / "lib" / "wine" / "x86_64-windows" / "kernel32.dll"
        assert shared.stat().st_ino == original.stat().st_ino

    def test_the_patched_dll_is_a_separate_inode(
        self, plugin_dir: Path, proton: Path,
    ) -> None:
        variant = wc3_crypt32.ensure_patched_proton(plugin_dir, proton)
        assert variant is not None
        patched = variant / "files" / "lib" / "wine" / "x86_64-windows" / "crypt32.dll"
        original = proton / "files" / "lib" / "wine" / "x86_64-windows" / "crypt32.dll"
        assert patched.stat().st_ino != original.stat().st_ino
        assert patched.stat().st_nlink == 1

    def test_carries_the_unix_half_across(
        self, plugin_dir: Path, proton: Path,
    ) -> None:
        """crypt32.so must be present, or the PE half faults on a null call."""
        variant = wc3_crypt32.ensure_patched_proton(plugin_dir, proton)
        assert variant is not None
        assert (variant / "files" / "lib" / "wine" / "x86_64-unix" / "crypt32.so").is_file()

    def test_preserves_symlinks(
        self, plugin_dir: Path, proton: Path,
    ) -> None:
        os.symlink("x86_64-windows", proton / "files" / "lib" / "wine" / "current")
        variant = wc3_crypt32.ensure_patched_proton(plugin_dir, proton)
        assert variant is not None
        assert (variant / "files" / "lib" / "wine" / "current").is_symlink()

    def test_reuses_an_existing_variant(
        self, plugin_dir: Path, proton: Path,
    ) -> None:
        first = wc3_crypt32.ensure_patched_proton(plugin_dir, proton)
        assert first is not None
        patched = first / "files" / "lib" / "wine" / "x86_64-windows" / "crypt32.dll"
        stamp = patched.stat().st_ino
        second = wc3_crypt32.ensure_patched_proton(plugin_dir, proton)
        assert second == first
        assert patched.stat().st_ino == stamp

    def test_rebuilds_when_the_bundled_dll_changes(
        self, plugin_dir: Path, proton: Path,
    ) -> None:
        wc3_crypt32.ensure_patched_proton(plugin_dir, proton)
        source = plugin_dir / "bin" / "stubs" / "wc3fix" / "x86_64-windows" / "crypt32.dll"
        source.write_bytes(b"a newer patched build")
        variant = wc3_crypt32.ensure_patched_proton(plugin_dir, proton)
        assert variant is not None
        patched = variant / "files" / "lib" / "wine" / "x86_64-windows" / "crypt32.dll"
        assert patched.read_bytes() == b"a newer patched build"

    def test_rebuilds_when_the_source_proton_changes(
        self, plugin_dir: Path, proton: Path,
    ) -> None:
        """A Proton update must not leave a variant built from the old one."""
        wc3_crypt32.ensure_patched_proton(plugin_dir, proton)
        (proton / "version").write_text(
            f"1799999999 {_SUPPORTED}\n", encoding="utf-8",
        )
        shared = proton / "files" / "lib" / "wine" / "x86_64-windows" / "kernel32.dll"
        shared.unlink()
        shared.write_bytes(b"updated kernel32")
        variant = wc3_crypt32.ensure_patched_proton(plugin_dir, proton)
        assert variant is not None
        rebuilt = variant / "files" / "lib" / "wine" / "x86_64-windows" / "kernel32.dll"
        assert rebuilt.read_bytes() == b"updated kernel32"

    def test_a_half_built_variant_is_rebuilt(
        self, plugin_dir: Path, proton: Path, variants_root: Path,
    ) -> None:
        """A variant with no marker is not trusted."""
        stale = variants_root / f"{_SUPPORTED}{wc3_crypt32.VARIANT_SUFFIX}"
        stale.mkdir(parents=True)
        (stale / "proton").write_text("junk", encoding="utf-8")
        variant = wc3_crypt32.ensure_patched_proton(plugin_dir, proton)
        assert variant is not None
        wine = variant / "files" / "lib" / "wine"
        assert (wine / "x86_64-windows" / "crypt32.dll").read_bytes() == _PATCHED_64

    def test_builds_inside_the_variants_root(
        self, plugin_dir: Path, proton: Path, variants_root: Path,
    ) -> None:
        variant = wc3_crypt32.ensure_patched_proton(plugin_dir, proton)
        assert variant is not None
        assert variants_root.resolve() in variant.resolve().parents
