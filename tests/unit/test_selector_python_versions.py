"""The umu interpreter is any system Python 3.10 or newer.

Bazzite 45 (Fedora 45) moved ``/usr/bin/python3`` to 3.15. The selector held a
closed list ending at 3.14, so ``find_python_3_10_plus`` raised
``DependencyMissingError`` and every Proton launch failed, Ubisoft sign-in
included, while umu itself ran fine on 3.15 (support bundle 2026-11-13).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from unifideck.launcher.proton.infrastructure import selector


@pytest.mark.parametrize(
    ("version", "ok"),
    [("3.9", False), ("3.10", True), ("3.14", True), ("3.15", True),
     ("3.16", True), ("4.0", True), ("2.7", False), ("garbage", False)],
)
def test_any_python_from_3_10_is_accepted(version: str, ok: bool):
    assert selector._accepted(version) is ok


def test_the_running_system_interpreter_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    # Any file under /usr stands in for the system interpreter; the version
    # check reads ``sys.version_info``, which is this test's own (>= 3.10).
    exe = Path("/usr/bin/env")
    monkeypatch.setattr(selector.sys, "executable", str(exe))
    monkeypatch.setattr(selector, "PYTHON_CANDIDATES", [])  # would raise
    assert selector.find_python_3_10_plus() == exe


def test_a_non_system_interpreter_falls_back_to_the_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Decky's bundled Python (/tmp/_MEI…) or a venv is never handed to umu."""
    monkeypatch.setattr(selector.sys, "executable", str(tmp_path / "_MEI123/python"))
    fake = tmp_path / "python3.15"
    fake.write_text("#!/bin/sh\necho 3.15\n")
    fake.chmod(0o755)
    monkeypatch.setattr(selector, "PYTHON_CANDIDATES", [str(fake)])
    assert selector.find_python_3_10_plus() == fake
