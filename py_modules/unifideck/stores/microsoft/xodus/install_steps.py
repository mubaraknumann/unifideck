"""stores/microsoft/xodus/install_steps.py — the steps an install and an update share.

Both stream a package into a game folder and then check and record what
landed:

1. :func:`ready_code`: the feature is on, the components are installed and
   the Xbox PC account is signed in. An account nobody has asked about
   since the plugin started is asked about once here.
2. :func:`choose_content`: ``resolve-package``, then one supported Game.
3. :func:`stream_into`: ``xodus-cli streaming`` under the credential lock
   and the shared component locks, so a component update cannot delete the
   backend mid-download.
4. :func:`verify`: the CLI said ``complete`` and exited 0, and the folder
   holds the manifest, the launch executable and the package container.
5. :func:`commit`: the ownership marker again (now naming the exe), then
   the ``installed`` record, both before the result returns.

:func:`mark_owned` writes the marker before the first byte streams, so a
cancelled or failed download is still found by a destructive cleanup.

Each step raises :class:`~.errors.InstallError`; the caller turns it into
the failed ``InstallResult``.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Protocol

from unifideck.core.manifest import write_manifest
from unifideck.core.types import InstallResult
from unifideck.launcher.xbox_pc.component_locks import ComponentsBusyError, reading_components
from unifideck.launcher.xbox_pc.install_records import write_install_record
from unifideck.launcher.xbox_pc.layout import ActiveComponents
from unifideck.stores.shared.installed_size import dir_size_bytes

from .errors import InstallError, classify_failure, cli_error_text
from .game_facts import GameFacts, read_game_facts
from .ids import store_game_id
from .package_choice import select_content
from .progress import ProgressCallback
from .streaming import StreamOutcome, run_streaming

logger = logging.getLogger(__name__)

STORE = "microsoft"

#: Runtime states a game install can use. ``outdated`` means a newer build
#: is available from the source, not that the installed one is broken.
USABLE_RUNTIME = frozenset({"installed", "outdated"})
#: Account states that hold a usable sign-in. ``unverified`` is signed in;
#: only the gamertag check is still pending.
SIGNED_IN = frozenset({"signed_in", "unverified"})


class XboxPcLike(Protocol):
    """What these steps need from the ``XboxPc`` facade (stage A)."""

    def enabled(self) -> bool:
        """The feature toggle."""

    async def status(self) -> dict[str, Any]:
        """The cached status snapshot."""

    async def checked_status(self) -> dict[str, Any]:
        """The status, with an ``unknown`` account asked about once."""

    def market_hint(self) -> str | None:
        """The xCloud session market, if known."""


class XodusCliLike(Protocol):
    """What these steps need from ``XodusCli`` (stage A)."""

    credential_lock: asyncio.Lock

    def credential_dir(self) -> Path:
        """``XODUS_CREDENTIAL_DIR``: absolute and 0700."""

    async def backend(self) -> Any:
        """The verified backend paths. Raises ``XodusCliError``."""

    async def resolve_package(self, pid: str, market: str) -> dict[str, Any]:
        """``resolve-package`` JSON. Raises ``XodusCliError``."""

    async def spawn_streaming(
        self, pid: str, dest: str, market: str, content_id: str,
        active: ActiveComponents | None = None,
    ) -> Any:
        """Start ``xodus-cli streaming`` from ``active``'s backend. Raises ``XodusCliError``."""


def failed_result(game_id: str, fail: InstallError) -> InstallResult:
    """The ``InstallResult`` for a refused or failed install or update."""
    return InstallResult(
        success=False, store=STORE, game_id=game_id,
        error=str(fail), error_code=fail.code,
    )


async def ready_code(xbox_pc: XboxPcLike) -> str | None:
    """Why the feature cannot run a download now, or None when it can."""
    if not xbox_pc.enabled():
        return "xbox_pc_disabled"
    # After a plugin restart the account is ``unknown`` until someone asks
    # Xodus. ``checked_status`` asks once, so a signed-in user is not
    # refused as signed out.
    status = await xbox_pc.checked_status()
    if (status.get("runtime") or {}).get("state") not in USABLE_RUNTIME:
        return "xbox_pc_runtime_missing"
    if (status.get("account") or {}).get("state") not in SIGNED_IN:
        return "xbox_pc_not_signed_in"
    return None


async def check_backend(cli: XodusCliLike) -> None:
    """Fail fast when the backend is missing or fails its checksums."""
    try:
        await cli.backend()
    except Exception as e:
        raise InstallError(cli_error_text(e)) from e


