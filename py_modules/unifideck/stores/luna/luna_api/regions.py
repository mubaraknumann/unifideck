"""Per-region constants for the Luna web API.

Every Luna country site talks to its own regional "tempo" backend and expects
that country's Amazon marketplace id, so the values cannot be derived from the
host alone. They are kept in one frozen table keyed by the Luna host.

Only ``luna.amazon.de`` has been exercised against a real account
(``verified=True``). The other rows use the public Amazon marketplace ids and
assume the tempo host of the region's AWS location; they are marked
``verified=False`` so nobody mistakes them for tested values. A host without a
row resolves to ``None``, which the store reports as ``region_unavailable``.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

_TEMPO_EU = "proxy-prod.eu-west-1.tempo.digital.a2z.com"
_TEMPO_NA = "proxy-prod.us-east-1.tempo.digital.a2z.com"


@dataclass(frozen=True)
class LunaRegion:
    """The constants one Luna country site needs for its API calls."""

    luna_host: str
    cookie_suffix: str
    marketplace_id: str
    country: str
    locale: str
    tempo_host: str
    verified: bool


_REGIONS: tuple[LunaRegion, ...] = (
    LunaRegion("luna.amazon.de", "amazon.de", "A1PA6795UKMFR9", "DE", "en_US", _TEMPO_EU, True),
    LunaRegion("luna.amazon.com", "amazon.com", "ATVPDKIKX0DER", "US", "en_US", _TEMPO_NA, False),
    LunaRegion("luna.amazon.co.uk", "amazon.co.uk", "A1F83G8C2ARO7P", "GB", "en_GB", _TEMPO_EU, False),
    LunaRegion("luna.amazon.fr", "amazon.fr", "A13V1IB3VIYZZH", "FR", "en_US", _TEMPO_EU, False),
    LunaRegion("luna.amazon.it", "amazon.it", "APJ6JRA9NG5V4", "IT", "en_US", _TEMPO_EU, False),
    LunaRegion("luna.amazon.es", "amazon.es", "A1RKKUPIHCS9HS", "ES", "en_US", _TEMPO_EU, False),
    LunaRegion("luna.amazon.ca", "amazon.ca", "A2EUQ1WTGCTBG2", "CA", "en_US", _TEMPO_NA, False),
)

_BY_HOST = {region.luna_host: region for region in _REGIONS}


def resolve_region(luna_host: str) -> LunaRegion | None:
    """The region row for ``luna_host`` (bare host or URL), or ``None``."""
    raw = (luna_host or "").strip()
    host = urlparse(raw).hostname if "://" in raw else raw.split("/")[0]
    return _BY_HOST.get((host or "").lower())
