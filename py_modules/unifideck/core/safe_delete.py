"""Shared safe-deletion helpers for uninstall / cleanup flows.

Centralises the "is this path safe to ``rmtree``?" guard that the per-store
uninstallers (Epic, GOG, Amazon, Ubisoft) and the global "Delete all data"
cleanup each used to re-implement (some only checked ``/`` and ``$HOME``,
others a loose substring allowlist). One guard means custom install locations
(SD card, ``/mnt`` libraries, user-picked folders) delete reliably while
system paths stay protected.

All functions are synchronous and do blocking I/O — call them from a thread
(``asyncio.to_thread``) on the event loop.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

# A path must have at least this many ``parts`` (``/`` counts as one) to be
# eligible for deletion. ``/home/deck/X`` has 4 → allowed; ``/home/deck`` has
# 3 → rejected. Mirrors Ubisoft's existing ``_DELETE_MIN_PATH_DEPTH`` guard.
_MIN_DEPTH = 4

def is_safe_to_delete(path: str | Path) -> bool:
    """True iff *path* is safe to recursively delete.

    Rejects empty paths, ``/``, ``$HOME`` and any ancestor of ``$HOME``
    (e.g. ``/home``), and anything shallower than :data:`_MIN_DEPTH`. Symlinks
    are resolved first so ``~/foo -> /`` can't slip a dangerous target through.
    """
    if not path:
        return False
    try:
        resolved = Path(path).expanduser().resolve()
    except (OSError, RuntimeError):
        logger.exception("[safe_delete] resolve(%s) failed", path)
        return False
    home = Path.home().resolve()
    if resolved == Path("/") or resolved == home:
        return False
    # Reject ancestors of $HOME (``/``, ``/home``, ``/home/deck`` → all unsafe).
    if home == resolved or _is_ancestor(resolved, home):
        return False
    return len(resolved.parts) >= _MIN_DEPTH

def _is_ancestor(maybe_ancestor: Path, child: Path) -> bool:
    """True iff *maybe_ancestor* is an ancestor of (or equal to) *child*."""
    try:
        child.relative_to(maybe_ancestor)
        return True
    except ValueError:
        return False

def safe_rmtree(path: str | Path) -> bool:
    """``rmtree`` *path* iff it passes :func:`is_safe_to_delete`.

    Returns True when the path is gone afterwards (already-absent counts as
    success — deletion is idempotent), False if the guard rejected it or the
    directory still exists after the attempt.
    """
    p = Path(path).expanduser()
    if not p.exists():
        return True
    if not is_safe_to_delete(p):
        logger.error("[safe_delete] refusing to delete unsafe path: %s", p)
        return False
    try:
        shutil.rmtree(p, ignore_errors=True)
    except OSError:
        logger.exception("[safe_delete] rmtree(%s) failed", p)
    gone = not p.exists()
    if not gone:
        logger.warning("[safe_delete] %s still present after rmtree", p)
    return gone

def foreign_installs_under(
    path: str | Path, *, owner_key: str,
) -> list[str]:
    """games.map keys, other than *owner_key*, whose install lives under *path*.

    The ownership oracle for "may I delete/extract into this directory?". Two
    stores can pick the same folder name under the same install root — GOG and
    a GameVault archive both call Bastion's folder ``Bastion`` — and the loser
    of that race previously had its files deleted by the winner's cleanup.

    games.map is the right source: it is the one file that records, per
    shortcut, the exe and work_dir actually in use. An install with no row yet
    (mid-install) is invisible here, which is why callers that *create* a
    directory should also treat a non-empty pre-existing directory as a signal.

    Both sides are ``resolve()``d so a symlinked SD-card mount cannot slip a
    match past a string comparison, and an unresolvable candidate is reported
    as foreign — the conservative answer when the question is "is it safe to
    delete this?".
    """
    from unifideck.services.shortcut.games_map import parse_games_map
    from unifideck.utils.paths import get_games_map_path

    try:
        target = Path(path).expanduser().resolve()
    except (OSError, RuntimeError):
        logger.exception("[safe_delete] resolve(%s) failed", path)
        return [owner_key]
    try:
        content = Path(get_games_map_path()).read_text()
    except OSError as exc:
        # No manifest yet, or unreadable. Report nothing rather than blocking
        # every cleanup on this device: the caller's own safety guards still
        # apply, and a missing games.map means no shortcut exists to protect.
        logger.debug("[safe_delete] could not read games.map: %s", exc)
        return []
    found: list[str] = []
    for key, entry in parse_games_map(content).items():
        if key == owner_key:
            continue
        if any(_is_under(candidate, target) for candidate in (entry.work_dir, entry.exe)):
            found.append(key)
    return found


def _is_under(candidate: str, target: Path) -> bool:
    """True iff *candidate* resolves to *target* or something inside it.

    An unresolvable or empty candidate is False: it names nothing on this
    filesystem, so it cannot be the install that would be destroyed. The
    xCloud sentinel (``exe="xcloud"``, a URL in ``work_dir``) falls out here.
    """
    if not candidate or candidate == "xcloud":
        return False
    try:
        resolved = Path(candidate).expanduser().resolve()
    except (OSError, RuntimeError):
        return False
    return _is_ancestor(target, resolved)


def canonical_prefix(game_id: str) -> Path:
    """Per-game Proton prefix path for non-Ubisoft stores.

    Matches the launcher's ``_resolve_prefix`` (Epic/GOG/Amazon use a flat
    ``prefixes/<game_id>`` dir — no store subdirectory). Keep in sync with
    ``launcher/proton/infrastructure/core.py``.
    """
    return Path(
        "~/.local/share/unifideck/prefixes",
    ).expanduser() / game_id
