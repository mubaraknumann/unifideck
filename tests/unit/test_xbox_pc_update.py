"""Xbox PC update and update check.

The updater refuses while the game runs or another download writes the
folder, and leaves the record ``updating`` until the same checks an install
passes. The update check talks to a fake ``xodus-service`` (a small Python
script bound to the private ``XDG_RUNTIME_DIR``), never the real binary.
"""
from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
import shutil
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from _xbox_pc_fakes import (
    CID,
    PID,
    FakeCli,
    FakeProc,
    FakeStdout,
    FakeXboxPc,
    record_line,
    write_game,
)

from unifideck.launcher.xbox_pc.component_locks import game_lock
from unifideck.launcher.xbox_pc.install_records import read_install_record, write_install_record
from unifideck.stores.microsoft.xodus import install_steps, update_service
from unifideck.stores.microsoft.xodus.package_source import PACKAGE_SOURCE
from unifideck.stores.microsoft.xodus.update_service import (
    ServiceError,
    encode_request,
    parse_reply,
    service_session,
)
from unifideck.stores.microsoft.xodus.updater import XboxPcUpdater
from unifideck.stores.microsoft.xodus.updates import XboxPcUpdates
from unifideck.stores.shared import cli_install_helpers as helpers

OTHER = "9NBLGGH4R315"


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr("unifideck.utils.paths.get_games_map_path",
                        lambda config=None: str(tmp_path / "games.map"))

    async def fake_terminate(proc: Any, _prefix: str, **_kw: Any) -> None:
        if proc.returncode is None and hasattr(proc, "kill"):
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()

    monkeypatch.setattr(helpers, "terminate_process_tree", fake_terminate)
    monkeypatch.setattr(update_service, "terminate_process_tree", fake_terminate)

    @contextlib.contextmanager
    def fake_components(**_kw: Any) -> Iterator[object]:
        yield object()

    monkeypatch.setattr(install_steps, "reading_components", fake_components)
    return {"tmp": tmp_path, "root": tmp_path / "games" / "Xbox" / PID}


def _installed(root: Path, pid: str = PID, version: str = "1.0.5.0",
               state: str = "installed") -> None:
    write_game(root / "game", version=version)
    write_install_record({
        "schema": 1, "productId": pid, "state": state, "market": "IN", "contentId": CID,
        "installRoot": str(root), "gameDir": str(root / "game"),
        "exe": str(root / "game" / "love.exe"), "exeRelative": "love.exe",
        "version": version, "titleId": "6adc6be5", "startedAt": 1.0, "installedAt": 2.0,
    })


# ── updater ─────────────────────────────────────────────────────────────
async def test_an_update_streams_into_the_same_folder(env: dict[str, Any]) -> None:
    _installed(env["root"])
    cli = FakeCli(env["tmp"], on_spawn=lambda d: write_game(d, version="1.0.6.0"))
    result = await XboxPcUpdater(FakeXboxPc(), cli).update(PID)
    assert result.success and result.install_path == str(env["root"])
    assert cli.spawned == [(PID, str(env["root"] / "game"), "IN", CID)]
    record = read_install_record(PID)
    assert record is not None and record["state"] == "installed"
    assert record["version"] == "1.0.6.0" and "updatedAt" in record


async def test_an_update_refuses_while_the_game_runs(env: dict[str, Any]) -> None:
    _installed(env["root"])
    cli = FakeCli(env["tmp"])
    with game_lock(PID) as held:
        assert held
        result = await XboxPcUpdater(FakeXboxPc(), cli).update(PID)
    assert result.error_code == "xbox_game_running" and cli.spawned == []


