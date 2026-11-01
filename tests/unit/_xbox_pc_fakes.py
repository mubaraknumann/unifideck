"""Fakes for the Xbox PC install, update and uninstall tests.

tests/unit/_xbox_pc_fakes.py

Nothing here runs a real Xodus binary or touches a real user path. The
fake process has separate stdout and stderr streams, like the real
``spawn_streaming`` (stderr is never merged into stdout).

Naming follows ``_repo_root.py``: leading underscore so pytest does not
collect it as a test module.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

PID = "9PK087LNGJC5"
CID = "1760268b-59d2-488b-8f68-b864a82b23be"
XBOX_CID = "3e7fcb43-8e2e-40a4-83ef-e7a57af5a6ae"

GAME_CONFIG = """<?xml version="1.0" encoding="utf-8"?>
<Game configVersion="1">
  <Identity Name="PlayStack.Balatro" Publisher="CN=X" Version="{version}" />
  <ShellVisuals DefaultDisplayName="Balatro" />
  <ExecutableList><Executable Name="love.exe" TargetDeviceFamily="PC" Id="Balatro" /></ExecutableList>
  <TitleId>6adc6be5</TitleId>
</Game>
"""


def plan(*choices: dict[str, Any], status: str = "ready", root: str = PID) -> dict[str, Any]:
    """A ``resolve-package`` answer (the measured Balatro shape by default)."""
    if not choices:
        choices = (game_choice(CID), game_choice(XBOX_CID, platform="Windows.Xbox",
                                                  supported=False, fmt="XVC"))
    return {"schema": 1, "rootProductId": root, "market": "IN", "status": status,
            "choices": list(choices)}


def game_choice(cid: str, *, kind: str = "Game", platform: str = "Windows.Desktop",
                supported: bool = True, fmt: str = "MSIXVC") -> dict[str, Any]:
    """One ``choices`` entry."""
    return {"contentId": cid, "kind": kind, "packageFormat": fmt, "platform": platform,
            "productId": PID, "supported": supported, "title": "Balatro"}


def record_line(seq: int, phase: str, done: int = 0, total: int | None = None,
                rate: int = 0) -> bytes:
    """One ``NEXUS_PROGRESS`` stdout line."""
    rec = {"schema": 1, "event": "progress", "sequence": seq, "phase": phase,
           "paused": False, "canPause": phase == "downloading",
           "completedBytes": done, "totalBytes": total, "bytesPerSecond": rate}
    return f"NEXUS_PROGRESS {json.dumps(rec)}\n".encode()


def happy_stdout(total: int = 1000) -> list[bytes]:
    """The phases of a clean run, as the spike measured them."""
    return [
        record_line(0, "initializing"), record_line(1, "metadata"),
        b"INFO xodus: downloading package\n",
        record_line(9, "downloading", 0, total), record_line(20, "downloading", total // 2, total, 50),
        record_line(50, "finalizing", total, total), record_line(51, "complete", total, total),
    ]


class FakeStdout:
    """``readline`` over scripted lines. ``(delay, line)`` items sleep first."""

    def __init__(self, lines: list[Any], *, hang_at_end: bool = False,
                 exc: BaseException | None = None) -> None:
        self._lines = list(lines)
        self._hang = hang_at_end
        self._exc = exc

    async def readline(self) -> bytes:
        if self._exc is not None and not self._lines:
            raise self._exc
        if not self._lines:
            if self._hang:
                await asyncio.sleep(3600)
            return b""
        item = self._lines.pop(0)
        if isinstance(item, tuple):
            await asyncio.sleep(item[0])
            item = item[1]
        return item


class FakeStderr:
    """``read(n)`` over scripted chunks, then EOF."""

    def __init__(self, chunks: list[bytes] | None = None) -> None:
        self._chunks = list(chunks or [])

    async def read(self, _n: int = -1) -> bytes:
        await asyncio.sleep(0)
        return self._chunks.pop(0) if self._chunks else b""


class FakeProc:
    """A started streaming process with separate stdout and stderr pipes."""

    def __init__(self, stdout: FakeStdout, stderr: FakeStderr | None = None, *,
                 rc: int = 0, hang_exit: bool = False) -> None:
        self.stdout = stdout
        self.stderr = stderr or FakeStderr()
        self.pid = 4242
        self.returncode: int | None = None
        self._rc = rc
        self._hang_exit = hang_exit

    async def wait(self) -> int:
        if self._hang_exit:
            await asyncio.sleep(3600)
        self.returncode = self._rc
        return self._rc


def write_game(game_dir: Path, *, version: str = "1.0.5.0", container: bool = True,
               exe: bool = True, config_name: str = "MicrosoftGame.Config") -> None:
    """What a finished ``streaming`` run leaves in the game folder."""
    game_dir.mkdir(parents=True, exist_ok=True)
    (game_dir / config_name).write_text(GAME_CONFIG.format(version=version))
    if exe:
        (game_dir / "love.exe").write_bytes(b"MZ" + b"\0" * 64)
    if container:
        (game_dir / ".xodus-streaming.msixvc").write_bytes(b"\0" * 32)


class FakeXboxPc:
    """The ``XboxPc`` facade surface the install steps read."""

    def __init__(self, *, enabled: bool = True, runtime: str = "installed",
                 account: str = "signed_in", market: str | None = "IN",
                 checked_account: str | None = None) -> None:
        self.on = enabled
        self.runtime = runtime
        self.account = account
        self.market = market
        #: What asking Xodus finds for an ``unknown`` account.
        self.checked_account = checked_account
        self.checks = 0

    def enabled(self) -> bool:
        return self.on

    async def status(self) -> dict[str, Any]:
        return {"enabled": self.on, "runtime": {"state": self.runtime},
                "account": {"state": self.account}}

    async def checked_status(self) -> dict[str, Any]:
        if self.account == "unknown" and self.checked_account is not None:
            self.checks += 1
            self.account = self.checked_account
        return await self.status()

    def market_hint(self) -> str | None:
        return self.market


class FakeCliError(Exception):
    """Shaped like ``XodusCliError``: a ``code`` and ``xodus_<code>`` text."""

    def __init__(self, code: str, tail: str = "") -> None:
        self.code = code
        super().__init__(f"xodus_{code}: {tail}" if tail else f"xodus_{code}")


class FakeCli:
    """The ``XodusCli`` surface the install steps use.

    ``on_spawn(dest)`` runs when streaming starts, so a test can write the
    files a real run would leave. ``make_proc()`` builds the process.
    """

    def __init__(self, tmp: Path, *, resolve: Any = None,
                 make_proc: Callable[[], FakeProc] | None = None,
                 on_spawn: Callable[[Path], None] | None = None) -> None:
        self.credential_lock = asyncio.Lock()
        self._cred = tmp / "creds"
        self.resolve = plan() if resolve is None else resolve
        self.make_proc = make_proc or (lambda: FakeProc(FakeStdout(happy_stdout())))
        self.on_spawn = on_spawn or write_game
        self.spawned: list[tuple[str, str, str, str]] = []
        self.backend_error: Exception | None = None
        self.service = tmp / "xodus-service"

    def credential_dir(self) -> Path:
        self._cred.mkdir(mode=0o700, exist_ok=True)
        return self._cred

    async def backend(self) -> Any:
        if self.backend_error is not None:
            raise self.backend_error
        return type("Paths", (), {"service": self.service, "cli": self.service})()

    async def resolve_package(self, pid: str, market: str) -> dict[str, Any]:
        if isinstance(self.resolve, Exception):
            raise self.resolve
        return self.resolve

    async def spawn_streaming(self, pid: str, dest: str, market: str, content_id: str,
                              active: Any = None) -> Any:
        assert self.credential_lock.locked(), "streaming must hold the credential lock"
        assert active is not None, "streaming runs the backend the locks hold"
        self.spawned.append((pid, dest, market, content_id))
        self.on_spawn(Path(dest))
        return self.make_proc()


class FakeBus:
    """Records emitted events."""

    def __init__(self) -> None:
        self.events: list[tuple[Any, dict[str, Any]]] = []

    async def emit(self, event: Any, **kwargs: Any) -> None:
        self.events.append((event, kwargs))


def title_info(**overrides: Any) -> dict[str, Any]:
    """The library's ``metadata["xbox_pc"]`` of a ready, owned title."""
    info = {"v": 1, "state": "ready", "reason": None, "access": "owned", "plan_id": None,
            "product_id": PID, "target_id": PID, "content_id": CID, "choices": 1,
            "size_bytes": 105408295, "market": "IN", "end_date": None}
    info.update(overrides)
    return info
