"""Prefix cloning and ownership marking, shared by the wrapper stores.

py_modules/unifideck/stores/shared/prefix_clone.py

Wrapper stores give every game its own prefix, built by cloning a pristine
template rather than running the vendor installer N times. Ubisoft has done
this since 0.7; Battle.net is the second consumer, which is why the
mechanics moved here from ``stores/ubisoft/prefix/helpers.py``.

Three rules are encoded here rather than left to callers, because each one
maps to a way user data has been lost or nearly lost:

* **Never ``--delete``.** Repairing an existing game prefix must add and
  overwrite identity files without removing anything, or the repair eats
  the game sitting inside the prefix.
* **Exclude the games directory when repairing.** The install lives inside
  the prefix for these stores, so a naive re-clone would copy a template's
  empty games tree over a populated one.
* **Ownership is a marker, never a path.** A prefix is provably ours only
  because of a file we wrote inside it. Inferring ownership from location
  once nearly deleted a gigabyte of user prefixes.

Measured cost on a Steam Deck (ext4, no reflink): a 1.6 GB / 4642-file
prefix clones in about 12 seconds, so this is cheap enough to do at install
time and does not need a copy-on-write fast path.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Cloning a large prefix is slow but bounded; a hung rsync must not wedge an
# install queue forever.
CLONE_TIMEOUT_SECONDS = 30 * 60

# The vendor client installs games under this directory inside the prefix.
GAMES_DIR_NAME = "games"


@dataclass(frozen=True, slots=True)
class PrefixMarker:
    """Proof that Unifideck created a prefix, and how.

    ``source`` is free-text provenance and the two stores fill it
    differently on purpose: Battle.net records the template path it cloned,
    Ubisoft records *how* the prefix came to exist (``cloned_from_template``,
    ``fresh_install``, ``template_from_auth``, …). Nothing parses it, so it
    is documented rather than forced into one shape.

    ``game_id`` is set only for a per-game prefix; a template or auth prefix
    leaves it None.
    """

    store: str
    created_at: float
    source: str | None = None
    client_build: str | None = None
    game_id: str | None = None
    version: int = 1


def marker_path(prefix: Path, filename: str) -> Path:
    return Path(prefix) / filename


def write_marker(prefix: Path, filename: str, marker: PrefixMarker) -> bool:
    """Stamp a prefix as ours. Returns False if it could not be written.

    A failure here matters: an unmarked prefix is treated as not ours, so
    later cleanup will refuse to touch it. That is the safe direction.
    """
    path = marker_path(prefix, filename)
    payload = {
        "store": marker.store,
        "created_at": marker.created_at,
        "source": marker.source,
        "client_build": marker.client_build,
        "game_id": marker.game_id,
        "version": marker.version,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("[prefix_clone] cannot write marker %s: %s", path, exc)
        return False
    return True


def read_marker(prefix: Path, filename: str) -> PrefixMarker | None:
    """Read a prefix's ownership marker, or None when it is not ours."""
    try:
        raw = marker_path(prefix, filename).read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        logger.warning("[prefix_clone] cannot read marker in %s: %s", prefix, exc)
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        # A marker we cannot parse is still a marker we wrote.
        return PrefixMarker(store="", created_at=0.0)
    if not isinstance(data, dict):
        return PrefixMarker(store="", created_at=0.0)
    created = data.get("created_at")
    return PrefixMarker(
        store=str(data.get("store") or ""),
        created_at=float(created) if isinstance(created, (int, float)) else 0.0,
        source=data.get("source") if isinstance(data.get("source"), str) else None,
        client_build=(
            data.get("client_build")
            if isinstance(data.get("client_build"), str)
            else None
        ),
        game_id=data.get("game_id") if isinstance(data.get("game_id"), str) else None,
        version=int(data.get("version") or 1),
    )


def is_owned_by(prefix: Path, filename: str, store: str) -> bool:
    """True only when the in-prefix marker names this store.

    Deliberately strict. Deleting a prefix is unrecoverable, and a prefix
    living under our directory is not evidence that we made it.
    """
    marker = read_marker(prefix, filename)
    return marker is not None and marker.store == store


async def rsync_clone(
    src: Path,
    dst: Path,
    *,
    exclude_games: bool = False,
    delete: bool = False,
    checksum: bool = False,
) -> bool:
    """Clone ``src`` into ``dst`` with rsync. Never raises.

    ``delete`` defaults to False and callers repairing an existing prefix
    must leave it that way: for wrapper stores the game lives inside the
    prefix, and ``--delete`` would remove it.

    ``checksum`` forces content comparison instead of rsync's default
    size-plus-mtime quick check. Required when *repairing* an existing
    prefix: identity files are small and are frequently rewritten within
    the same second at the same length, so the quick check skips them and
    the repair silently does nothing. Left off for a first clone, where the
    destination is empty and the quick check is both correct and faster.
    """
    args = ["rsync", "-a"]
    if checksum:
        args.append("--checksum")
    if exclude_games:
        args.append(f"--exclude={GAMES_DIR_NAME}")
    if delete:
        args.append("--delete")
    args.extend([f"{Path(src)}/", f"{Path(dst)}/"])

    try:
        await asyncio.to_thread(Path(dst).mkdir, parents=True, exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (OSError, ValueError):
        logger.exception("[prefix_clone] rsync spawn failed")
        return False

    try:
        _out, err = await asyncio.wait_for(
            proc.communicate(), timeout=CLONE_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        logger.exception(
            "[prefix_clone] rsync timed out after %ss — killing",
            CLONE_TIMEOUT_SECONDS,
        )
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        return False

    if proc.returncode != 0:
        logger.warning(
            "[prefix_clone] rsync failed (%s): %s",
            proc.returncode,
            err.decode(errors="replace")[:300],
        )
        return False
    return True


async def clone_template(
    template: Path,
    destination: Path,
    *,
    store: str,
    marker_filename: str,
    client_build: str | None = None,
    now: float | None = None,
) -> bool:
    """Create a fresh game prefix from a template, then mark it as ours."""
    if not await asyncio.to_thread(Path(template).is_dir):
        logger.warning("[prefix_clone] template missing: %s", template)
        return False
    if not await rsync_clone(template, destination):
        return False
    await asyncio.to_thread(
        write_marker,
        destination,
        marker_filename,
        PrefixMarker(
            store=store,
            created_at=time.time() if now is None else now,
            source=str(template),
            client_build=client_build,
        ),
    )
    return True


async def repair_from_template(
    template: Path,
    destination: Path,
) -> bool:
    """Refresh an existing game prefix's identity without touching its games.

    Additive only: no ``--delete``, and the games directory is excluded.
    Both are required — the game's files live inside this prefix.
    """
    both_exist = await asyncio.to_thread(
        lambda: Path(template).is_dir() and Path(destination).is_dir(),
    )
    if not both_exist:
        return False
    return await rsync_clone(
        template, destination, exclude_games=True, delete=False, checksum=True,
    )


def ensure_pfx_symlink(prefix: Path) -> None:
    """Ensure ``<prefix>/pfx`` exists, pointing at the prefix itself.

    umu normalises ``WINEPREFIX`` to ``<prefix>/pfx/`` and creates this
    self-symlink, so ``<prefix>/drive_c`` and ``<prefix>/pfx/drive_c`` are
    the same directory. A clone that loses it makes the client unfindable
    even though it is present on disk.
    """
    link = Path(prefix) / "pfx"
    if link.is_symlink() or link.exists():
        return
    with contextlib.suppress(OSError):
        link.symlink_to(".")
