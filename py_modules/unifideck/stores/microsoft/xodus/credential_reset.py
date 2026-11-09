"""stores/microsoft/xodus/credential_reset.py — clear what older builds left in plain text.

Backends with ``ours-encrypted-credentials`` seal everything they write, in the
same format as ``SecureTokenStore``, and can still read plain files. So plain
files would stay on disk until something happened to rewrite them, and some are
never rewritten. Once such a backend is installed, this signs the user out so
the next sign-in writes sealed files:

* the sign-in, the subscription cache, leftover temp files and the plain offline
  licences are deleted (an online launch stores a licence again);
* ``dev_license`` and ``device-tokens`` are the device's identity, not the
  sign-in. They are sealed in place: deleting them registers another Microsoft
  device at the next sign-in, and a full device group refuses licences (I-045).

Safe to repeat: sealed files are left alone, so an interrupted run finishes next
time. It waits while a download or a running game uses the credentials.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from unifideck.launcher.xbox_pc.component_locks import components_in_use
from unifideck.launcher.xbox_pc.layout import credential_dir, license_cache_dir
from unifideck.security import SecureTokenStore
from unifideck.security.secure_token_store import SecureTokenStoreError

from . import account_state
from .cli import XodusCliError

if TYPE_CHECKING:
    from .cli import XodusCli

logger = logging.getLogger(__name__)

_LOG = "[XboxPc]"
DEVICE_FILES = frozenset({"dev_license", "device-tokens"})
SESSION_FILE = "user-session"


@dataclass(frozen=True)
class ResetOutcome:
    """``done``: nothing is left to do this run. ``signed_out``: a sign-in was removed."""

    done: bool = False
    signed_out: bool = False


def _entries(folder: Path) -> list[Path]:
    if not folder.is_dir() or folder.is_symlink():
        return []
    return [p for p in folder.iterdir() if p.is_file() and not p.is_symlink()]


def _is_plain(path: Path, store: SecureTokenStore) -> bool:
    with path.open("rb") as fh:
        return not store.is_encrypted(fh.read(4))


def _seal_in_place(path: Path, store: SecureTokenStore) -> None:
    sealed = store.encrypt_bytes(path.read_bytes())
    tmp = path.with_name(f".{path.name}.reset.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(sealed)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def drop_all() -> int:
    """Remove the stored Xodus credentials by hand. Used only when the CLI
    cannot sign out (no backend). Returns how many files went."""
    folder = credential_dir()
    if not folder.is_dir() or folder.is_symlink():
        return 0
    removed = 0
    for entry in folder.iterdir():
        if entry.is_file() or entry.is_symlink():
            with contextlib.suppress(OSError):
                entry.unlink()
                removed += 1
    return removed


def clear_plain_files(store: SecureTokenStore | None = None) -> bool:
    """Delete or seal every plain file. True when a sign-in was removed. Blocking."""
    store = store or SecureTokenStore()
    # The key first: without one nothing can be sealed, so nothing is deleted.
    store.encrypt_bytes(b"")
    entries = _entries(credential_dir())
    # The sign-in goes first, so an interrupted run is already signed out.
    entries.sort(key=lambda p: p.name != SESSION_FILE)
    signed_out = False
    for path in entries:
        if path.name.startswith("."):
            path.unlink(missing_ok=True)
        elif _is_plain(path, store):
            if path.name in DEVICE_FILES:
                _seal_in_place(path, store)
            else:
                signed_out = signed_out or path.name == SESSION_FILE
                path.unlink(missing_ok=True)
    for path in _entries(license_cache_dir()):
        if _is_plain(path, store):
            path.unlink(missing_ok=True)
    if signed_out:
        account_state.clear_state()
    return signed_out


async def run(cli: XodusCli) -> ResetOutcome:
    """Clear the plain files once a sealing backend is installed."""
    try:
        backend = await cli.backend()
    except XodusCliError:
        return ResetOutcome()
    if not backend.sealed_credentials:
        return ResetOutcome()
    if cli.credential_lock.locked() or await asyncio.to_thread(components_in_use):
        logger.info("%s credentials in use; clearing the old plain files later", _LOG)
        return ResetOutcome()
    async with cli.credential_lock:
        try:
            signed_out = await asyncio.to_thread(clear_plain_files)
        except (SecureTokenStoreError, OSError) as e:
            # Rule 8: the user keeps a working plain sign-in, but it is not protected.
            logger.warning(
                "%s could not encrypt the Xbox PC sign-in in %s (%s): it stays in plain "
                "text, and a new sign-in will fail until this is fixed",
                _LOG, credential_dir(), type(e).__name__,
            )
            return ResetOutcome(done=True)
    if signed_out:
        logger.warning(
            "%s signed out of Xbox PC once: the old sign-in in %s was in plain text, "
            "and the next sign-in is encrypted", _LOG, credential_dir(),
        )
    return ResetOutcome(done=True, signed_out=signed_out)
