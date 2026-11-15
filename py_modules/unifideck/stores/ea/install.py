"""EA installs, updates and uninstalls through ``maxima-cli install``.

``maxima-cli install`` downloads one build into the game folder and prints
JSON-lines (``build``, ``progress``, ``done``, ``cancelled``). Every file is
checked against the build's checksum, and the ``.maxima-files`` ledger lets a
resumed or updated install skip files already verified, so an update is the
same command run over the existing folder. Touchup is not part of it: it needs
the game's prefix and runs at the prefix warmup (``launcher/ea/touchup.py``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from unifideck.core.manifest import write_manifest
from unifideck.core.safe_delete import safe_rmtree
from unifideck.core.types import InstallResult, Result
from unifideck.launcher.ea.records import InstallRecord, delete_record, read_record, write_record
from unifideck.stores.shared.cli_install_helpers import DrainedRun, TailRingBuffer, run_drained_cli

from .cli import MaximaCli, MaximaCliError
from .ids import STORE

logger = logging.getLogger(__name__)
_LOG = "[EA/install]"

ProgressCallback = Callable[[dict[str, Any]], Awaitable[None]] | None
EntryLookup = Callable[[str], Awaitable[dict[str, Any] | None]]

EXIT_WAIT_S = 60
#: The CLI prints progress every second, also while it checks existing files.
STALL_S = 300.0
_REGISTRY_PREFIX = re.compile(r"^\[[^\]]*\]")


def exe_relative_path(template: str | None) -> str | None:
    """The game exe relative to its folder, from an EA path template.

    ``[HKEY_LOCAL_MACHINE\\...\\Install Dir]bin_ship\\Game.exe`` → ``bin_ship/Game.exe``.
    """
    if not template or not template.lower().endswith(".exe"):
        return None
    relative = _REGISTRY_PREFIX.sub("", template).replace("\\", "/").strip("/")
    return relative or None


def resolve_case_insensitive(root: Path, relative: str) -> Path | None:
    """``root/relative`` matched one component at a time ignoring case, or None."""
    current = root
    for part in relative.split("/"):
        if not part:
            continue
        exact = current / part
        if exact.exists():
            current = exact
            continue
        try:
            match = next((c for c in current.iterdir() if c.name.lower() == part.lower()), None)
        except OSError:
            return None
        if match is None:
            return None
        current = match
    return current if current.is_file() else None


def game_folder(base_path: str | None, slug: str) -> Path:
    """``<base_path>/EA/<slug>``; ``base_path`` is the queue's storage root."""
    return Path(base_path or "~/Games").expanduser() / "EA" / slug


def locate_exe(folder: Path, entry: dict[str, Any]) -> tuple[Path, str] | str:
    """``(exe, path relative to folder)`` from the offer's templates, or why not."""
    relative = exe_relative_path(entry.get("execute_path")) or exe_relative_path(entry.get("install_check"))
    if relative is None:
        return "no path from EA"
    exe = resolve_case_insensitive(folder, relative)
    if exe is None:
        return relative
    return exe, os.path.relpath(exe, folder)


class _ProgressMapper:
    """Turns the CLI's JSON-lines into download-queue progress."""

    def __init__(self, tail: TailRingBuffer) -> None:
        self._tail = tail
        self.build: dict[str, Any] = {}
        self.done = False
        self._last: tuple[float, int] | None = None

    async def handle_line(self, line: str, game_id: str, progress_cb: ProgressCallback) -> None:
        """``run_drained_cli`` line handler."""
        event = _json_line(line)
        if event is None:
            self._tail.append(line.strip()[:300])
            return
        kind = event.get("event")
        if kind == "build":
            self.build = event
        elif kind == "done":
            self.done = True
        if kind not in ("progress", "done") or progress_cb is None:
            return
        try:
            await progress_cb(self._payload(event))
        except Exception:
            logger.exception("%s progress callback failed for %s", _LOG, game_id)

    def _payload(self, event: dict[str, Any]) -> dict[str, Any]:
        total = int(event.get("total") or 0)
        done = min(int(event.get("downloaded") or 0), total) if total else 0
        now = time.monotonic()
        speed = 0.0
        if self._last is not None and now > self._last[0]:
            speed = max(0.0, (done - self._last[1]) / (now - self._last[0]))
        self._last = (now, done)
        payload: dict[str, Any] = {
            "phase": "downloading",
            "downloaded_bytes": done,
            "total_bytes": total,
            "percentage": 100.0 if total == 0 else round(done * 100.0 / total, 2),
            "speed_bps": speed,
        }
        if speed > 0 and total > done:
            payload["eta_seconds"] = int((total - done) / speed)
        return payload


def _json_line(line: str) -> dict[str, Any] | None:
    text = line.strip()
    if not text.startswith("{"):
        return None
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


