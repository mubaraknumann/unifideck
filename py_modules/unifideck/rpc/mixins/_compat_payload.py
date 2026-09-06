"""Project cached compat entries onto the wire, per device.

py_modules/unifideck/rpc/mixins/_compat_payload.py

The cache holds every device's rating (see
``compatibility/deck_verified.py``). What each RPC ships differs, and
the split is by payload size rather than by principle:

* ``get_protondb_cache`` — the whole library at boot. Ships **only the
  active device's** status. Resolving device-side rather than in the
  frontend is not a preference: ``loadDeviceType()`` is async and its
  answer can land after the compat cache is consumed, so a frontend
  resolution would mis-filter the first render on a Steam Machine.
* ``get_overview_enrichment`` — per shortcut. Ships the resolved
  category **plus every track's raw int**, because Steam's own library
  filters read bits we do not otherwise control.
* ``get_game_metadata_display`` — one game, on demand. Ships every
  track; the cost is irrelevant at one game and it leaves room for
  showing "Verified on Deck / Playable on Machine" without a new RPC.

The device-specific ladder lives here and only here. The frontend never
branches on device for compatibility — only for labels.
"""

from __future__ import annotations

from typing import Any

from unifideck.compatibility.deck_verified import (
    TRACK_NAMES,
    compat_track_for,
    spec_for,
)
from unifideck.utils.device import detect_device_type

#: ProtonDB tiers we treat as good enough to call a title Playable when
#: Valve has not rated it. Deliberately *not* Steam's own Great-on-Deck
#: rule — this promotion is ours and predates the multi-device work.
_OPTIMISTIC_TIERS = ("platinum", "native")

#: The category meaning "Playable" in Valve's 4-value ladder, and
#: "Compatible" in the SteamOS 3-value one. Both are 2.
_PLAYABLE = 2


def active_track() -> str:
    """The rating track describing the device this is running on."""
    return compat_track_for(detect_device_type())


