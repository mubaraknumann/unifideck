"""Valve's per-device compatibility report, parsed into four tracks.

py_modules/unifideck/compatibility/deck_verified.py

Steam's ``ajaxgetdeckappcompatibilityreport`` endpoint is named after the
Deck but has not been Deck-only for some time. One response carries four
independent ratings::

    resolved_category         / resolved_items          -> Steam Deck
    steamos_resolved_category / steamos_resolved_items  -> generic SteamOS
    machine_resolved_category / machine_resolved_items  -> Steam Machine
    frame_resolved_category   / frame_resolved_items    -> Steam Frame

**They are independent per title**, which Valve states outright, and
measurement agrees: sampling 300 titles from a real library found 21 that
Valve rates Playable on Deck and Verified on Machine, and 7 rated
Unsupported on Deck that carry no Machine rating at all. Serving the Deck
record on a Steam Machine is therefore wrong per-title, not merely
mislabelled.

Two shapes to keep straight:

* ``DECK_CATEGORIES`` is Valve's 4-value ladder, used by the Deck,
  Machine and Frame tracks.
* ``STEAMOS_CATEGORIES`` is a **3-value** enum -- across those same 300
  titles the SteamOS track never emitted ``3``. It answers "does this run
  on SteamOS", not "how well does it run on this device", so its integers
  must never be routed through the Deck ladder.

:data:`TrackSpec.packed_shift` is the offset of that track inside Steam's
own ``AppOverview.steam_hw_compat_category_packed`` bitfield, read off
the client's getters. Storing the raw integer alongside our status string
is deliberate: the bitfield needs Valve's number, our UI needs our word,
and deriving either from the other is how the SteamOS bits get corrupted.

Stdlib only, never raises.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from unifideck.utils.device import DeviceType

#: Valve's 4-value compatibility ladder (Deck, Machine, Frame).
DECK_CATEGORIES: dict[int, str] = {
    0: "unknown",
    1: "unsupported",
    2: "playable",
    3: "verified",
}

#: The SteamOS track's 3-value enum. Measured, not documented: across a
#: 300-title sample it emitted only 0, 1 and 2.
STEAMOS_CATEGORIES: dict[int, str] = {
    0: "unknown",
    1: "unsupported",
    2: "compatible",
}

TRACK_DECK = "deck"
TRACK_STEAMOS = "steamos"
TRACK_MACHINE = "machine"
TRACK_FRAME = "frame"

#: ``display_type`` in a ``*_resolved_items`` entry meaning "passed"
#: (green check). Anything else is a warning. Track-agnostic.
PASSED_DISPLAY_TYPE = 4


@dataclass(frozen=True)
class TrackSpec:
    """How to read one device's rating out of the shared response."""

    track: str
    category_key: str
    items_key: str
    statuses: dict[int, str]
    packed_shift: int


#: The four tracks, in bitfield order. Adding a device means adding a row
#: here and nothing else -- every consumer loops over this.
TRACKS: tuple[TrackSpec, ...] = (
    TrackSpec(
        TRACK_DECK, "resolved_category", "resolved_items",
        DECK_CATEGORIES, 0,
    ),
    TrackSpec(
        TRACK_STEAMOS, "steamos_resolved_category", "steamos_resolved_items",
        STEAMOS_CATEGORIES, 4,
    ),
    TrackSpec(
        TRACK_MACHINE, "machine_resolved_category", "machine_resolved_items",
        DECK_CATEGORIES, 6,
    ),
    TrackSpec(
        TRACK_FRAME, "frame_resolved_category", "frame_resolved_items",
        DECK_CATEGORIES, 8,
    ),
)

TRACK_NAMES: tuple[str, ...] = tuple(spec.track for spec in TRACKS)

_BY_NAME: dict[str, TrackSpec] = {spec.track: spec for spec in TRACKS}


@dataclass
class TrackResult:
    """One device's rating.

    ``test_results`` holds Valve's ``loc_token`` rather than English
    prose. The Steam client already ships every one of these strings
    translated into the user's language; resolving them here would mean
    maintaining ~90 hand-written English strings that go stale the next
    time Valve ships hardware.
    """

    category: int = 0
    status: str = "unknown"
    test_results: list[dict[str, Any]] = field(default_factory=list)


#: Which rating track describes each device class. Unrecognised
#: hardware gets the generic SteamOS track rather than being told how
#: well its games run on a handheld it does not own.
_DEVICE_TRACKS: dict[DeviceType, str] = {
    DeviceType.DECK: TRACK_DECK,
    DeviceType.MACHINE: TRACK_MACHINE,
    DeviceType.OTHER: TRACK_STEAMOS,
}


def compat_track_for(device: DeviceType) -> str:
    """The rating track to show on ``device``."""
    return _DEVICE_TRACKS.get(device, TRACK_STEAMOS)


def spec_for(track: str) -> TrackSpec | None:
    """The :class:`TrackSpec` named ``track``, or ``None``."""
    return _BY_NAME.get(track)


def _category(results: dict[str, Any], key: str) -> int:
    """Read one track's category int. 0 for missing, null or non-numeric.

    ``frame_resolved_category`` is ``null`` for almost every title today,
    so a missing value is the common case, not an error.
    """
    raw = results.get(key)
    if raw is None:
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _items(results: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """Read one track's test-result entries as ``{token, passed}``.

    Entries without a ``loc_token`` are dropped: without a token there is
    nothing to render, and inventing prose for one is how a stale English
    table starts.
    """
    raw = results.get(key)
    if not isinstance(raw, list):
        return []
    parsed: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        token = str(entry.get("loc_token", "")).strip()
        if not token:
            continue
        parsed.append({
            "token": token,
            "passed": entry.get("display_type") == PASSED_DISPLAY_TYPE,
        })
    return parsed


def parse_compat_response(payload: dict[str, Any]) -> dict[str, TrackResult]:
    """Parse every device track out of one compatibility report.

    Always returns an entry for every track in :data:`TRACKS`, so callers
    never have to guard a lookup. A malformed or truncated payload yields
    all-unknown rather than raising -- an older Steam build that answers
    with only ``resolved_category`` is a supported shape, not an error.
    """
    empty = {spec.track: TrackResult() for spec in TRACKS}
    if not isinstance(payload, dict):
        return empty  # type: ignore[unreachable]
    results = payload.get("results")
    if not isinstance(results, dict):
        return empty
    parsed: dict[str, TrackResult] = {}
    for spec in TRACKS:
        category = _category(results, spec.category_key)
        parsed[spec.track] = TrackResult(
            category=category,
            status=spec.statuses.get(category, "unknown"),
            test_results=_items(results, spec.items_key),
        )
    return parsed
