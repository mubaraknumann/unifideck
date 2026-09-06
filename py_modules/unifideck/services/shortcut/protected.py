"""Protected shortcut IDs — shortcuts the reconcile pass must never drop.

Auth-forwarder shortcuts (e.g. ``ubisoft:upc-auth``) are owned by
the auth flow, not the game-library flow. Without an explicit
protected-set, the reconcile pass would treat them as stale managed
entries (they match the ``<store>:<id>`` LaunchOptions shape) and
delete them after every sync — breaking the next login attempt.

Membership rule
===============
A LaunchOptions full-id is protected if either:

* it appears verbatim in :data:`PROTECTED_IDS` (exact match), or
* it starts with one of the prefixes in :data:`PROTECTED_PREFIXES`
  (catches store-internal auth shortcuts that share a launcher).

Both checks ignore the user-param suffix (``[...]``) — protection
is per-launch-target, not per-user-permutation.
"""
from __future__ import annotations

# Exact full-ids that must be preserved. Singular per store, but
# new stores might add their own; extend this set rather than
# scattering string literals across reconcile code.
PROTECTED_IDS: frozenset[str] = frozenset({
    "ubisoft:upc-auth",
    "battlenet:bnet-auth",
    "epic:epic-auth",
    "gog:gog-auth",
    "amazon:amazon-auth",
    # NOTE: Microsoft has no protected auth id. Its 0.7 auth flow uses
    # ephemeral, frontend-managed shortcuts (``microsoft:ms-auth`` /
    # ``microsoft:ms-auth-temp-*``) that never reach shortcuts.vdf, so
    # there is nothing to protect here. The old persistent 0.6.x
    # ``microsoft:ms-auth`` row MUST stay sweepable so reconcile can
    # remove it on the next sync — do not add it back. It is listed in
    # :data:`LEGACY_SWEEP_IDS` instead.
})

# The exact inverse of :data:`PROTECTED_IDS`: full-ids from an earlier
# release that are always stale, whatever the current library says.
#
# These exist because the reconcile sweep is otherwise gated on the store
# having answered this sync (see ``events._sweepable_stores``), and this
# row belongs to a store the affected user has usually never signed into
# — someone who upgraded from 0.6.x and left Microsoft alone. Gating the
# whole store on availability so one dead row gets collected is what made
# the sweep able to delete a signed-out store's entire library. Naming
# the row is the narrow fix; widening the rule was the wide one.
#
# Add to this set only for an id no current code path can create. If a
# live flow can still produce it, it is not legacy — fix the flow.
LEGACY_SWEEP_IDS: frozenset[str] = frozenset({
    # 0.6.x wrote a persistent Microsoft auth shortcut; 0.7's auth flow
    # is frontend-managed and ephemeral, so nothing recreates this.
    "microsoft:ms-auth",
})

# Prefix-protected — when an auth shortcut uses a per-session id
# (e.g. ``store:auth-2026-05-18T...``) the suffix changes each
# time but the prefix is stable.
PROTECTED_PREFIXES: tuple[str, ...] = (
    "auth-",
)


def is_protected(full_id: str | None) -> bool:
    """Return True if ``full_id`` matches the protected-set or a prefix.

    Caller normally strips the ``[...]`` user-param suffix before
    calling (see :func:`launch_options.get_full_id`).
    """
    if not full_id:
        return False
    if full_id in PROTECTED_IDS:
        return True
    # Match on the id portion after the ``store:`` prefix so
    # ``epic:auth-xyz`` reads as protected.
    parts = full_id.split(":", 1)
    return len(parts) == 2 and any(
        parts[1].startswith(prefix) for prefix in PROTECTED_PREFIXES
    )


def is_legacy_sweepable(full_id: str | None) -> bool:
    """Return True for a :data:`LEGACY_SWEEP_IDS` member.

    Callers must still check :func:`is_protected` first — protection
    always wins. The two sets are asserted disjoint by
    ``test_sync_failed_store_keeps_shortcuts``, because an id in both
    would resolve differently depending on call order.
    """
    return bool(full_id) and full_id in LEGACY_SWEEP_IDS
