"""``XodusCli``: the only plugin module that runs xodus-cli.

Every test installs a fake backend (a small Python ``xodus-cli``) under a
temporary ``XDG_DATA_HOME`` and runs the real ``XodusCli`` against it, so
the verified-backend lookup, the environment, the timeouts and the error
classes are all exercised for real. No network, no real Xodus.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest
from _xodus_backend_fakes import cli_path, install_backend

from unifideck.launcher.xbox_pc import backend, layout
from unifideck.launcher.xbox_pc.component_locks import game_lock
from unifideck.stores.microsoft.xodus import cli as cli_mod
from unifideck.stores.microsoft.xodus.cli import XodusCli, XodusCliError, classify_output, raise_for

PID = "9PK087LNGJC5"


@pytest.fixture(autouse=True)
def data_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    backend._DIGEST_CACHE.clear()
    return tmp_path


def _cli() -> XodusCli:
    return XodusCli(env_base={"PATH": os.environ.get("PATH", ""), "HOME": "/nonexistent"})


ACCOUNT_SCRIPT = """
import json, os, sys
if sys.argv[1:] == ["account", "--json"]:
    print(json.dumps({"stored": True, "username": os.environ["XODUS_CREDENTIAL_DIR"]}))
    sys.exit(0)
sys.exit(3)
"""


async def test_account_uses_the_verified_backend_and_private_credentials() -> None:
    install_backend(ACCOUNT_SCRIPT)
    status = await _cli().account()
    assert status.stored is True
    cred = layout.credential_dir()
    assert status.username == str(cred)
    assert cred.is_dir() and (cred.stat().st_mode & 0o777) == 0o700


async def test_never_uses_path(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "xodus-cli").write_text("#!/bin/sh\necho '{\"stored\": true}'\n")
    (fake_bin / "xodus-cli").chmod(0o755)
    cli = XodusCli(env_base={"PATH": str(fake_bin)})
    with pytest.raises(XodusCliError) as err:
        await cli.account()
    assert err.value.code == "backend_missing"
    assert (await cli.library("IN")).status == "not_ready"


async def test_tampered_binary_is_refused() -> None:
    active = install_backend(ACCOUNT_SCRIPT)
    path = cli_path(active)
    path.write_text(path.read_text() + "\n# changed\n")
    with pytest.raises(XodusCliError) as err:
        await _cli().account()
    assert err.value.code == "backend_tampered"


@pytest.mark.parametrize("stderr, rc, code", [
    ("thread 'main' panicked at login.rs:15: EOF while parsing a value", 101, "empty_response"),
    ("thread 'main' panicked at package.rs:22: boom", 101, "panic"),
    ("NEXUS_CREDENTIAL_ERROR_V1 keyring-invalid-data\nStored Xodus credentials are empty", 1, "credential_keyring_invalid_data"),
    ("plain failure", 4, "exit_4"),
])
async def test_failures_are_classified(stderr: str, rc: int, code: str) -> None:
    install_backend(f"""
import sys
sys.stderr.write({stderr!r} + "\\n")
sys.exit({rc})
""")
    with pytest.raises(XodusCliError) as err:
        await _cli().account()
    assert err.value.code == code
    assert str(err.value).startswith(f"xodus_{code}")


async def test_empty_stdout_is_an_empty_response() -> None:
    install_backend("import sys\nsys.exit(0)\n")
    with pytest.raises(XodusCliError) as err:
        await _cli().account()
    assert err.value.code == "empty_response"


def test_tail_is_redacted() -> None:
    out = classify_output(2, b"", b"signed in as someone@example.com with " + b"T" * 60 + b"\n")
    assert "@" not in out.tail and "TTTT" not in out.tail
    with pytest.raises(XodusCliError) as err:
        raise_for(out)
    assert "example.com" not in str(err.value)


async def test_timeout_kills_the_whole_tree(data_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    marker = data_home / "child.pid"
    install_backend(f"""
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
open({str(marker)!r}, "w").write(str(child.pid))
time.sleep(60)
""")
    monkeypatch.setattr(cli_mod, "ACCOUNT_TIMEOUT_S", 1.0)
    started = time.monotonic()
    with pytest.raises(XodusCliError) as err:
        await _cli().account()
    assert err.value.code == "timeout"
    assert time.monotonic() - started < 20
    child = int(marker.read_text())
    for _ in range(50):
        if not _alive(child):
            break
        await asyncio.sleep(0.1)
    assert not _alive(child)


def _alive(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rpartition(")")[2].split()[0] != "Z"


async def test_library_reads_a_line_over_64_kib() -> None:
    install_backend("""
import json, sys
assert sys.argv[1:] == ["library", "--json", "--market", "IN"], sys.argv
print("note: warming up")
print(json.dumps({"schema": 1, "account": "a@b.co", "rows": ["x" * 200] * 800}))
""")
    read = await _cli().library("IN")
    assert read.status == "ok"
    assert len(read.stdout) > 65536 and json.loads(read.stdout)["schema"] == 1
    assert "a@b.co" not in repr(read)


async def test_library_signed_out_and_failed() -> None:
    install_backend("""
