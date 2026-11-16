"""``luna_api.regions``: one region row per allowed Luna host."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from unifideck.stores.luna.luna_api import resolve_region

_DEFAULTS = Path(__file__).resolve().parents[2] / "defaults" / "config.json"


def test_de_is_the_verified_row() -> None:
    region = resolve_region("https://luna.amazon.de")
    assert region is not None
    assert region.verified is True
    assert region.cookie_suffix == "amazon.de"
    assert region.marketplace_id == "A1PA6795UKMFR9"
    assert region.country == "DE"
    assert region.locale == "en_US"
    assert region.tempo_host == "proxy-prod.eu-west-1.tempo.digital.a2z.com"


@pytest.mark.parametrize("host", [
    "luna.amazon.de", "https://luna.amazon.de/", "HTTPS://LUNA.AMAZON.DE", "luna.amazon.de/x",
])
def test_host_forms_resolve(host: str) -> None:
    region = resolve_region(host)
    assert region is not None and region.luna_host == "luna.amazon.de"


@pytest.mark.parametrize("host", ["", "https://luna.amazon.co.jp", "luna.amazon.de.evil.example"])
def test_unmapped_host_is_none(host: str) -> None:
    assert resolve_region(host) is None


def test_every_allowed_stream_host_has_a_row() -> None:
    luna = json.loads(_DEFAULTS.read_text(encoding="utf-8"))["stores"]["luna"]
    for host in luna["allowed_stream_hosts"]:
        region = resolve_region(host)
        assert region is not None, host
        assert region.cookie_suffix == host.removeprefix("luna.")
    assert resolve_region(luna["luna_host"]) is not None


def test_only_de_is_marked_verified() -> None:
    luna = json.loads(_DEFAULTS.read_text(encoding="utf-8"))["stores"]["luna"]
    verified = [h for h in luna["allowed_stream_hosts"] if resolve_region(h).verified]  # type: ignore[union-attr]
    assert verified == ["luna.amazon.de"]
