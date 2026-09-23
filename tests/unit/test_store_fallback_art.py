"""Tests for the artwork service's store fallback phase (itch.io covers).

The store fallback runs after SteamGridDB and the Steam CDN and may only fill
kinds they left empty: an itch.io landscape cover must never replace a real
portrait capsule.
"""
from __future__ import annotations

from unifideck.services.artwork.store_metadata import (
    fetch_store_fallback_urls,
    itch_fallback_urls,
)


def test_itch_cover_fills_both_capsules() -> None:
    urls = itch_fallback_urls({"cover_url": "https://img.itch.zone/a.png"})
    assert urls == {"grid": "https://img.itch.zone/a.png", "grid_l": "https://img.itch.zone/a.png"}


def test_animated_cover_uses_the_still_version() -> None:
    urls = itch_fallback_urls({"cover_url": "https://img.itch.zone/a.GIF",
                               "still_cover_url": "https://img.itch.zone/a-still.png"})
    assert urls["grid"] == "https://img.itch.zone/a-still.png"


def test_no_cover_means_no_fallback() -> None:
    assert itch_fallback_urls({}) == {}
    assert itch_fallback_urls({"cover_url": ""}) == {}


def test_other_stores_have_no_fallback() -> None:
    assert fetch_store_fallback_urls("epic", {"cover_url": "https://x"}) == {}
    assert fetch_store_fallback_urls("itch", None) == {}
