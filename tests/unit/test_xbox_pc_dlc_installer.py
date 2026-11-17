"""``XboxPcDlcInstaller``: stream the pending DLC of an installed Xbox PC game.

Nothing runs a real Xodus binary: ``FakeCli`` answers ``resolve-package``
per product and writes what a finished ``streaming`` run leaves.
"""
from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from _xbox_pc_fakes import PID, FakeCli, FakeProc, FakeStdout, happy_stdout

from unifideck.launcher.xbox_pc.dlc_records import dlc_entries, pending_dlc
from unifideck.launcher.xbox_pc.install_records import read_install_record, write_install_record
from unifideck.stores.microsoft.xodus import install_steps
from unifideck.stores.microsoft.xodus.dlc_installer import XboxPcDlcInstaller, package_in_place
from unifideck.stores.shared import cli_install_helpers as helpers

DLC_A = "9NB77CW5J1TT"
DLC_B = "9N77PDNB7CWZ"
CID_A = "bfedb5f5-cd3c-40b7-9b43-f169b55cbc78"
CID_B = "31c01c74-1cff-4912-a66b-5422714fc29f"
CIDS = {DLC_A: CID_A, DLC_B: CID_B}


def dlc_answer(pid: str, *, parent: str = PID) -> dict[str, Any]:
    """A ``resolve-package --include-dlc`` answer for one DLC (measured shape)."""
    return {"schema": 1, "rootProductId": pid, "market": "IN", "status": "choice", "choices": [
        {"contentId": CIDS[pid], "kind": "Durable", "packageFormat": "MSIXVC",
         "platform": "Windows.Desktop", "productId": pid, "supported": True,
         "parentProductId": parent, "mainPackageFamilyName": "Example.Base_hash", "title": pid},
    ]}


def write_dlc(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "MicrosoftGame.Config").write_text("<Game/>")
    (folder / ".xodus-streaming.msixvc").write_bytes(b"\0" * 8)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))

    async def fake_terminate(proc: Any, _prefix: str, **_kw: Any) -> None:
        return None

    monkeypatch.setattr(helpers, "terminate_process_tree", fake_terminate)

    @contextlib.contextmanager
    def fake_components(**_kw: Any) -> Iterator[object]:
        yield object()

    monkeypatch.setattr(install_steps, "reading_components", fake_components)
    root = tmp_path / "games" / "Xbox" / PID
    (root / "game").mkdir(parents=True)
    return {"tmp": tmp_path, "root": root}


def record(root: Path, **dlc: str) -> dict[str, Any]:
    rec = {"schema": 1, "productId": PID, "state": "installed", "market": "IN",
           "installRoot": str(root), "gameDir": str(root / "game"),
           "dlc": {pid: {"state": state, "title": f"T-{pid}"} for pid, state in dlc.items()}}
    write_install_record(rec)
    return rec


def cli(env: dict[str, Any], **kw: Any) -> FakeCli:
    kw.setdefault("resolve", lambda pid, include_dlc: dlc_answer(pid))
    kw.setdefault("on_spawn", write_dlc)
    return FakeCli(env["tmp"], **kw)


async def test_every_pending_dlc_is_streamed_into_its_own_folder(env: dict[str, Any]) -> None:
    fake = cli(env)
    rec = record(env["root"], **{DLC_A: "selected", DLC_B: "failed", "9N5PQG53S4SW": "declined"})
    out, run = await XboxPcDlcInstaller(fake).install_pending(rec, env["root"])
    assert run.installed == [DLC_B, DLC_A] and run.failed == []
    assert fake.resolved == [(DLC_B, True), (DLC_A, True)]
    assert [(p, Path(d).relative_to(env["root"]).as_posix(), c) for p, d, _m, c in fake.spawned] == [
        (DLC_B, f"dlc/{DLC_B}", CID_B), (DLC_A, f"dlc/{DLC_A}", CID_A),
    ]
    saved = read_install_record(PID)
    assert saved is not None and pending_dlc(saved) == [] and saved == out
    assert dlc_entries(saved)[DLC_A]["contentId"] == CID_A
    assert dlc_entries(saved)["9N5PQG53S4SW"]["state"] == "declined"


async def test_a_dlc_of_another_game_is_never_installed(env: dict[str, Any]) -> None:
    fake = cli(env, resolve=lambda pid, include_dlc: dlc_answer(pid, parent="9CCCCCCCCCCC"))
    rec = record(env["root"], **{DLC_A: "selected"})
    _out, run = await XboxPcDlcInstaller(fake).install_pending(rec, env["root"])
    assert run.failed == [DLC_A] and run.failed_titles == [f"T-{DLC_A}"] and fake.spawned == []
    saved = read_install_record(PID)
    assert saved is not None and dlc_entries(saved)[DLC_A]["state"] == "failed"
    assert dlc_entries(saved)[DLC_A]["error"] == "xbox_dlc_unavailable"


async def test_one_failure_does_not_stop_the_others(env: dict[str, Any]) -> None:
    procs = iter([
        FakeProc(FakeStdout(happy_stdout()[:-1]), rc=1),  # B dies before "complete"
        FakeProc(FakeStdout(happy_stdout())),
    ])
    fake = cli(env, make_proc=lambda: next(procs))
    rec = record(env["root"], **{DLC_A: "selected", DLC_B: "selected"})
    _out, run = await XboxPcDlcInstaller(fake).install_pending(rec, env["root"])
    assert run.failed == [DLC_B] and run.installed == [DLC_A]
    saved = read_install_record(PID)
    assert saved is not None and pending_dlc(saved) == [DLC_B]
    assert saved["state"] == "installed", "the game itself is untouched"


async def test_a_finished_run_without_package_files_is_a_failure(env: dict[str, Any]) -> None:
    fake = cli(env, on_spawn=lambda folder: folder.mkdir(parents=True, exist_ok=True))
    rec = record(env["root"], **{DLC_A: "selected"})
    _out, run = await XboxPcDlcInstaller(fake).install_pending(rec, env["root"])
    assert run.failed == [DLC_A]


async def test_cancel_leaves_the_dlc_partial(env: dict[str, Any]) -> None:
    fake = cli(env, make_proc=lambda: FakeProc(FakeStdout(happy_stdout()[:3], hang_at_end=True)))
    rec = record(env["root"], **{DLC_A: "selected"})
    task = asyncio.ensure_future(XboxPcDlcInstaller(fake).install_pending(rec, env["root"]))
    for _ in range(50):
        await asyncio.sleep(0.01)
        if fake.spawned:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    saved = read_install_record(PID)
    assert saved is not None and dlc_entries(saved)[DLC_A]["state"] == "partial"
    assert pending_dlc(saved) == [DLC_A], "a cancelled DLC still blocks Play"


async def test_nothing_pending_runs_nothing(env: dict[str, Any]) -> None:
    fake = cli(env)
    rec = record(env["root"], **{DLC_A: "installed"})
    _out, run = await XboxPcDlcInstaller(fake).install_pending(rec, env["root"])
    assert run.installed == [] and fake.resolved == []


def test_package_in_place(tmp_path: Path) -> None:
    assert not package_in_place(tmp_path / "missing")
    (tmp_path / "AppxManifest.xml").write_text("x")
    assert package_in_place(tmp_path)
