"""launcher/proton/handlers/ubisoft_recovery.py — find or rebuild a UPC prefix.

Split out of ``handlers/ubisoft.py`` (file-size cap). That module drives the
three UPC entry points — play, auth, install; this one answers the question all
three depend on: *where is a usable, signed-in UPC prefix for this game, and
can we rebuild one if it is gone?*

Dependencies point one way: ``ubisoft.py`` imports from here, never the
reverse. This module owns the UPC-location primitives (``find_upc_in``, the
template/base paths, the id_map file) precisely so both can share them without
an import cycle.

Recovery is ordered cheapest-first:

1. the prefix the plan resolved (the id_map pointer);
2. the same ``space_id`` under any other storage base the id_map knows about —
   the recorded pointer can be lost while the real prefix survives on SD;
3. a clone of the prebuilt ``.template``, which is a pure file copy (no umu).

Two things the clone must carry that a bare ``rsync -a`` gets wrong, both
confirmed live on 2026-08-01:

* the target's ``.unifideck_proton_version`` — the template ships a stale one
  and ``-a`` preserves mtime, so the copy silently reverts the marker the
  current launch just stamped, turning a one-off prefix reset into a permanent
  wipe-and-reclone loop; and
* the **credentials** — ``.template`` is deliberately pristine, so a bare clone
  is SIGNED OUT and hands the user a UPC demanding a sign-in they already did.
"""
from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from unifideck.launcher.proton.infrastructure.prefix_layout import resolve_drive_c

logger = logging.getLogger(__name__)

ID_MAP_FILE = Path(
    "~/.local/share/unifideck/ubisoft_id_map.json",
).expanduser()
# upc.exe location relative to a prefix's drive_c (see :func:`find_upc_in` —
# umu/Proton nest the real drive_c under ``pfx/``, so this is never combined
# with a prefix root directly; always go through ``resolve_drive_c``).
_UPC_RELATIVE = (
    Path("Program Files (x86)")
    / "Ubisoft"
    / "Ubisoft Game Launcher"
    / "upc.exe"
)
# Prebuilt UPC prefix the backend clones per-game prefixes from.
_TEMPLATE_DIR = Path(
    "~/.local/share/unifideck/prefixes/ubisoft/.template",
).expanduser()
# Fixed internal storage base (``<base>/prefixes/ubisoft/<id>``).
_PREFIXES_BASE_DEFAULT = Path("~/.local/share/unifideck").expanduser()


def find_upc_in(prefix_root: Path) -> Path | None:
    """upc.exe under ``prefix_root`` if present, or ``None``.

    Goes through :func:`resolve_drive_c` rather than combining
    ``prefix_root / "drive_c"`` directly — umu/Proton nest the real
    ``drive_c`` under ``pfx/``, so a direct combine never finds it on a
    modern prefix layout. Same bug class already fixed elsewhere for
    ``system.reg``/``user.reg``; this file's several upc.exe lookups had
    never been ported to it, which is why the Ubisoft auth/install flows
    silently failed to find an upc.exe that was genuinely present.
    """
    drive_c = resolve_drive_c(prefix_root)
    if drive_c is None:
        return None
    candidate = drive_c / _UPC_RELATIVE
    return candidate if candidate.is_file() else None


def _candidate_prefix_dirs(space_id: str) -> list[Path]:
    """Per-game prefix dirs for ``space_id`` across every known storage base.

    The launcher resolves a single prefix from the id_map; when that path is
    an empty husk (the recorded pointer was lost), the real populated prefix
    can live under a different storage base the user picked for another game.
    Derive each base from the recorded ``prefix_path`` values in the id_map
    (strip the trailing ``prefixes/ubisoft/<id>``) and rebuild the path for
    THIS ``space_id`` under each, plus the fixed internal default. Strict:
    the candidate basename is always ``space_id`` (never a fuzzy match).
    """
    seen: set[str] = set()
    candidates: list[Path] = []
    for base in _read_storage_bases():
        cand = base / "prefixes" / "ubisoft" / space_id
        key = str(cand)
        if key not in seen:
            seen.add(key)
            candidates.append(cand)
    return candidates


