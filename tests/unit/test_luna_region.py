"""``luna_api.regions``: one region row per Luna country site, and detection.

The rows were read from each site's own page config (``/?g=s``) on
2026-11-16; the expectations below pin those values.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from unifideck.stores.luna.luna_api import regions as regions_mod
from unifideck.stores.luna.luna_api import resolve_region
from unifideck.stores.luna.luna_api.regions import (
    LUNA_HOSTS,
    SIGN_IN_FALLBACK_HOST,
    detect_region,
    is_luna_url,
    region_for_cookie_hosts,
)

_DEFAULTS = Path(__file__).resolve().parents[2] / "defaults" / "config.json"
_EU = "proxy-prod.eu-west-1.tempo.digital.a2z.com"
_NA = "proxy-prod.us-east-1.tempo.digital.a2z.com"


def test_de_is_the_verified_row() -> None:
    region = resolve_region("https://luna.amazon.de")
    assert region is not None
    assert region.verified is True
    assert region.cookie_suffix == "amazon.de"
    assert region.marketplace_id == "A1PA6795UKMFR9"
    assert region.country == "DE"
    assert region.locale == "de_DE"
    assert region.tempo_host == _EU
    assert region.site == "https://luna.amazon.de"


@pytest.mark.parametrize(
    ("host", "marketplace", "locale", "tempo"),
    [
        ("luna.amazon.com", "ATVPDKIKX0DER", "en_US", _NA),
        ("luna.amazon.ca", "A2EUQ1WTGCTBG2", "en_CA", _NA),
        ("luna.amazon.co.uk", "A1F83G8C2ARO7P", "en_GB", _EU),
        ("luna.amazon.fr", "A13V1IB3VIYZZH", "fr_FR", _EU),
        ("luna.amazon.it", "APJ6JRA9NG5V4", "it_IT", _EU),
        ("luna.amazon.es", "A1RKKUPIHCS9HS", "es_ES", _EU),
        ("luna.amazon.pl", "A1C3SOZRARQ6R3", "pl_PL", _EU),
        ("luna.amazon.nl", "A1805IZSGTT6HS", "nl_NL", _EU),
        ("luna.amazon.se", "A2NODRKZP88ZB9", "sv_SE", _EU),
        ("luna.amazon.com.be", "AMEN7PMS3EDWL", "nl_NL", _EU),
    ],
)
def test_site_config_values(host: str, marketplace: str, locale: str, tempo: str) -> None:
    region = resolve_region(host)
    assert region is not None
    assert (region.marketplace_id, region.locale, region.tempo_host) == (
        marketplace, locale, tempo,
    )
    assert region.cookie_suffix == host.removeprefix("luna.")


@pytest.mark.parametrize("host", [
    "luna.amazon.de", "https://luna.amazon.de/", "HTTPS://LUNA.AMAZON.DE", "luna.amazon.de/x",
])
def test_host_forms_resolve(host: str) -> None:
    region = resolve_region(host)
    assert region is not None and region.luna_host == "luna.amazon.de"


@pytest.mark.parametrize("host", ["", "https://luna.amazon.co.jp", "luna.amazon.de.evil.example"])
def test_unmapped_host_is_none(host: str) -> None:
    assert resolve_region(host) is None


def test_every_allowed_stream_host_has_a_row_and_back() -> None:
    luna = json.loads(_DEFAULTS.read_text(encoding="utf-8"))["stores"]["luna"]
    assert sorted(luna["allowed_stream_hosts"]) == sorted(LUNA_HOSTS)
    # Empty luna_host = auto (region from the profile's sign-in).
    assert luna["luna_host"] == ""
    assert SIGN_IN_FALLBACK_HOST in LUNA_HOSTS


def test_only_de_is_marked_verified() -> None:
    verified = [h for h in LUNA_HOSTS if resolve_region(h).verified]  # type: ignore[union-attr]
    assert verified == ["luna.amazon.de"]


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://luna.amazon.de/game/x/B0", True),
        ("https://luna.amazon.com.be/", True),
        ("https://www.amazon.de/ap/signin", False),
        ("https://luna.amazon.de.evil.example/", False),
    ],
)
def test_is_luna_url(url: str, expected: bool) -> None:
    assert is_luna_url(url) is expected


@pytest.mark.parametrize(
    ("hosts", "expected"),
    [
        ({"amazon.de", "www.amazon.de"}, "luna.amazon.de"),
        ({"amazon.com"}, "luna.amazon.com"),
        ({"amazon.com.be"}, "luna.amazon.com.be"),
        ({"amazon.co.jp"}, None),
        (set(), None),
    ],
)
def test_region_for_cookie_hosts(hosts: set[str], expected: str | None) -> None:
    region = region_for_cookie_hosts(hosts)
    assert (region.luna_host if region else None) == expected


def test_detect_region_reads_auth_cookie_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    import unifideck.auth.edge_browser.cookie_reader as reader

    seen: list[tuple[str, tuple[str, ...]]] = []

    def fake_hosts(profile: str, prefixes: tuple[str, ...]) -> set[str]:
        seen.append((profile, prefixes))
        return {"amazon.de"}

    monkeypatch.setattr(reader, "cookie_hosts", fake_hosts)
    region = detect_region("/profile")
    assert region is not None and region.country == "DE"
    assert seen == [("/profile", regions_mod._AUTH_COOKIE_PREFIXES)]
