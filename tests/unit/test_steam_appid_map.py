"""The one shortcut → real-Steam-AppID read — audit register item 47.

Two backfill services held a byte-identical private copy of this that tried
the **signed** AppID form alone. That was correct for their callers (both
pass ``Game.app_id``, which sync stores signed), so it was a robustness gap
rather than a live defect — the kind of duplicate that stays harmless until
someone routes a frontend-supplied AppID through it, which arrives unsigned.

These tests pin the two decisions that made this one function instead of
five: it tries **both** AppID forms, and it collapses the ``-1`` sentinel.
The three readers that preserve ``-1`` are deliberately not folded in, so a
future pass does not "finish the job" and break the sync partition's
skip-this-game signal.
"""
from __future__ import annotations

from typing import Any

import pytest

from unifideck.core.steam_appid_map import (
    STEAM_REAL_APPID_NS,
    read_positive_steam_appid,
)

SIGNED = -1867837430  # the form sync writes
UNSIGNED = 2427129866  # the form Steam's frontend hands plugins


class _Cache:
    """The one method this read touches."""

    def __init__(self, entries: dict[str, Any]) -> None:
        self.entries = entries
        self.asked: list[str] = []

    def get(self, namespace: str, key: str) -> Any:
        assert namespace == STEAM_REAL_APPID_NS
        self.asked.append(key)
        return self.entries.get(key)


# ── both AppID forms ────────────────────────────────────────────────
def test_reads_an_entry_written_under_the_signed_form() -> None:
    cache = _Cache({str(SIGNED): 945360})

    assert read_positive_steam_appid(cache, SIGNED) == 945360


def test_an_unsigned_lookup_finds_the_signed_entry() -> None:
    """The gap the two private copies had.

    Sync writes ``str(game.app_id)`` (signed); anything originating from
    ``overview.appid`` is unsigned. A single-form read is reachable from
    only one side.
    """
    cache = _Cache({str(SIGNED): 945360})

    assert read_positive_steam_appid(cache, UNSIGNED) == 945360


def test_a_signed_lookup_finds_an_unsigned_entry() -> None:
    cache = _Cache({str(UNSIGNED): 945360})

    assert read_positive_steam_appid(cache, SIGNED) == 945360


def test_the_stored_form_is_tried_first_and_short_circuits() -> None:
    """No second cache round-trip once the value is found."""
    cache = _Cache({str(SIGNED): 945360})
    read_positive_steam_appid(cache, SIGNED)

    assert cache.asked == [str(SIGNED)]


# ── the return contract ─────────────────────────────────────────────
def test_the_negative_sentinel_collapses_to_zero() -> None:
    """``-1`` means "this title has no Steam counterpart".

    Callers here ask "is there an AppID to look up", where the sentinel and
    a miss mean the same thing. Readers that must *preserve* ``-1`` — the
    sync partition's skip signal — are deliberately not routed through this.
    """
    cache = _Cache({str(SIGNED): -1})

    assert read_positive_steam_appid(cache, SIGNED) == 0


def test_a_missing_mapping_is_zero() -> None:
    assert read_positive_steam_appid(_Cache({}), SIGNED) == 0


@pytest.mark.parametrize("stored", ["945360", None, {}, [], 0.5])
def test_a_non_integer_value_is_zero(stored: Any) -> None:
    """A string AppID is a malformed entry, not a value to coerce."""
    cache = _Cache({str(SIGNED): stored})

    assert read_positive_steam_appid(cache, SIGNED) == 0


# ── never fail a backfill ───────────────────────────────────────────
def test_a_none_app_id_is_zero_without_touching_the_cache() -> None:
    cache = _Cache({})
    assert read_positive_steam_appid(cache, None) == 0
    assert cache.asked == []


def test_no_cache_at_all_is_zero() -> None:
    assert read_positive_steam_appid(None, SIGNED) == 0


def test_a_raising_cache_is_zero_not_an_exception() -> None:
    """An unregistered namespace raises ``ValueError`` by design.

    A cold or broken cache must degrade to "no mapping", never take the
    backfill down.
    """

    class _Broken:
        def get(self, namespace: str, key: str) -> Any:
            raise ValueError("cache not registered")

    assert read_positive_steam_appid(_Broken(), SIGNED) == 0