def _read_storage_bases() -> list[Path]:
    """Storage bases to probe: the fixed internal default plus every base
    derived from recorded id_map ``prefix_path`` values."""
    bases: list[Path] = [_PREFIXES_BASE_DEFAULT]
    try:
        data = json.loads(ID_MAP_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return bases
    if not isinstance(data, dict):
        return bases
    for entry in data.values():
        base = _base_from_id_map_entry(entry)
        if base is not None:
            bases.append(base)
    return bases


def _base_from_id_map_entry(entry: object) -> Path | None:
    """The storage base from one id_map entry's ``prefix_path`` (strip the
    trailing ``prefixes/ubisoft/<id>``), or None when it doesn't match."""
    recorded = entry.get("prefix_path") if isinstance(entry, dict) else None
    if not isinstance(recorded, str) or not recorded:
        return None
    p = Path(recorded)
    if p.parent.name == "ubisoft" and p.parent.parent.name == "prefixes":
        return p.parent.parent.parent
    return None


def find_recovered_prefix(space_id: str) -> Path | None:
    """A populated (upc.exe-bearing) prefix for ``space_id``, found by scan."""
    for cand in _candidate_prefix_dirs(space_id):
        if find_upc_in(cand) is not None:
            return cand
    return None


def clone_template_into(prefix_dir: Path) -> bool:
    """Best-effort clone of the ``.template`` prefix into ``prefix_dir``.

    Last-resort recovery when no populated prefix exists anywhere: the
    template is an already-built UPC prefix, so this is a pure file copy (no
    umu). Guarded — never runs when the target already has upc.exe, and
    preserves the template's ``pfx`` symlink (the shared auth prefix) via
    ``rsync -a`` / ``cp -a``. Returns True only if upc.exe lands.

    The target's ``.unifideck_proton_version`` is carried across the copy: the
    template ships its own (``Proton - Experimental``, written when it was
    built), and ``rsync -a`` preserves mtime, so without this the clone
    silently reverts the marker the current launch just stamped. That turned a
    one-off prefix reset into a permanent loop — every launch saw the same
    stale family, reset again, re-cloned, reverted again (2026-08-01,
    Rayman Origins). Done around the copy rather than with ``--exclude`` so
    the ``cp -a`` fallback cannot drift from the rsync path.

    **Deliberately not ``shared/prefix_clone.rsync_clone``**, which the
    backend's Ubisoft clones all use. Three reasons, each disqualifying on
    its own: this runs in the launcher under the system Python, so it is
    synchronous ``subprocess.run`` rather than asyncio; it needs the
    ``cp -a`` fallback for a host with no rsync; and it must restore the
    marker described above, which a generic clone knows nothing about.
    Folding it in would drop that restoration and re-open the loop.
    """
    if find_upc_in(prefix_dir) is not None:
        return True
    if find_upc_in(_TEMPLATE_DIR) is None:
        logger.warning(
            "[launcher.proton.ubisoft] template missing upc.exe at %s — "
            "cannot clone",
            _TEMPLATE_DIR,
        )
        return False
    marker = _read_proton_marker(prefix_dir)
    try:
        prefix_dir.mkdir(parents=True, exist_ok=True)
        rsync = subprocess.run(
            ["rsync", "-a", f"{_TEMPLATE_DIR}/", f"{prefix_dir}/"],
            capture_output=True,
            check=False,
        )
        if rsync.returncode != 0:
            subprocess.run(
                ["cp", "-a", f"{_TEMPLATE_DIR}/.", str(prefix_dir)],
                check=True,
                capture_output=True,
            )
    except (OSError, subprocess.SubprocessError):
        logger.exception(
            "[launcher.proton.ubisoft] template clone into %s failed",
            prefix_dir,
        )
        return False
    _restore_proton_marker(prefix_dir, marker)
    if find_upc_in(prefix_dir) is None:
        return False
    _inject_credentials(prefix_dir)
    return True


def _inject_credentials(prefix_dir: Path) -> None:
    """Copy the signed-in UPC credentials into a freshly cloned prefix.

    The ``.template`` is deliberately pristine (invariant: it is rewritten only
    on explicit sign-in/sign-out), so a bare clone of it is SIGNED OUT — its
    ``ConnectSecureStorage.dat`` is the ~1 KB never-logged-in shape and it has
    no ``user.dat`` at all. Without this, the recovery hands the user a working
    UPC that demands a sign-in they already completed.

    The install path gets this for free via
    ``UbisoftPrefixManager.bootstrap_game_prefix``; this launch-time recovery
    was a bare rsync and never did it. Best-effort — a launch must not fail
    because credentials could not be copied; the user just sees the sign-in
    prompt, which is the pre-existing behaviour.
    """
    try:
        from unifideck.stores.ubisoft.session import build_standalone_session

        if build_standalone_session().inject_into_prefix(str(prefix_dir)):
            logger.info(
                "[launcher.proton.ubisoft] injected UPC credentials into %s",
                prefix_dir,
            )
    except Exception:
        logger.exception(
            "[launcher.proton.ubisoft] credential injection into %s failed "
            "(non-fatal; UPC may ask the user to sign in)",
            prefix_dir,
        )


def _read_proton_marker(prefix_dir: Path) -> str | None:
    """This prefix's recorded Proton, or None when it has no marker yet."""
    from unifideck.launcher.proton.compat.prefix_init import _MARKER_NAME

    try:
        return (prefix_dir / _MARKER_NAME).read_text(encoding="utf-8")
    except OSError:
        return None


def _restore_proton_marker(prefix_dir: Path, marker: str | None) -> None:
    """Put ``marker`` back after a template clone overwrote it."""
    from unifideck.launcher.proton.compat.prefix_init import _MARKER_NAME

    if marker is None:
        return
    try:
        (prefix_dir / _MARKER_NAME).write_text(marker, encoding="utf-8")
    except OSError:
        logger.exception(
            "[launcher.proton.ubisoft] could not restore the Proton marker "
            "in %s — the next launch may see a spurious family change",
            prefix_dir,
        )

