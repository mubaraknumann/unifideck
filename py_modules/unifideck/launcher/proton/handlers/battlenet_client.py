"""Locating the Battle.net client and resolving launch codes.

py_modules/unifideck/launcher/proton/handlers/battlenet_client.py

Runs inside the out-of-process launcher, under the SYSTEM python (3.10 to
3.14), so this is stdlib-only and must not import the plugin backend.

Two things it deliberately does not do:

* **It never combines ``prefix / "drive_c"`` directly.** umu creates
  ``pfx -> .`` as a self-symlink and both layouts occur in the wild; the
  naive combine is what made Ubisoft's recovery path fail to find a
  ``upc.exe`` that was genuinely present.
* **It never derives a FAMILY from a uid by transformation.** The two are
  unrelated namespaces (``fenris`` -> ``Fen``, ``hs_beta`` -> ``WTCG``) and
  Blizzard renames families, so the mapping is read from the id map the
  backend writes. A wrong family fails *silently*.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# The Battle.net client path constants are NOT declared here. They live in
# ``stores/battlenet/paths.py``, which owns them; this module used to hold
# identical string literals beside them — a hand-maintained pair of the
# §3.1 class that check 13 cannot see, because constants are not function
# bodies. Nothing outside this file read them. Audit register item 47.
# The client itself, inside the versioned payload dir — a DLL, not an exe.
# Mirrors ``stores/battlenet/paths.CLIENT_DLL``.
CLIENT_DLL = "battle.net.dll"

def id_map_path() -> Path:
    """Where the Battle.net id map lives.

    Resolved on every call rather than once at import, for the reason
    ``wrapper_session.prefix_index_path`` spells out: a module-level constant
    is computed before pytest's autouse fixture redirects ``HOME``, so it
    keeps pointing at the developer's real data directory for the whole run.
    That is not hypothetical — a test run wrote a synthetic ``fenris`` row
    into the live map hours after the plugin had last been up. Reading the
    environment at call time is both correct and cheap.
    """
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "unifideck" / "battlenet_id_map.json"



def _client_dir(prefix: Path | str) -> Path | None:
    """The Battle.net client directory inside *prefix*, or ``None``.

    This module held its own copy of this and the two exe lookups below,
    plus its own ``CLIENT_DIR``/``CLIENT_EXE``/``LAUNCHER_EXE`` literals —
    the launcher reimplementing the store's own path logic. The two agreed
    by luck rather than by construction. Audit register item 47.

    **The import stays inside the function.** ``stores/battlenet/paths``
    reaches back into the launcher (it resolves ``drive_c`` through
    ``prefix_layout``), so a module-level import closes a cycle — which is
    what the first attempt at this consolidation did, breaking collection of
    ``test_battlenet_agent_queue``. ``battlenet_bootstrap`` reaches into the
    store package the same way, for the same reason.
    """
    from unifideck.stores.battlenet import paths

    return paths.client_dir(Path(prefix))


def find_client_exe(prefix: Path | str) -> Path | None:
    """``Battle.net.exe`` — the binary that accepts ``--exec``.

    Confirmed on-device: ``Battle.net Launcher.exe`` does not.
    """
    from unifideck.stores.battlenet import paths

    return paths.client_exe(Path(prefix))


def find_launcher_exe(prefix: Path | str) -> Path | None:
    """``Battle.net Launcher.exe`` — started first, owns the wineserver."""
    from unifideck.stores.battlenet import paths

    return paths.launcher_exe(Path(prefix))


def find_payload_dir(prefix: Path | str) -> Path | None:
    """The newest versioned client payload ``Battle.net.<build>/``, or None.

    ``Battle.net.exe`` beside the launcher is a ~1 MB **shim**: a host
    process that loads the real client out of this directory, and the
    bootstrapper writes the shim long before that payload finishes
    downloading. An install interrupted in that window leaves a prefix that
    passes every "is the client here" check and cannot start — measured in
    the field, where it poisoned the auth prefix, the template derived from
    it and every game prefix cloned from that, so each launch burned the
    full 300 s readiness timeout and no amount of signing out repaired it.

    Keyed on the client DLL (``battle.net.dll``), NOT on an exe: the payload
    dir holds no ``Battle.net.exe`` at all — the client is a DLL, beside
    ``libcef.dll`` and ``Battle.net.mpq``. Its only exes are the auxiliary
    ``BlizzardError.exe`` / ``GameSessionMonitor.exe``. Keying on an exe
    reported every correctly installed client as incomplete, which refused
    every install and made every client unstartable.

    The backend states the same rule in ``stores/battlenet/paths.py``
    (:func:`client_payload_dir`). It is written twice on purpose: this
    module runs in the out-of-process launcher under the SYSTEM python and
    must not import the plugin backend. ``test_battlenet_paths_config``
    holds both to the same fixtures.
    """
    parent = _client_dir(prefix)
    if parent is None:
        return None
    versioned = [p for p in parent.glob("Battle.net.*") if p.is_dir()]

    def _build(path: Path) -> tuple[int, str]:
        suffix = path.name.rsplit(".", 1)[-1]
        return (int(suffix), path.name) if suffix.isdigit() else (-1, path.name)

    for candidate in sorted(versioned, key=_build, reverse=True):
        if _holds_client_dll(candidate):
            return candidate
    return None


def _holds_client_dll(payload: Path) -> bool:
    """Whether a payload directory holds the client DLL (case-insensitive).

    Mirrors ``stores/battlenet/paths._holds_client_dll``.
    """
    try:
        return any(
            entry.name.lower() == CLIENT_DLL and entry.is_file()
            for entry in payload.iterdir()
        )
    except OSError:
        return False


def _load_id_map() -> dict[str, dict[str, object]]:
    try:
        data = json.loads(id_map_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def resolve_family(uid: str) -> str | None:
    """The ``--exec`` family code for a game uid, or None.

    Prefers a family already proven to launch this game: an obsolete code
    fails silently, so one that has demonstrably worked is never
    second-guessed.
    """
    record = _load_id_map().get(uid)
    if not isinstance(record, dict):
        logger.warning("[battlenet] no id-map record for uid=%s", uid)
        return None
    proven = record.get("last_launch_family") if record.get("launch_ok_at") else None
    family = proven or record.get("family")
    if not isinstance(family, str) or not family:
        logger.warning("[battlenet] id-map record for %s has no family", uid)
        return None
    return family


def resolve_prefix(uid: str) -> Path | None:
    """The recorded prefix for a game. Never reconstructed from the uid."""
    record = _load_id_map().get(uid)
    if not isinstance(record, dict):
        return None
    path = record.get("prefix_path")
    return Path(path) if isinstance(path, str) and path else None


def record_launch_ok(uid: str, family: str, when: float) -> None:
    """Stamp a family that has demonstrably started this game.

    Written from the launcher rather than the backend because only this
    process observes the outcome: the game is spawned by the client inside
    the prefix, so the backend never sees it start. The backend reads the
    same fields back through ``BattlenetIdMap``.

    Read-modify-atomic-write against the whole file. The backend can be
    writing concurrently (a sync refreshing family codes), and a partial
    write would lose every recorded ``prefix_path`` — which is the one
    thing in this file that cannot be recomputed. Best-effort throughout:
    failing to record a successful launch must never fail the launch.
    """
    data = _load_id_map()
    record = data.get(uid)
    entry: dict[str, object] = dict(record) if isinstance(record, dict) else {}
    entry.update(
        {"family": family, "last_launch_family": family, "launch_ok_at": when},
    )
    data[uid] = entry
    path = id_map_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
        Path(tmp).replace(path)
    except OSError as exc:
        logger.warning("[battlenet] cannot record launch for %s: %s", uid, exc)
        return
    logger.info("[battlenet] recorded proven family %s for %s", family, uid)


def client_installed(prefix: Path | str) -> bool:
    """Both halves present — the ``--exec`` shim and the payload it loads.

    See :func:`find_payload_dir` for why the shim alone is not enough.
    """
    return find_client_exe(prefix) is not None and find_payload_dir(prefix) is not None


def client_startable(prefix: Path | str) -> bool:
    """Everything the two-phase launch needs before it starts anything.

    Phase A runs ``Battle.net Launcher.exe`` and phase C drives
    ``Battle.net.exe``, so both must exist — and so must the payload the
    launcher hands off to, which is the piece an interrupted install
    leaves out.
    """
    return find_launcher_exe(prefix) is not None and client_installed(prefix)
