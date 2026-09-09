"""Is this ``shortcuts.vdf`` entry a Unifideck shortcut we should delete?

py_modules/unifideck/services/shortcut/stale_predicate.py

Split out of ``reconcile_phases.py`` (2026-08-26) to keep that file under
the 550 LOC volumetry cap. Pure decision, no service state — which is how
it was already being tested, directly, by two test modules.

The decision is the most destructive one in the package, so it is layered
deliberately and the order is load-bearing:

1. **protected** — auth forwarders are owned by the auth flow, never here;
2. **managed** — is this row ours at all;
3. **ownership** — does its ``Exe`` point at our launcher (UD-006);
4. **legacy identity** — stale whatever the library says;
5. **library comparison** — store answered, and this appid is not in it.
"""

from __future__ import annotations

from typing import Any, NewType

from .games_map import UNIFIDECK_TAG
from .orphan_scan import _is_launcher_exe
from .protected import is_legacy_sweepable, is_protected


def is_managed_sweepable(entry: dict[str, Any], full_id: Any) -> bool:
    """True if *entry* is a Unifideck-managed, non-protected shortcut.

    The protected/auth early-exit + managed-by-options-or-tag check, kept
    separate from :func:`is_stale_managed_shortcut` so that one stays
    under the cognitive-complexity cap. Returns ``False`` for
    protected/auth shortcuts and for entries we never managed.
    """
    # Centralised protected-set check — replaces the previous hardcoded
    # ``ubisoft:upc-auth`` literal so new stores can register their auth
    # shortcuts in one place.
    if is_protected(full_id):
        return False
    tags = entry.get("tags", {})
    tags_dict = tags if isinstance(tags, dict) else {}
    is_auth_tag = any(str(t).startswith("auth-") for t in tags_dict.values())
    if is_auth_tag:
        return False
    is_managed_by_options = full_id is not None
    is_managed_by_tag = any(t == UNIFIDECK_TAG for t in tags_dict.values())
    return is_managed_by_options or is_managed_by_tag


#: The stores a sweep is allowed to delete shortcuts for.
#:
#: A ``NewType`` rather than a bare ``set[str]`` so the wrong call cannot be
#: written. §3.5 finding B was not a missing guard — the guard existed and
#: was documented ("how staging avoided nuking the user's Epic shortcuts
#: after they logged out of Epic") — it was a **caller that widened it** to
#: every registered store. A store then contributes zero games in four ways
#: without owning zero games (it raised, it timed out, it was never fetched,
#: or it returned an empty list), and each deleted every shortcut it owned.
#:
#: Only :func:`services.shortcut.events._sweepable_stores` constructs this,
#: so mypy rejects ``reconcile(games, valid_stores=set(registry.store_ids()))``
#: — the exact line that caused the incident. Audit register item 30; per
#: §2.1, prefer making the wrong call impossible over testing that it is not
#: made.
SweepableStores = NewType("SweepableStores", frozenset[str])


def is_stale_managed_shortcut(
    entry: Any,
    valid_app_ids: set[int],
    valid_stores: SweepableStores | None = None,
    launcher_path: str = "",
) -> bool:
    """True if ``entry`` is a Unifideck-managed shortcut no longer needed.

    Ownership is decided on the shortcut's ``Exe`` field: only entries
    whose ``Exe`` points at our ``bin/unifideck-launcher`` are ever swept
    (via :func:`orphan_scan._is_launcher_exe`, a basename match — the
    plugin dir differs across installs). A foreign shortcut
    (NonSteamLaunchers', or a manually-added one) can carry a
    ``"<store>:<id>"``-shaped ``LaunchOptions`` token or even our stale
    ``UNIFIDECK_TAG``, so LaunchOptions/tags alone cannot distinguish it
    from ours — matching on them deleted the user's own non-Steam
    shortcuts (UD-006). The launcher ``Exe`` is the one marker a foreign
    scanner cannot forge.

    Beyond the Exe gate, identification of *which* Unifideck games a
    shortcut maps to stays **LaunchOptions-based** (regex on
    ``"<store>:<game_id>"``) rather than tag-based — Steam can strip our
    ``UNIFIDECK_TAG`` on update / by user edit, but it preserves
    ``LaunchOptions`` reliably.

    Auth shortcuts (``ubisoft:upc-auth`` and any ``auth-*``-tagged entry)
    are explicitly preserved — their lifecycle is owned by
    ``services/shortcut/shortcut.py``.

    When ``valid_stores`` is supplied, only sweep shortcuts whose store
    prefix is in that set — this is how staging avoided nuking the user's
    Epic shortcuts after they logged out of Epic. The post-sync caller
    passes the stores that *answered* (``events._sweepable_stores``), so
    one that raised, timed out, could not read or was never fetched keeps
    its shortcuts (audit §3.5, finding B). A
    :data:`~.protected.LEGACY_SWEEP_IDS` member bypasses that gate.
    """
    from .launch_options import get_full_id, get_store_prefix

    if not isinstance(entry, dict):
        return False
    launch = entry.get("LaunchOptions", "") or ""
    full_id = get_full_id(launch) if isinstance(launch, str) else None
    if not is_managed_sweepable(entry, full_id):
        return False
    # Ownership gate: never sweep a shortcut we didn't create. A foreign
    # shortcut whose Exe was left intact (or a Unifideck one whose Exe a
    # foreign scanner rewrote) is handed to ``orphan_scan``'s recover path
    # instead of being deleted here.
    exe_raw = entry.get("Exe") or entry.get("exe") or ""
    exe = exe_raw.strip().strip('"') if isinstance(exe_raw, str) else ""
    if not _is_launcher_exe(exe, launcher_path):
        return False
    # Stale by identity — after the protected + ownership gates.
    if is_legacy_sweepable(full_id):
        return True
    if valid_stores is not None and full_id is not None:
        store = get_store_prefix(launch)
        if store and store not in valid_stores:
            return False
    return entry.get("appid") not in valid_app_ids


__all__ = ["is_managed_sweepable", "is_stale_managed_shortcut"]