class EAInstaller:
    """Install, update and uninstall for the EA store."""

    def __init__(self, cli: MaximaCli, entry: EntryLookup) -> None:
        self._cli = cli
        self._entry = entry

    async def install(
        self, game_id: str, base_path: str | None, progress_cb: ProgressCallback,
    ) -> InstallResult:
        """Install ``game_id`` under ``<base_path>/EA/<slug>``."""
        entry = await self._entry(game_id)
        if entry is None:
            return _failed(game_id, "ea_game_not_in_library")
        folder = game_folder(base_path, str(entry.get("slug") or game_id))
        return await self._download(game_id, entry, folder, progress_cb)

    async def update(self, game_id: str, progress_cb: ProgressCallback) -> InstallResult:
        """Download the live build over the installed one."""
        record = await asyncio.to_thread(read_record, game_id)
        entry = await self._entry(game_id)
        if record is None or entry is None:
            return _failed(game_id, "ea_not_installed")
        return await self._download(game_id, entry, Path(record.install_path), progress_cb)

    async def live_build(self, game_id: str) -> str | None:
        """The build id EA serves now, or None when it cannot be read."""
        entry = await self._entry(game_id)
        if entry is None:
            return None
        try:
            return await self._cli.live_build_id(str(entry["offer_id"]))
        except MaximaCliError as e:
            logger.warning("%s could not read the live build of %s: %s", _LOG, game_id, e)
            return None

    async def size(self, game_id: str) -> int | None:
        """Space the live build takes once installed, before downloading it."""
        entry = await self._entry(game_id)
        if entry is None:
            return None
        try:
            return await self._cli.build_size(str(entry["offer_id"]))
        except MaximaCliError as e:
            logger.warning("%s could not read the size of %s: %s", _LOG, game_id, e)
            return None

    async def uninstall(self, game_id: str) -> Result:
        """Delete the game folder and its record. The prefix is the RPC's job."""
        record = await asyncio.to_thread(read_record, game_id)
        if record is None:
            return Result(success=False, store=STORE, error="ea_not_installed")
        if not await asyncio.to_thread(safe_rmtree, record.install_path):
            return Result(success=False, store=STORE, error="ea_uninstall_failed")
        await asyncio.to_thread(delete_record, game_id)
        return Result(success=True, store=STORE)

    async def _download(
        self, game_id: str, entry: dict[str, Any], folder: Path, progress_cb: ProgressCallback,
    ) -> InstallResult:
        await asyncio.to_thread(lambda: folder.mkdir(parents=True, exist_ok=True))
        args = ["install", "--offer-id", str(entry["offer_id"]), "--path", str(folder)]
        try:
            proc = await self._cli.spawn(args, merge_stderr=True)
        except MaximaCliError as e:
            return _failed(game_id, f"ea_install_failed: {e.code}")
        tail = TailRingBuffer(maxlen=40)
        mapper = _ProgressMapper(tail)
        drained: DrainedRun = await run_drained_cli(
            proc, game_id, progress_cb, mapper.handle_line, tail,
            log_prefix=_LOG, wait_s=EXIT_WAIT_S, stall_s=STALL_S,
        )
        if drained.rc != 0 or not mapper.done:
            logger.error("%s %s failed (exit %s): %s", _LOG, game_id, drained.rc, drained.tail)
            return _failed(game_id, f"ea_install_failed: {drained.tail[-300:]}")
        return await self._finish(game_id, entry, folder, mapper.build)

    async def _finish(
        self, game_id: str, entry: dict[str, Any], folder: Path, build: dict[str, Any],
    ) -> InstallResult:
        located = await asyncio.to_thread(locate_exe, folder, entry)
        if isinstance(located, str):
            return _failed(game_id, f"ea_exe_not_found: {located}")
        exe, exe_relative = located
        record = InstallRecord(
            game_id=game_id,
            offer_id=str(entry["offer_id"]),
            content_id=str(entry.get("content_id") or ""),
            install_path=str(folder),
            exe=str(exe),
            build_id=str(build.get("build_id") or ""),
            version=str(build.get("version") or ""),
        )
        await asyncio.to_thread(write_record, record)
        await write_manifest(
            install_dir=str(folder), store=STORE, store_id=game_id,
            title=str(entry.get("name") or game_id),
            executable_relative=exe_relative, platform="windows",
        )
        logger.info("%s %s installed at %s (%s)", _LOG, game_id, folder, record.version)
        # ``exe_path`` is what the download worker writes to games.map; without it the
        # worker guesses, and picked a build's trial exe (which demands Origin).
        return InstallResult(
            success=True, store=STORE, game_id=game_id, install_path=str(folder),
            metadata={"exe_path": str(exe)},
        )


def _failed(game_id: str, error: str) -> InstallResult:
    return InstallResult(success=False, store=STORE, game_id=game_id, error=error)