def _int(entry: dict[str, Any], key: str) -> int:
    """Read an int field from a cached entry, 0 on anything unusable."""
    try:
        return int(entry.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0


def raw_category(entry: dict[str, Any], track: str) -> int:
    """Valve's own integer for ``track``, no bump applied.

    Entries written before the per-track fields existed carry only a
    status *string*, so fall back to reversing that. Without this, a
    warm cache reads as Unknown for every title in the window between
    startup and the schema self-heal — which on a Deck would be a
    visible regression from a change that is meant to leave it alone.
    """
    category = _int(entry, f"{track}_category")
    if category > 0:
        return category
    status = str(entry.get(f"{track}_status", "") or "").lower()
    spec = spec_for(track)
    if spec is None or not status or status == "unknown":
        return 0
    for value, name in spec.statuses.items():
        if name == status:
            return value
    return 0


def raw_status(entry: dict[str, Any], track: str) -> str:
    """Valve's own status word for ``track``, no bump applied."""
    return str(entry.get(f"{track}_status", "") or "unknown").lower()


#: Tracks our ProtonDB inference is allowed to speak for at all.
#:
#: Frame is excluded outright: a desktop-Linux ProtonDB report says
#: nothing about an ARM64 VR headset, so there is no basis for an
#: opinion. The other three are x86 desktop-class Proton, which is
#: exactly what a ProtonDB report measures — including SteamOS, where
#: the inference is arguably strongest.
_INFERABLE_TRACKS = frozenset({"deck", "machine", "steamos"})


def _bumped(entry: dict[str, Any], track: str) -> bool:
    """Whether our ProtonDB inference applies to ``track`` for ``entry``."""
    if track not in _INFERABLE_TRACKS:
        return False
    return str(entry.get("protondb_tier", "") or "").lower() in _OPTIMISTIC_TIERS


def compat_category(entry: dict[str, Any], track: str) -> int:
    """Valve's rating for ``track``, with our ProtonDB-optimism bump.

    Structurally identical to the Deck-only version this replaced —
    only the field prefix is now a parameter. There is no cross-device
    fallback: Valve rates each device independently, and a measured
    300-title sample diverges in both directions, so borrowing another
    device's verdict would invent an answer.

    Feeds the compatibility tab's filter, not the badge. See
    :func:`compat_block` for why the badge must not carry the bump.
    """
    category = raw_category(entry, track)
    if category > 0:
        return category
    return _PLAYABLE if _bumped(entry, track) else 0


def compat_status(entry: dict[str, Any], track: str) -> str:
    """Our status word for ``track``, honouring the same bump."""
    status = raw_status(entry, track)
    if status != "unknown":
        return status
    if _bumped(entry, track):
        # The SteamOS track calls this rung "compatible"; the 4-value
        # tracks call it "playable". Name it the way the track does.
        return "compatible" if track == "steamos" else "playable"
    return "unknown"


#: Tracks whose bits may carry our inference as well as Valve's verdict.
#:
#: Narrower than :data:`_INFERABLE_TRACKS`, and for a different reason.
#: These integers go into Steam's OWN field, where a 2 is read as
#: "Valve rated this Playable". On the Deck and Machine ladders that is
#: what our bump means, so writing it is honest. The SteamOS track's 2
#: means something else entirely ("runs on SteamOS"), so putting our
#: inference there would be a claim Steam's filter misreads.
_BUMPABLE_BITS = frozenset({"deck", "machine"})


def compat_categories(entry: dict[str, Any]) -> dict[str, int]:
    """Every track's category int, for the packed bitfield.

    These go straight into Steam's own
    ``steam_hw_compat_category_packed`` slots, so anything here has to be
    a number Steam itself would have written. Valve's own value always
    is; our ProtonDB bump only is for the tracks in
    :data:`_BUMPABLE_BITS`.
    """
    return {
        name: (
            compat_category(entry, name)
            if name in _BUMPABLE_BITS
            else raw_category(entry, name)
        )
        for name in TRACK_NAMES
    }


def track_test_results(
    entry: dict[str, Any], track: str,
) -> list[dict[str, Any]]:
    """Per-test rows for ``track`` as ``{token, passed}``.

    Named ``track_test_results`` rather than ``test_results``: pytest
    collects any imported callable whose name starts with ``test_``,
    so the shorter name turned every test module that imported it into
    a spurious "fixture 'entry' not found" error.

    Both shapes are accepted. Entries written before the multi-device
    work hold ``{text, passed}`` with pre-resolved English; new ones
    hold Valve's ``loc_token``, which the frontend localises through the
    Steam client. Passing both through means a warm cache keeps
    rendering until the schema self-heal rewrites it.
    """
    rows = entry.get(f"{track}_test_results")
    if not isinstance(rows, list):
        return []
    out: list[dict[str, Any]] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        token = item.get("token")
        text = item.get("text")
        row: dict[str, Any] = {"passed": bool(item.get("passed"))}
        if isinstance(token, str) and token:
            row["token"] = token
        elif isinstance(text, str) and text:
            row["text"] = text
        else:
            continue
        out.append(row)
    return out


def compat_block(entry: dict[str, Any]) -> dict[str, Any]:
    """Every track, for the single-game metadata payload.

    **Valve's verdict only — the ProtonDB bump is deliberately not
    applied here.** This feeds the info-panel badge and the Details
    modal, which present a rating in Valve's own vocabulary
    ("VERIFIED", "PLAYABLE") under a "<device> Compatibility" heading.
    Showing our own inference there would put words in Valve's mouth,
    and the modal would render a PLAYABLE pill above an empty
    test-result list because there is no report behind it.

    This also preserves the pre-multi-device Deck behaviour exactly: the
    old display path (``deck_compat_enum``) read only ``deck_status``
    and had no bump, while the *facet* path (``_deck_category``) did.
    That asymmetry is intentional and load-bearing — the compatibility
    tab may include a title on our inference, but the badge will not
    claim Valve rated it.
    """
    return {
        name: {
            "category": raw_category(entry, name),
            "status": raw_status(entry, name),
            "test_results": track_test_results(entry, name),
        }
        for name in TRACK_NAMES
    }


def slim_cache_entry(entry: dict[str, Any], track: str) -> dict[str, Any]:
    """One library-wide cache row, reduced to what the frontend reads.

    Smaller than what this RPC used to return — it shipped the entire
    cached entry, test-result prose included, of which the consumer read
    four keys.
    """
    return {
        "title": entry.get("title", ""),
        "protondb_tier": entry.get("protondb_tier"),
        "compat_status": compat_status(entry, track),
        "sources": entry.get("sources", []),
    }
