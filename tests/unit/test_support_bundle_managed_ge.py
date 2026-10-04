"""Support bundle: the ``managed_ge`` probe and ``managed_ge_complete`` check.

Field report (v0.7.5): a tester's GE-Proton11-7 had an executable ``proton``
but no usable ``toolmanifest.vdf``. Every game and the Ubisoft login failed
with umu's ``KeyError: 'manifest'``, and the bundle only listed the build by
name, so nothing in ``diagnostics.txt`` pointed at it. The check must FAIL
on exactly that state and name the path and the missing piece.
"""
from __future__ import annotations

import stat
from pathlib import Path
from typing import Any

from unifideck.launcher.proton.infrastructure import ge_marker
from unifideck.services.support_bundle import checks_proton, probe_proton
from unifideck.services.support_bundle.check_kit import View
from unifideck.utils import vdf_compat

_TAG = "GE-Proton11-7"


def _tool(root: Path, *, manifest: bool = True) -> Path:
    """A GE tool dir; ``manifest=False`` reproduces the field state."""
    tool = root / _TAG
    (tool / "files" / "bin").mkdir(parents=True, exist_ok=True)
    (tool / "files" / "bin" / "wine").write_text("#!/bin/sh\n")
    (tool / "version").write_text(f"{_TAG}\n")
    proton = tool / "proton"
    proton.write_text("#!/bin/sh\n")
    proton.chmod(proton.stat().st_mode | stat.S_IXUSR)
    if manifest:
        (tool / "toolmanifest.vdf").write_text('"manifest" { "commandline" "" }\n')
    return tool


def _probe(tmp_path, monkeypatch, tag: str | None) -> dict[str, Any]:
    root = tmp_path / "compatibilitytools.d"
    root.mkdir(exist_ok=True)
    monkeypatch.setattr(vdf_compat, "STEAM_COMPAT_ROOTS", (str(root),))
    monkeypatch.setattr(ge_marker, "read_cached_latest_tag", lambda: tag)
    return root


def _verdict(block: dict[str, Any] | None):
    view = View.__new__(View)
    view.env = {"managed_ge": block} if block is not None else {}
    view.by_key = {}
    return checks_proton.check_managed_ge(view)


# ── probe ─────────────────────────────────────────────────────────────


def test_probe_reports_the_missing_manifest(tmp_path, monkeypatch):
    root = _probe(tmp_path, monkeypatch, _TAG)
    tool = _tool(root, manifest=False)

    block = probe_proton.managed_ge_block()

    assert block["tag"] == _TAG
    assert block["dirs"] == [{
        "path": str(tool),
        "problem": "toolmanifest.vdf missing, empty or unreadable",
    }]


def test_probe_reports_a_complete_build(tmp_path, monkeypatch):
    root = _probe(tmp_path, monkeypatch, _TAG)
    tool = _tool(root)

    assert probe_proton.managed_ge_block() == {
        "tag": _TAG, "dirs": [{"path": str(tool), "problem": None}],
    }


def test_probe_reports_a_symlinked_root_once(tmp_path, monkeypatch):
    """``~/.steam/root`` and ``~/.steam/steam`` usually point at one dir."""
    root = _probe(tmp_path, monkeypatch, _TAG)
    _tool(root)
    alias = tmp_path / "alias"
    alias.symlink_to(root)
    monkeypatch.setattr(vdf_compat, "STEAM_COMPAT_ROOTS", (str(root), str(alias)))

    assert len(probe_proton.managed_ge_block()["dirs"]) == 1


def test_probe_without_a_recorded_tag(tmp_path, monkeypatch):
    _probe(tmp_path, monkeypatch, None)
    assert probe_proton.managed_ge_block() == {"tag": None, "dirs": []}


# ── verdict ───────────────────────────────────────────────────────────


def test_verdict_fails_and_names_path_and_problem():
    verdict = _verdict({"tag": _TAG, "dirs": [
        {"path": "/x/GE-Proton11-7", "problem": "toolmanifest.vdf missing, empty or unreadable"},
    ]})
    assert verdict.status == "fail"
    assert "/x/GE-Proton11-7" in verdict.detail
    assert "toolmanifest.vdf" in verdict.detail


def test_verdict_passes_when_any_copy_is_complete():
    verdict = _verdict({"tag": _TAG, "dirs": [
        {"path": "/a/GE-Proton11-7", "problem": "files/bin/wine missing"},
        {"path": "/b/GE-Proton11-7", "problem": None},
    ]})
    assert verdict.status == "pass"
    assert "/b/GE-Proton11-7" in verdict.detail


def test_verdict_warns_when_recorded_but_absent():
    assert _verdict({"tag": _TAG, "dirs": []}).status == "warn"


def test_verdict_na_without_tag_or_data():
    assert _verdict({"tag": None, "dirs": []}).status == "na"
    assert _verdict(None).status == "na"
    assert _verdict({"error": "RuntimeError()"}).status == "na"
