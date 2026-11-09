"""``core.store_urls`` one-time shop page: what the next shop window opens.

The file decides what a signed-in browser opens, so only a fresh https
page on the store's own site is honoured, and only once.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from unifideck.core.store_urls import storefront_target_path, take_storefront_target


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))


def _leave(store: str, url: str, *, age_s: float = 0) -> Path:
    path = storefront_target_path(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(url)
    if age_s:
        stamp = time.time() - age_s
        os.utime(path, (stamp, stamp))
    return path


@pytest.mark.parametrize("url", [
    "https://itch.io/my-purchases/bundles",
    "https://someone.itch.io/sky-rogue",
])
def test_a_fresh_page_on_the_store_site_is_taken_once(url: str) -> None:
    path = _leave("itch", url)
    assert take_storefront_target("itch") == url
    assert not path.exists()
    assert take_storefront_target("itch") == ""


@pytest.mark.parametrize("url", [
    "http://itch.io/my-purchases/bundles",          # not https
    "https://evil.example/itch.io",                 # another site
    "https://itch.io.evil.example/",                # look-alike host
])
def test_a_page_off_the_store_site_is_dropped(url: str) -> None:
    path = _leave("itch", url)
    assert take_storefront_target("itch") == ""
    assert not path.exists()


def test_a_stale_page_is_dropped() -> None:
    _leave("itch", "https://itch.io/my-purchases/bundles", age_s=3600)
    assert take_storefront_target("itch") == ""


def test_a_store_without_a_shop_takes_nothing() -> None:
    _leave("ubisoft", "https://store.ubisoft.com/")
    assert take_storefront_target("ubisoft") == ""