async def choose_content(
    cli: XodusCliLike, pid: str, market: str, prefer_cid: str | None,
) -> str:
    """The content id to stream. ``resolve-package`` needs no sign-in."""
    try:
        plan = await cli.resolve_package(pid, market)
    except Exception as e:
        raise InstallError("xbox_resolve_failed", cli_error_text(e)) from e
    return select_content(plan, pid, prefer_cid)


@contextlib.asynccontextmanager
async def holding_components() -> AsyncIterator[ActiveComponents]:
    """Hold the installed components shared for the ``async with`` body.

    Taking the locks may wait up to 10 s for a component promote, so it
    runs in a thread. Releasing them is two ``flock`` calls and runs here,
    so a second cancel cannot skip it.
    """
    holder = reading_components()
    try:
        active = await asyncio.to_thread(holder.__enter__)
    except ComponentsBusyError as e:
        raise InstallError("xodus_components_busy") from e
    except OSError as e:
        # An unsafe or unreadable data folder. The message names a home
        # path, so only the type goes out.
        raise InstallError("xodus_components_unreadable", type(e).__name__) from e
    try:
        if active is None:
            raise InstallError("xbox_pc_runtime_missing")
        yield active
    finally:
        holder.__exit__(None, None, None)


async def report_starting(progress_cb: ProgressCallback) -> None:
    """Show "Preparing download" from the first step.

    Resolving the package and starting the CLI take a few seconds before its
    first progress record; without this the row reads "Downloading 0.0%".
    """
    if progress_cb is not None:
        await progress_cb({"phase": "starting"})


async def stream_into(
    cli: XodusCliLike, pid: str, game_dir: Path, market: str, content_id: str,
    progress_cb: ProgressCallback,
) -> StreamOutcome:
    """Run one ``streaming`` into ``game_dir`` and return how it ended.

    A cancel kills the process tree and re-raises. The partial download
    and its journal stay, so the next run resumes.
    """
    async with cli.credential_lock, holding_components() as active:
        try:
            proc = await cli.spawn_streaming(pid, str(game_dir), market, content_id, active)
        except Exception as e:
            raise InstallError(cli_error_text(e)) from e
        logger.info("[xbox_install] streaming %s (%s) into %s", pid, market, game_dir)
        return await run_streaming(proc, pid, progress_cb)


async def verify(outcome: StreamOutcome, game_dir: Path) -> GameFacts:
    """The facts of a finished download. Raises when it is not complete."""
    if not outcome.succeeded:
        if outcome.terminal == "complete":
            # The package was written, then the CLI failed or hung on exit.
            raise InstallError(
                "xbox_install_incomplete", f"exit_after_complete | {outcome.tail}",
            )
        raise classify_failure(outcome)
    facts, missing = await asyncio.to_thread(read_game_facts, game_dir)
    if facts is None:
        raise InstallError("xbox_install_incomplete", missing)
    return facts


async def save_record(record: dict[str, Any]) -> None:
    """Write an install record. Raises ``record_write_failed``."""
    try:
        await asyncio.to_thread(write_install_record, record)
    except (OSError, ValueError) as e:
        raise InstallError("record_write_failed", type(e).__name__) from e


async def mark_owned(root: Path, game_id: str, title: str, exe_relative: str = "") -> None:
    """Write the ownership marker into ``root``. Raises ``marker_write_failed``.

    The marker carries the ``games.map`` id, which the uninstall RPC's
    marker sweep looks it up by. ``exe_relative`` is empty until the
    download has landed.
    """
    marked = await write_manifest(
        install_dir=str(root), store=STORE, store_id=game_id, title=title,
        executable_relative=exe_relative,
    )
    if not marked:
        raise InstallError("marker_write_failed")


async def commit(
    record: dict[str, Any], facts: GameFacts, root: Path, *, stamp: str,
) -> InstallResult:
    """Write the marker and the ``installed`` record, then the result.

    ``stamp`` is the record field that gets the time (``installedAt`` or
    ``updatedAt``). The marker goes first: without it a destructive cleanup
    could not find the folder, so its failure fails the install.
    """
    pid, game_id = str(record["productId"]), store_game_id(record)
    await mark_owned(root, game_id, facts.title, f"game/{facts.exe_relative}")
    record.update({
        "state": "installed", "exe": str(facts.exe), "exeRelative": facts.exe_relative,
        "version": facts.version or "", "titleId": facts.title_id or "",
        stamp: time.time(),
    })
    await save_record(record)
    size = await asyncio.to_thread(dir_size_bytes, str(root))
    logger.info("[xbox_install] %s installed at %s (%d bytes)", pid, root, size)
    return InstallResult(
        success=True, store=STORE, game_id=game_id, install_path=str(root),
        size_bytes=size,
        metadata={"exe_path": str(facts.exe), "runtime_managed_prefix": True},
    )
