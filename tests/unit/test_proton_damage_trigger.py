"""A failed run checks its Proton; a damaged one is marked and routed around.

Field bundles: every game on the managed GE-Proton died within seconds while
the tree still looked complete. Nothing checked the tree after those
failures, so the next launch picked it again. ``check_after_failure`` runs
the quick integrity check only when a run fails, and records damage so
``proton_install_problem`` rejects the tree on every later launch.
"""
from __future__ import annotations

import stat

import pytest

from unifideck.launcher.proton.infrastructure import (
    ge_installer,
    proton_health,
    proton_integrity,
)
from unifideck.launcher.proton.infrastructure import umu_runtime as ur

_MANIFEST = '"manifest"\n{\n  "commandline" "/proton run"\n}\n'


def _proton_tree(root, marker=b"Wine builtin DLL"):
    """A complete Proton tool dir whose shell32 carries ``marker``."""
    (root / "files/bin").mkdir(parents=True)
    (root / "files/bin/wine").write_text("")
    pe = root / "files/lib/wine/x86_64-windows"
    pe.mkdir(parents=True)
    (pe / "shell32.dll").write_bytes(b"\0" * 0x40 + marker + b"\0" * 64)
    (root / "version").write_text("1 GE\n")
    (root / "toolmanifest.vdf").write_text(_MANIFEST)
    proton = root / "proton"
    proton.write_text("#!/usr/bin/env python3\n")
    proton.chmod(proton.stat().st_mode | stat.S_IXUSR)
    return proton


@pytest.fixture
def toasts(monkeypatch):
    sent = []
    from unifideck.launcher import frontend_bridge

    monkeypatch.setattr(
        frontend_bridge, "launcher_toast", lambda key, **kw: sent.append((key, kw)),
    )
    return sent


def test_damaged_proton_is_marked_and_rejected(tmp_path, toasts):
    root = tmp_path / "GE-Proton11-11"
    proton = _proton_tree(root, marker=b"Wine placeholder DLL")
    assert ge_installer.proton_install_problem(proton) is None, "the field blind spot"

    problem = proton_health.check_after_failure({"PROTONPATH": str(root)})

    assert problem == "x86_64-windows/shell32.dll is a Wine placeholder, not the builtin"
    assert proton_integrity.damaged_marker(root) == problem
    assert ge_installer.proton_install_problem(proton) == f"damaged: {problem}"
    assert toasts == [(
        "toasts.launcher.protonDamagedMessage",
        {
            "i18n_title_key": "toasts.header.proton",
            "i18n_params": {"version": "GE-Proton11-11"},
            "severity": "error",
        },
    )]


def test_healthy_proton_is_left_alone(tmp_path, toasts):
    """An ordinary game crash must not mark the Proton it ran on."""
    root = tmp_path / "GE-Proton11-11"
    _proton_tree(root)

    assert proton_health.check_after_failure({"PROTONPATH": str(root)}) is None
    assert proton_integrity.damaged_marker(root) is None
    assert toasts == []


def test_already_marked_proton_is_not_toasted_again(tmp_path, toasts):
    root = tmp_path / "GE-Proton11-11"
    _proton_tree(root, marker=b"Wine placeholder DLL")
    proton_integrity.mark_damaged(root, "earlier")

    assert proton_health.check_after_failure({"PROTONPATH": str(root)}) is None
    assert toasts == []


def test_no_protonpath_is_a_no_op(toasts):
    assert proton_health.check_after_failure(None) is None
    assert proton_health.check_after_failure({"PROTONPATH": "/nonexistent"}) is None
    assert toasts == []


async def _run_with_rc(monkeypatch, rc):
    checked = []

    async def _once(*_a, **_kw):
        return rc

    monkeypatch.setattr(ur, "_run_umu_once", _once)
    monkeypatch.setattr(ur, "check_after_failure", checked.append)
    env = {"PROTONPATH": "/p"}
    await ur.run_umu_with_retry(["umu-run"], env=env, max_attempts=1)
    return checked


async def test_failed_run_checks_its_proton(monkeypatch):
    assert await _run_with_rc(monkeypatch, 1) == [{"PROTONPATH": "/p"}]


async def test_successful_run_checks_nothing(monkeypatch):
    assert await _run_with_rc(monkeypatch, 0) == []
