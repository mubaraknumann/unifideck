"""Regression: a GE-Proton tree with replaced Windows DLLs passed every check.

Field bundles showed every launch on the managed GE dying within seconds on
``unimplemented function shell32.dll.SHGetFolderPathW``, a real export of a
healthy build. The tree had its script, Wine loader, version and
``toolmanifest.vdf``, so ``proton_install_problem`` called it complete, the
selector kept choosing it and nothing ever re-downloaded it.

``proton_integrity`` answers the question that check cannot: are the files GE
shipped still the files GE shipped?
"""
from __future__ import annotations

import os

from unifideck.launcher.proton.infrastructure import proton_integrity as pi

_BUILTIN = b"Wine builtin DLL"
_PLACEHOLDER = b"Wine placeholder DLL"


def _dll(path, marker=_BUILTIN, size=256):
    path.parent.mkdir(parents=True, exist_ok=True)
    body = b"MZ" + b"\0" * (0x40 - 2) + marker
    path.write_bytes(body + b"\0" * max(0, size - len(body)))


def _tree(root, *, lib="lib"):
    """A small Proton-shaped tree with every sentinel DLL for both arches."""
    for name in pi.SENTINEL_DLLS:
        _dll(root / f"files/{lib}/wine/x86_64-windows/{name}.dll")
        _dll(root / f"files/lib/wine/i386-windows/{name}.dll")
    (root / "files/bin").mkdir(parents=True, exist_ok=True)
    (root / "files/bin/wine").write_bytes(b"#!wine\n")
    (root / "files/share").mkdir(parents=True, exist_ok=True)
    (root / "files/share/data.txt").write_text("payload\n")
    os.symlink("wine", root / "files/bin/wine64")
    return root


# ── sentinel DLLs ────────────────────────────────────────────────────

def test_intact_builtins_pass(tmp_path):
    assert pi.sentinel_dll_problem(_tree(tmp_path / "GE")) is None


def test_placeholder_dll_is_named(tmp_path):
    root = _tree(tmp_path / "GE")
    _dll(root / "files/lib/wine/x86_64-windows/shell32.dll", _PLACEHOLDER)

    assert pi.sentinel_dll_problem(root) == (
        "x86_64-windows/shell32.dll is a Wine placeholder, not the builtin"
    )


def test_truncated_dll_is_named(tmp_path):
    root = _tree(tmp_path / "GE")
    (root / "files/lib/wine/x86_64-windows/kernel32.dll").write_bytes(b"MZ")

    assert pi.sentinel_dll_problem(root) == "x86_64-windows/kernel32.dll is truncated or replaced"


def test_i386_placeholder_is_named(tmp_path):
    root = _tree(tmp_path / "GE")
    _dll(root / "files/lib/wine/i386-windows/win32u.dll", _PLACEHOLDER)

    assert "i386-windows/win32u.dll" in pi.sentinel_dll_problem(root)


def test_lib64_layout_is_checked(tmp_path):
    """UMU-Proton 9 keeps its 64-bit PE DLLs under ``lib64``."""
    root = _tree(tmp_path / "UMU", lib="lib64")
    _dll(root / "files/lib64/wine/x86_64-windows/ntdll.dll", _PLACEHOLDER)

    assert "ntdll.dll" in pi.sentinel_dll_problem(root)


def test_unknown_layout_is_never_failed(tmp_path):
    """No wine PE dir at all: an unfamiliar layout, not a damaged one."""
    root = tmp_path / "Odd"
    (root / "files/bin").mkdir(parents=True)

    assert pi.sentinel_dll_problem(root) is None


def test_absent_sentinel_is_ignored(tmp_path):
    root = _tree(tmp_path / "GE")
    (root / "files/lib/wine/x86_64-windows/user32.dll").unlink()

    assert pi.sentinel_dll_problem(root) is None


# ── manifest ─────────────────────────────────────────────────────────

def _with_manifest(tmp_path):
    root = _tree(tmp_path / "GE")
    manifest = pi.build_manifest(root, "GE-Proton11-11", "abc")
    assert manifest is not None
    assert pi.write_manifest(root, manifest)
    return root, manifest


def test_manifest_records_files_and_links(tmp_path):
    root, manifest = _with_manifest(tmp_path)

    assert manifest["files"]["files/share/data.txt"][0] == len("payload\n")
    assert manifest["links"] == {"files/bin/wine64": "wine"}
    assert pi.load_manifest(root) == manifest
    assert pi.MANIFEST_NAME not in manifest["files"]


def test_intact_tree_matches_its_manifest(tmp_path):
    root, manifest = _with_manifest(tmp_path)

    assert pi.manifest_problem(root, manifest, deep=False) is None
    assert pi.manifest_problem(root, manifest, deep=True) is None


def test_missing_file_fails(tmp_path):
    root, manifest = _with_manifest(tmp_path)
    (root / "files/share/data.txt").unlink()

    assert pi.manifest_problem(root, manifest, deep=False) == "files/share/data.txt: missing"


def test_resized_file_fails_the_quick_check(tmp_path):
    root, manifest = _with_manifest(tmp_path)
    (root / "files/share/data.txt").write_text("x")

    assert "size 1, expected 8" in pi.manifest_problem(root, manifest, deep=False)


def test_same_size_content_change_needs_the_deep_check(tmp_path):
    root, manifest = _with_manifest(tmp_path)
    (root / "files/share/data.txt").write_text("PAYLOAD\n")

    assert pi.manifest_problem(root, manifest, deep=False) is None
    assert pi.manifest_problem(root, manifest, deep=True) == "files/share/data.txt: content changed"


def test_retargeted_symlink_fails(tmp_path):
    root, manifest = _with_manifest(tmp_path)
    (root / "files/bin/wine64").unlink()
    os.symlink("elsewhere", root / "files/bin/wine64")

    assert "points at elsewhere" in pi.manifest_problem(root, manifest, deep=False)


def test_files_proton_adds_at_runtime_are_ignored(tmp_path):
    root, manifest = _with_manifest(tmp_path)
    (root / "dist.lock").write_text("")
    (root / "__pycache__").mkdir()
    (root / "__pycache__/proton.pyc").write_bytes(b"x")

    assert pi.manifest_problem(root, manifest, deep=True) is None


def test_several_problems_are_counted(tmp_path):
    root, manifest = _with_manifest(tmp_path)
    (root / "files/share/data.txt").unlink()
    (root / "files/bin/wine").unlink()

    assert pi.manifest_problem(root, manifest, deep=False).endswith("(and 1 more)")


def test_quick_problem_uses_the_recorded_manifest(tmp_path):
    root, _ = _with_manifest(tmp_path)
    (root / "files/share/data.txt").unlink()

    assert pi.quick_problem(root) == "files/share/data.txt: missing"


def test_unreadable_manifest_counts_as_none(tmp_path):
    root = _tree(tmp_path / "GE")
    (root / pi.MANIFEST_NAME).write_text("{not json")

    assert pi.load_manifest(root) is None
    assert pi.quick_problem(root) is None


# ── damage marker ────────────────────────────────────────────────────

def test_marker_round_trip(tmp_path):
    root = _tree(tmp_path / "GE")
    assert pi.damaged_marker(root) is None

    pi.mark_damaged(root, "shell32.dll replaced")
    assert pi.damaged_marker(root) == "shell32.dll replaced"

    pi.clear_damaged(root)
    assert pi.damaged_marker(root) is None
