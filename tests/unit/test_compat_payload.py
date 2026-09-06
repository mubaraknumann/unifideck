"""Per-device projection of a cached compat entry onto the wire.

These cover what the linters and the type checker cannot: which track a
number came from, and whether a number we *inferred* is allowed to be
written into a slot Steam reads as Valve's own verdict.
"""

from __future__ import annotations

from typing import Any

import pytest

from unifideck.compatibility.deck_verified import TRACK_NAMES
from unifideck.rpc.mixins._compat_payload import (
    compat_block,
    compat_categories,
    compat_category,
    compat_status,
    raw_category,
    slim_cache_entry,
    track_test_results,
)


def test_protondb_bump_never_speaks_for_the_frame() -> None:
    """A desktop-Linux ProtonDB report says nothing about an ARM64 headset.

    These ints go straight into Steam's own packed field, so a bumped
    Frame value would be us inventing a rating for a device this plugin
    does not support and handing it to Steam as fact.
    """
    entry: dict[str, Any] = {"protondb_tier": "platinum"}
    cats = compat_categories(entry)
    assert cats["deck"] == 2
    assert cats["machine"] == 2
    assert cats["frame"] == 0
    assert cats["steamos"] == 0


def test_protondb_bump_never_speaks_for_steamos() -> None:
    """The SteamOS track's integers are a different, 3-value enum."""
    assert compat_categories({"protondb_tier": "native"})["steamos"] == 0


def test_valves_own_frame_rating_is_passed_through() -> None:
    """Excluding the bump must not discard a real Valve verdict."""
    assert compat_categories({"frame_category": 3})["frame"] == 3


def test_every_track_is_always_present() -> None:
    """Callers index this by track name without guarding."""
    assert set(compat_categories({})) == set(TRACK_NAMES)


@pytest.mark.parametrize("track", ["deck", "machine"])
def test_valve_rating_beats_the_bump(track: str) -> None:
    entry = {f"{track}_category": 1, "protondb_tier": "platinum"}
    assert compat_category(entry, track) == 1


def test_raw_category_reverses_a_status_only_entry() -> None:
    """Warm caches predate the per-track ints and carry only strings."""
    assert raw_category({"deck_status": "verified"}, "deck") == 3
    assert raw_category({"steamos_status": "compatible"}, "steamos") == 2
    assert raw_category({"deck_status": "unknown"}, "deck") == 0
    assert raw_category({}, "deck") == 0


def test_raw_category_survives_junk() -> None:
    for bad in (None, "", "banana", {}, []):
        assert raw_category({"deck_category": bad}, "deck") == 0


def test_status_bump_uses_each_tracks_own_word() -> None:
    """SteamOS calls the middle rung 'compatible'; the others 'playable'."""
    entry = {"protondb_tier": "platinum"}
    assert compat_status(entry, "deck") == "playable"
    assert compat_status(entry, "machine") == "playable"
    assert compat_status(entry, "steamos") == "compatible"


def test_test_results_accept_both_cache_shapes() -> None:
    """Old entries hold resolved English; new ones hold Valve's token."""
    entry = {"deck_test_results": [
        {"token": "#SteamDeckVerified_TestResult_X", "passed": True},
        {"text": "legacy prose", "passed": False},
        {"passed": True},              # neither -> dropped, nothing to show
        "not a dict",
    ]}
    rows = track_test_results(entry, "deck")
    assert rows == [
        {"passed": True, "token": "#SteamDeckVerified_TestResult_X"},
        {"passed": False, "text": "legacy prose"},
    ]


def test_test_results_of_a_cold_entry_is_empty_not_an_error() -> None:
    assert track_test_results({}, "machine") == []
    assert track_test_results({"machine_test_results": "junk"}, "machine") == []


def test_slim_entry_carries_only_what_the_frontend_reads() -> None:
    entry = {
        "title": "T", "protondb_tier": "gold", "sources": ["deck_verified"],
        "deck_status": "verified",
        "deck_test_results": [{"token": "#x", "passed": True}],
    }
    assert slim_cache_entry(entry, "deck") == {
        "title": "T",
        "protondb_tier": "gold",
        "compat_status": "verified",
        "sources": ["deck_verified"],
    }


def test_display_block_agrees_with_the_bitfield_on_valves_own_values() -> None:
    """Where Valve rated it, every consumer must report the same number.

    They diverge only where our inference applies, and that divergence
    is the point: the filter may act on it, the badge may not.
    """
    entry = {"machine_category": 3, "deck_category": 2, "steamos_category": 2}
    block = compat_block(entry)
    cats = compat_categories(entry)
    for track in TRACK_NAMES:
        assert block[track]["category"] == cats[track], track


# ── the display payload must carry Valve's verdict only ───────────────
#
# Regression guard for a real defect found in adversarial review: the
# badge and Details modal were briefly fed the bumped category, so a
# Deck showed a yellow PLAYABLE pill — in Valve's vocabulary, under a
# "Steam Deck Compatibility" heading, above an empty test-result list —
# for titles Valve had never rated.


def test_display_block_never_shows_our_inference() -> None:
    """The pre-change display path had no bump. It must stay that way."""
    entry = {
        "deck_status": "unknown", "deck_category": 0,
        "protondb_tier": "platinum", "deck_test_results": [],
    }
    block = compat_block(entry)["deck"]
    assert block["category"] == 0
    assert block["status"] == "unknown"
    # ...while the tab filter still includes it, exactly as before.
    assert compat_category(entry, "deck") == 2


def test_display_block_still_shows_valves_own_verdict() -> None:
    entry = {"machine_status": "verified", "machine_category": 3}
    assert compat_block(entry)["machine"]["category"] == 3
    assert compat_block(entry)["machine"]["status"] == "verified"


def test_inference_never_speaks_for_the_frame_in_any_field() -> None:
    """Category AND status — the first fix closed only the category."""
    entry = {"protondb_tier": "platinum"}
    assert compat_category(entry, "frame") == 0
    assert compat_status(entry, "frame") == "unknown"
    assert compat_categories(entry)["frame"] == 0


def test_steamos_inference_reaches_our_filter_but_not_steams_bits() -> None:
    """Two different questions, two different answers, both deliberate.

    Our own filter may act on a ProtonDB report for SteamOS — it is x86
    desktop Linux, exactly what ProtonDB measures. Steam's bitfield may
    not, because a 2 there is read as Valve's own "runs on SteamOS".
    """
    entry = {"protondb_tier": "native"}
    assert compat_status(entry, "steamos") == "compatible"
    assert compat_category(entry, "steamos") == 2
    assert compat_categories(entry)["steamos"] == 0
