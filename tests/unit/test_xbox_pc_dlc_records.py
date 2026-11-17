"""``dlc_records``: the DLC map of an Xbox PC install record."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from unifideck.launcher.xbox_pc.dlc_records import (
    dlc_dir_for,
    dlc_entries,
    installed_dlc_roots,
    pending_dlc,
    pending_titles,
    with_dlc_state,
)

A, B, C, D, E = "9NB77CW5J1TT", "9N77PDNB7CWZ", "9N5PQG53S4SW", "9MX7735D2RQC", "9N1GHC0V27BP"


def record(root: Path, **dlc: dict[str, Any]) -> dict[str, Any]:
    return {"schema": 1, "productId": "C2KDNLT2H7DM", "state": "installed",
            "installRoot": str(root), "dlc": dlc}


def test_pending_is_selected_partial_or_failed(tmp_path: Path) -> None:
    rec = record(tmp_path, **{
        A: {"state": "selected", "title": "A"}, B: {"state": "partial"},
        C: {"state": "failed", "title": "C"}, D: {"state": "installed"}, E: {"state": "declined"},
    })
    assert pending_dlc(rec) == [C, B, A]  # sorted by product id
    assert pending_titles(rec) == ["C", B, "A"]


def test_bad_entries_are_ignored(tmp_path: Path) -> None:
    rec = record(tmp_path, **{A: {"state": "weird"}, "bad-id": {"state": "selected"}, B: "x"})
    assert dlc_entries(rec) == {} and pending_dlc(rec) == []
    assert dlc_entries({"dlc": []}) == {} and dlc_entries(None) == {}


def test_only_installed_folders_that_exist_are_roots(tmp_path: Path) -> None:
    rec = record(tmp_path, **{
        A: {"state": "installed"}, B: {"state": "installed"}, C: {"state": "partial"},
    })
    dlc_dir_for(tmp_path, A).mkdir(parents=True)
    dlc_dir_for(tmp_path, C).mkdir(parents=True)
    assert installed_dlc_roots(rec) == [tmp_path / "dlc" / A]


def test_no_install_root_gives_no_roots() -> None:
    assert installed_dlc_roots({"dlc": {A: {"state": "installed"}}}) == []


def test_with_dlc_state_keeps_other_fields_and_entries(tmp_path: Path) -> None:
    rec = record(tmp_path, **{A: {"state": "selected", "title": "A"}, B: {"state": "installed"}})
    out = with_dlc_state(rec, A, "installed", contentId="cid")
    assert out["dlc"][A]["state"] == "installed" and out["dlc"][A]["title"] == "A"
    assert out["dlc"][A]["contentId"] == "cid" and out["dlc"][B]["state"] == "installed"
    assert rec["dlc"][A]["state"] == "selected"  # the input is not changed


@pytest.mark.parametrize(("pid", "state"), [(A, "nope"), ("../x", "installed")])
def test_with_dlc_state_refuses_bad_input(tmp_path: Path, pid: str, state: str) -> None:
    with pytest.raises(ValueError):
        with_dlc_state(record(tmp_path), pid, state)


def test_dlc_dir_refuses_a_path_as_id(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        dlc_dir_for(tmp_path, "../../etc")
