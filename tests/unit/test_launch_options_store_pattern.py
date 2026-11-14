"""``STORE_ID_PATTERN`` must know every store.

A store missing from it has no parseable ``"<store>:<game_id>"`` in its
shortcuts' launch options, so the boot orphan scan classifies every one of its
shortcuts as broken and the frontend removes them right after the sync that
added them (EA, 2026-11-15: 4 shortcuts added, then ``orphan-scan 4 to delete``).
"""
from __future__ import annotations

import re

from unifideck.bootstrap.cache_registry import _STORE_CACHES
from unifideck.services.shortcut.launch_options import STORE_ID_PATTERN, get_full_id


def test_pattern_store_list_equals_the_store_set() -> None:
    alternation = re.match(r"\\b\(([^)]*)\):", STORE_ID_PATTERN.pattern)
    assert alternation is not None
    assert set(alternation.group(1).split("|")) == set(_STORE_CACHES)


def test_every_store_id_parses() -> None:
    for store in _STORE_CACHES:
        assert get_full_id(f"{store}:Abc_1.2-3") == f"{store}:Abc_1.2-3"
    assert get_full_id("ea:OFB-EAST__59474 MANGOHUD=1") == "ea:OFB-EAST__59474"