import sys
sys.stderr.write("NEXUS_CREDENTIAL_ERROR_V1 keyring-missing-data\\n")
sys.exit(1)
""")
    read = await _cli().library("IN")
    assert read.status == "signed_out"


async def test_library_refuses_a_bad_market() -> None:
    with pytest.raises(ValueError):
        await _cli().library("india")


async def test_library_and_profile_wait_while_a_game_runs() -> None:
    install_backend("import sys\nprint('{}')\n")
    cli = _cli()
    with game_lock(PID) as held:
        assert held
        assert (await cli.library("IN")).status == "busy"
        with pytest.raises(XodusCliError) as err:
            await cli.profile()
        assert err.value.code == "busy_game_running"


async def test_library_is_busy_while_the_credential_lock_is_held() -> None:
    install_backend("import sys\nprint('{}')\n")
    cli = _cli()
    async with cli.credential_lock:
        assert (await cli.library("IN")).status == "busy"


async def test_account_and_logout_answer_busy_while_a_download_holds_the_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_mod, "CREDENTIAL_WAIT_S", 0.05)
    install_backend(ACCOUNT_SCRIPT)
    cli = _cli()
    async with cli.credential_lock:
        for call in (cli.account, cli.logout, cli.profile):
            with pytest.raises(XodusCliError) as err:
                await call()
            assert str(err.value) == "xodus_busy"
    assert (await cli.account()).stored is True, "the lock is free again"


async def test_streaming_runs_the_backend_the_caller_locked(tmp_path: Path) -> None:
    """A promote between the lock and the spawn must not swap the binary."""
    active = install_backend("print('NEXUS_PROGRESS {}')\n")
    (layout.data_root() / "active.json").unlink()
    cli = _cli()
    async with cli.credential_lock:
        proc = await cli.spawn_streaming(PID, tmp_path / "game", "IN", "1760268b-59d2-488b-8f68-b864a82b23be", active)
        out, _err = await proc.communicate()
    assert out.decode().strip() == "NEXUS_PROGRESS {}"


async def test_credential_calls_are_serialised(data_home: Path) -> None:
    log = data_home / "calls.log"
    install_backend(f"""
import json, time
with open({str(log)!r}, "a") as f:
    f.write(f"start {{time.monotonic()}}\\n")
time.sleep(0.4)
with open({str(log)!r}, "a") as f:
    f.write(f"end {{time.monotonic()}}\\n")
print(json.dumps({{"stored": False}}))
""")
    cli = _cli()
    await asyncio.gather(cli.account(), cli.account())
    kinds = [line.split()[0] for line in log.read_text().splitlines()]
    assert kinds == ["start", "end", "start", "end"]


async def test_profile_and_resolve_package() -> None:
    install_backend("""
import json, sys
if sys.argv[1] == "profile":
    print(json.dumps({"schema": 1, "account": "a@b.co", "gamertag": "Gamer", "gamerpicDataUrl": None}))
elif sys.argv[1] == "resolve-package":
    print(json.dumps({"schema": 1, "status": "ready", "args": sys.argv[2:]}))
""")
    cli = _cli()
    profile = await cli.profile()
    assert profile.gamertag == "Gamer" and profile.has_picture is False
    plan = await cli.resolve_package(PID, "IN")
    assert plan["args"] == [PID, "--json", "--market", "IN"]


async def test_spawn_streaming_keeps_stderr_apart(tmp_path: Path) -> None:
    install_backend("""
import sys
print("NEXUS_PROGRESS {}")
sys.stderr.write("NEXUS_CONTENT_LICENSE_DIAGNOSTIC_V1 content-parsed 200\\n")
""")
    cli = _cli()
    async with cli.credential_lock:
        proc = await cli.spawn_streaming(PID, tmp_path / "game", "IN", "1760268b-59d2-488b-8f68-b864a82b23be")
        out, err = await proc.communicate()
    assert out.decode().strip() == "NEXUS_PROGRESS {}"
    assert "LICENSE_DIAGNOSTIC" in err.decode()
    assert proc.returncode == 0


async def test_logout_passes_device_flag(data_home: Path) -> None:
    seen = data_home / "argv.json"
    install_backend(f"""
import json, sys
open({str(seen)!r}, "w").write(json.dumps(sys.argv[1:]))
""")
    await _cli().logout(device=True)
    assert json.loads(seen.read_text()) == ["logout", "--device"]


async def test_a_failed_exec_is_spawn_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    install_backend(ACCOUNT_SCRIPT)

    async def refuse(*_a: object, **_k: object) -> None:
        raise OSError(8, "Exec format error")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", refuse)
    with pytest.raises(XodusCliError) as err:
        await _cli().account()
    assert err.value.code == "spawn_failed"
    assert "Exec format error" in err.value.tail
