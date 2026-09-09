"""Boot-time orphaned-shortcut classifier.

Handles the two orphan shapes the post-sync reconcile pass leaves behind.
Both reconcile (``reconcile_phases._is_stale_managed_shortcut``) and this
classifier now require the launcher ``Exe`` before touching a shortcut
(see :func:`_is_launcher_exe`), so neither deletes a foreign entry:

* **Type A — delete**: ``Exe`` points at our launcher but ``LaunchOptions``
  has no valid ``"<store>:<game_id>"`` token. Unrecoverable — without the
  launch id we can't know which game it is, and reconcile keys its
  add/keep/drop off that id — so it can only be deleted.
* **Type B — recover**: a valid ``"<store>:<game_id>"`` ``LaunchOptions`` but
  a missing/foreign ``Exe``. Reconcile's Exe gate deliberately skips these
  (they may be the user's own shortcut for the same game, or ours with a
  foreign scanner's Exe), so they need repointing here rather than deletion.
  The launcher path is a known constant, so the next library sync's reconcile
  restores the target once the ``Exe`` is ours again. Reported here for
  logging only — not acted on.

Pure + stateless so the decision table is unit-testable without the service.
The RPC layer (``CleanupRPCMixin.scan_orphaned_shortcuts``) feeds it the
loaded ``shortcuts.vdf`` root plus the resolved launcher path.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from unifideck.core.compat_bridge import to_unsigned

from .launch_options import extract_store_id, get_full_id
from .protected import is_protected

# Known launcher binary basenames. Production writes ``unifideck-launcher``;
# ``.py`` covers dev/test shortcuts pointing at the source script.
_LAUNCHER_BASENAMES = frozenset({"unifideck-launcher", "unifideck-launcher.py"})


def _is_launcher_exe(exe: str, launcher_path: str) -> bool:
    """True if ``exe`` (dequoted) is Unifideck's launcher binary.

    Basename match only — the plugin dir differs across installs
    (``/home/deck/homebrew/...`` vs the screenshot's ``/home/reboot/...``),
    so an absolute-path compare would miss real orphans. When
    ``launcher_path`` is supplied we also accept its exact basename, so a
    future rename of the launcher stays detectable without editing this set.
    """
    if not exe:
        return False
    name = Path(exe).name
    if name in _LAUNCHER_BASENAMES:
        return True
    return bool(launcher_path) and name == Path(launcher_path).name


def _has_auth_tag(entry: dict[str, Any]) -> bool:
    """True if any tag value marks this as an auth shortcut (``auth-*``).

    Secondary protection signal alongside :func:`protected.is_protected`,
    mirroring ``_is_stale_managed_shortcut``.
    """
    tags = entry.get("tags", {})
    if not isinstance(tags, dict):
        return False
    return any(str(t).startswith("auth-") for t in tags.values())


def classify_orphan(
    entry: Any, launcher_path: str,
) -> tuple[str, dict[str, Any]] | None:
    """Classify one ``shortcuts.vdf`` entry.

    Returns ``("delete", payload)`` / ``("recover", payload)`` for orphans,
    or ``None`` to leave the shortcut untouched (protected, healthy, or not
    ours). Evaluated protected-first — that ordering is the load-bearing
    safety rule: an auth shortcut must never fall through to a delete branch.
    """
    if not isinstance(entry, dict):
        return None

    launch = entry.get("LaunchOptions", "")
    launch = launch if isinstance(launch, str) else ""
    full_id = get_full_id(launch)

    # 1. Protected auth shortcuts — never touch (owned by the auth flow).
    if is_protected(full_id) or _has_auth_tag(entry):
        return None

    exe_raw = entry.get("Exe") or entry.get("exe") or ""
    exe = exe_raw.strip().strip('"') if isinstance(exe_raw, str) else ""
    is_launcher = _is_launcher_exe(exe, launcher_path)
    has_id = full_id is not None

    # 2. Foreign shortcut (not our launcher, no Unifideck id) — leave.
    if not is_launcher and not has_id:
        return None
    # 3. Healthy managed shortcut (our launcher + valid id) — leave.
    if is_launcher and has_id:
        return None

    # Both orphan kinds need a runtime appid to be actionable by the
    # frontend; skip entries missing/with a non-int appid.
    appid = entry.get("appid")
    if not isinstance(appid, int):
        return None
    appid_unsigned = to_unsigned(appid)

    # 4. Type A — our launcher but no resolvable id -> delete.
    if is_launcher:
        return "delete", {
            "appid_unsigned": appid_unsigned,
            "name": entry.get("AppName", ""),
        }

    # 5. Type B — valid id but missing/foreign Exe -> recover (deferred).
    store_id = extract_store_id(launch)
    store, game_id = store_id if store_id else ("", "")
    return "recover", {
        "appid_unsigned": appid_unsigned,
        "store": store,
        "game_id": game_id,
        "full_id": full_id,
        "name": entry.get("AppName", ""),
    }


def scan_orphans(
    shortcuts_root: Any, launcher_path: str,
) -> dict[str, list[dict[str, Any]]]:
    """Classify every entry in a ``shortcuts.vdf`` ``shortcuts`` root.

    ``shortcuts_root`` is the ``{"0": {...}, "1": {...}}`` mapping (the value
    under the top-level ``"shortcuts"`` key). Returns
    ``{"delete": [...], "recover": [...]}`` — never raises on malformed input.
    """
    result: dict[str, list[dict[str, Any]]] = {"delete": [], "recover": []}
    if not isinstance(shortcuts_root, dict):
        return result
    for entry in shortcuts_root.values():
        classified = classify_orphan(entry, launcher_path)
        if classified is None:
            continue
        kind, payload = classified
        result[kind].append(payload)
    return result


__all__ = ["classify_orphan", "scan_orphans"]