async def test_an_update_refuses_while_a_download_writes_the_folder(env: dict[str, Any]) -> None:
    _installed(env["root"])
    cli = FakeCli(env["tmp"])
    fd = os.open(env["root"] / "game" / ".nexus-streaming.lock", os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        result = await XboxPcUpdater(FakeXboxPc(), cli).update(PID)
    finally:
        os.close(fd)
    assert result.error_code == "xbox_install_in_progress"


async def test_a_failed_update_stays_updating(env: dict[str, Any]) -> None:
    _installed(env["root"])
    failing = FakeCli(env["tmp"], on_spawn=lambda _d: None,
                      make_proc=lambda: FakeProc(FakeStdout([record_line(0, "failed")]), rc=1))
    result = await XboxPcUpdater(FakeXboxPc(), failing).update(PID)
    assert result.error_code == "xodus_exit_1"
    record = read_install_record(PID)
    assert record is not None and record["state"] == "updating"


@pytest.mark.parametrize(("xbox", "code"), [
    (FakeXboxPc(enabled=False), "xbox_pc_disabled"),
    (FakeXboxPc(account="signed_out"), "xbox_pc_not_signed_in"),
])
async def test_an_update_needs_the_feature_ready(env: dict[str, Any], xbox: FakeXboxPc,
                                                 code: str) -> None:
    _installed(env["root"])
    assert (await XboxPcUpdater(xbox, FakeCli(env["tmp"])).update(PID)).error_code == code


async def test_an_update_of_a_missing_game_is_not_located(env: dict[str, Any]) -> None:
    result = await XboxPcUpdater(FakeXboxPc(), FakeCli(env["tmp"])).update(PID)
    assert result.error_code == "install_not_located"


# ── wire format ─────────────────────────────────────────────────────────
def test_request_frame() -> None:
    frame = encode_request(PID, "1.0.5.0")
    body = f"<Request><ProductId>{PID}</ProductId><InstalledVersion>1.0.5.0</InstalledVersion></Request>"
    assert frame[:4] == (0x58445358).to_bytes(4, "little")
    assert frame[4:6] == (9).to_bytes(2, "little")
    assert frame[6:8] == len(body).to_bytes(2, "little") and frame[8:] == body.encode()


def _reply(hresult: int, checked: bool, latest: str = "", stage: str = "") -> bytes:
    return (f"<Response><HResult>{hresult}</HResult><Checked>{str(checked).lower()}</Checked>"
            f"<LatestVersion>{latest}</LatestVersion><Count>0</Count>"
            f"<ErrorStage>{stage}</ErrorStage></Response>").encode()


def test_reply_verdicts() -> None:
    assert parse_reply(_reply(0, True, "1.0.5.0")).available is False
    newer = parse_reply(_reply(0x80004001, False, "1.0.6.0", "version-comparison"))
    assert newer.available is True and newer.latest == "1.0.6.0"
    # The same HRESULT for "several editions" is not an update.
    assert parse_reply(_reply(0x80004001, False, "", "package-query")).available is None
    assert parse_reply(_reply(0x80070005, False, "", "package-query")).available is None
    assert parse_reply(b"<Response>").available is None


# ── a fake service on a real socket ─────────────────────────────────────
FAKE_SERVICE = r"""#!{python}
import os, signal, socket, struct, sys
if os.environ.get("FAKE_EXIT"):
    sys.stderr.write("NEXUS_CREDENTIAL_ERROR_V1 keyring-missing-data\n")
    sys.exit(1)
assert len(sys.argv) == 1, sys.argv
path = os.path.join(os.environ["XDG_RUNTIME_DIR"], "xodus.sock")
srv = socket.socket(socket.AF_UNIX)
srv.bind(path)
srv.listen(4)
signal.signal(signal.SIGINT, lambda *_: (os.unlink(path), sys.exit(0)))
newer = "{newer}"
while True:
    conn, _ = srv.accept()
    magic, kind, size = struct.unpack("<IHH", conn.recv(8))
    body = conn.recv(size).decode()
    pid = body.split("<ProductId>")[1].split("<")[0]
    if pid == newer:
        out = "<Response><HResult>2147500033</HResult><Checked>false</Checked><LatestVersion>9.0.0.0</LatestVersion><Count>0</Count><ErrorStage>version-comparison</ErrorStage></Response>"
    else:
        out = "<Response><HResult>0</HResult><Checked>true</Checked><LatestVersion>1.0.5.0</LatestVersion><Count>0</Count><ErrorStage></ErrorStage></Response>"
    data = out.encode()
    conn.sendall(struct.pack("<IHH", magic, kind + 1, len(data)) + data)
    conn.close()
"""


@pytest.fixture
def runtime_dir(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    # A short private folder: a unix socket path holds at most 107 bytes.
    folder = Path(tempfile.mkdtemp(prefix="uxt-"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(folder))
    yield folder
    shutil.rmtree(folder, ignore_errors=True)


def _service(tmp: Path, newer: str = OTHER) -> Path:
    script = tmp / "xodus-service"
    script.write_text(FAKE_SERVICE.replace("{python}", sys.executable).replace("{newer}", newer))
    script.chmod(0o755)
    return script


async def test_session_uses_a_private_runtime_dir_and_cleans_up(
    env: dict[str, Any], runtime_dir: Path,
) -> None:
    creds = env["tmp"] / "creds"
    creds.mkdir()
    async with service_session(_service(env["tmp"]), creds) as sock:
        folder = Path(sock).parent
        assert folder.parent == runtime_dir and folder.name.startswith("uxs-")
        assert len(os.fsencode(sock)) <= 107
        verdict = await update_service.query(sock, OTHER, "1.0.5.0")
        assert verdict.available is True
    assert not folder.exists()


async def test_a_service_that_exits_early_fails_fast(
    env: dict[str, Any], runtime_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    creds = env["tmp"] / "creds"
    creds.mkdir()
    started = time.monotonic()
    with pytest.raises(ServiceError, match="keyring-missing-data"):
        async with service_session(_service(env["tmp"]), creds,
                                   base_env={**os.environ, "FAKE_EXIT": "1"}):
            pass
    assert time.monotonic() - started < 10
    assert os.listdir(runtime_dir) == []


async def test_a_socket_path_over_the_limit_is_refused(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    deep = env["tmp"] / ("d" * 100)
    deep.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(deep))
    with pytest.raises(ServiceError, match="too long"):
        async with service_session(_service(env["tmp"]), env["tmp"]):
            pass
    assert os.listdir(deep) == []


# ── check_installed ─────────────────────────────────────────────────────
async def test_the_check_reports_newer_games_and_unfinished_updates(
    env: dict[str, Any], runtime_dir: Path,
) -> None:
    base = env["tmp"] / "games" / "Xbox"
    _installed(base / PID)
    _installed(base / OTHER, pid=OTHER)
    third = "9WZDNCRFJ3TJ"
    _installed(base / third, pid=third, state="updating")
    cli = FakeCli(env["tmp"])
    cli.service = _service(env["tmp"])
    assert await XboxPcUpdates(FakeXboxPc(), cli).check_installed() == sorted([OTHER, third])


async def test_nothing_starts_when_the_feature_is_off(env: dict[str, Any]) -> None:
    _installed(env["root"])
    cli = FakeCli(env["tmp"])
    cli.backend_error = AssertionError("must not be called")
    assert await XboxPcUpdates(FakeXboxPc(enabled=False), cli).check_installed() == []
    assert await XboxPcUpdates(FakeXboxPc(runtime="missing"), cli).check_installed() == []


async def test_no_records_means_no_service(env: dict[str, Any]) -> None:
    cli = FakeCli(env["tmp"])
    cli.backend_error = AssertionError("must not be called")
    assert await XboxPcUpdates(FakeXboxPc(), cli).check_installed() == []


async def test_a_running_download_gets_the_last_answer(env: dict[str, Any]) -> None:
    _installed(env["root"])
    cli = FakeCli(env["tmp"])
    cli.backend_error = AssertionError("must not be called")
    updates = XboxPcUpdates(FakeXboxPc(), cli)
    updates._last = [PID]
    async with cli.credential_lock:
        assert await updates.check_installed() == [PID]


async def test_a_failed_session_reports_nothing(
    env: dict[str, Any], runtime_dir: Path,
) -> None:
    _installed(env["root"])
    cli = FakeCli(env["tmp"])
    cli.service = env["tmp"] / "missing-binary"
    assert await XboxPcUpdates(FakeXboxPc(), cli).check_installed() == []



async def test_a_stub_title_is_checked_against_its_package_source(
    env: dict[str, Any], runtime_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Only OTHER has a newer package; PID streams OTHER's package.
    monkeypatch.setitem(PACKAGE_SOURCE, PID, OTHER)
    _installed(env["tmp"] / "games" / "Xbox" / PID)
    cli = FakeCli(env["tmp"])
    cli.service = _service(env["tmp"])
    assert await XboxPcUpdates(FakeXboxPc(), cli).check_installed() == [PID]


async def test_the_check_answers_in_row_ids(env: dict[str, Any], runtime_dir: Path) -> None:
    root = env["tmp"] / "games" / "Xbox" / OTHER
    _installed(root, pid=OTHER)
    record = read_install_record(OTHER)
    assert record is not None
    write_install_record(dict(record, storeGameId="xcloud-other"))
    cli = FakeCli(env["tmp"])
    cli.service = _service(env["tmp"])
    assert await XboxPcUpdates(FakeXboxPc(), cli).check_installed() == ["xcloud-other"]


async def test_a_cancel_during_the_service_stop_kills_and_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sweep's cancel must not be swallowed by the grace wait."""
    kills: list[int] = []

    async def fake_terminate(proc: Any, _prefix: str, **_kw: Any) -> None:
        kills.append(proc.pid)

    class _Slow:
        pid, returncode = 77, None

        def send_signal(self, _sig: int) -> None:
            return None

        async def wait(self) -> int:
            await asyncio.sleep(3600)
            return 0

    monkeypatch.setattr(update_service, "terminate_process_tree", fake_terminate)
    task = asyncio.ensure_future(update_service._stop(_Slow()))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert kills == [77]

