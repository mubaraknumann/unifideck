"""Per-region constants for the Luna web API, and which region a profile uses.

Every Luna country site talks to its own regional "tempo" backend and expects
that country's Amazon marketplace id, so the values cannot be derived from the
host alone. They are kept in one frozen table keyed by the Luna host.

The marketplace id, default locale and tempo host of every row were read from
the site's own page config (``https://<luna host>/?g=s`` serves
``marketplace: "…"``, ``locale: "…"`` and the tempo host it calls) on
2026-11-16. ``?g=s`` stops Luna's geo redirect, which otherwise sends every
country site to the visitor's own one. Only ``luna.amazon.de`` has also been
exercised end to end with a signed-in account (``verified=True``).

Which row applies is not a setting: Luna itself picks the country by the
visitor's location, and after sign-in the account's Amazon session cookies
sit on that country's domain. :func:`detect_region` reads that from the
browser profile; before any sign-in :data:`SIGN_IN_FALLBACK_HOST` is used,
and Luna's geo redirect lands the user on their own country.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_TEMPO_EU = "proxy-prod.eu-west-1.tempo.digital.a2z.com"
_TEMPO_NA = "proxy-prod.us-east-1.tempo.digital.a2z.com"

#: Where a sign-in starts when no region is known yet. Luna geo-redirects it
#: to the visitor's own country site.
SIGN_IN_FALLBACK_HOST = "luna.amazon.com"


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

    @property
    def site(self) -> str:
        """The site URL, e.g. ``https://luna.amazon.de``."""
        return f"https://{self.luna_host}"


_REGIONS: tuple[LunaRegion, ...] = (
    LunaRegion("luna.amazon.de", "amazon.de", "A1PA6795UKMFR9", "DE", "de_DE", _TEMPO_EU, True),
    LunaRegion("luna.amazon.com", "amazon.com", "ATVPDKIKX0DER", "US", "en_US", _TEMPO_NA, False),
    LunaRegion("luna.amazon.ca", "amazon.ca", "A2EUQ1WTGCTBG2", "CA", "en_CA", _TEMPO_NA, False),
    LunaRegion("luna.amazon.co.uk", "amazon.co.uk", "A1F83G8C2ARO7P", "GB", "en_GB", _TEMPO_EU, False),
    LunaRegion("luna.amazon.fr", "amazon.fr", "A13V1IB3VIYZZH", "FR", "fr_FR", _TEMPO_EU, False),
    LunaRegion("luna.amazon.it", "amazon.it", "APJ6JRA9NG5V4", "IT", "it_IT", _TEMPO_EU, False),
    LunaRegion("luna.amazon.es", "amazon.es", "A1RKKUPIHCS9HS", "ES", "es_ES", _TEMPO_EU, False),
    LunaRegion("luna.amazon.pl", "amazon.pl", "A1C3SOZRARQ6R3", "PL", "pl_PL", _TEMPO_EU, False),
    LunaRegion("luna.amazon.nl", "amazon.nl", "A1805IZSGTT6HS", "NL", "nl_NL", _TEMPO_EU, False),
    LunaRegion("luna.amazon.se", "amazon.se", "A2NODRKZP88ZB9", "SE", "sv_SE", _TEMPO_EU, False),
    LunaRegion("luna.amazon.com.be", "amazon.com.be", "AMEN7PMS3EDWL", "BE", "nl_NL", _TEMPO_EU, False),
)

_BY_HOST = {region.luna_host: region for region in _REGIONS}

#: Every Luna country host, for "is this page a Luna page" checks.
LUNA_HOSTS: tuple[str, ...] = tuple(region.luna_host for region in _REGIONS)

#: Cookie-name prefixes of an Amazon sign-in (``at-acbde``, ``sess-at-main``,
#: …). Tracking cookies such as ``session-id`` are set before any sign-in and
#: do not count.
_AUTH_COOKIE_PREFIXES = ("at-", "sess-at-")


def resolve_region(luna_host: str) -> LunaRegion | None:
    """The region row for ``luna_host`` (bare host or URL), or ``None``."""
    raw = (luna_host or "").strip()
    host = urlparse(raw).hostname if "://" in raw else raw.split("/")[0]
    return _BY_HOST.get((host or "").lower())


def is_luna_url(url: str) -> bool:
    """Whether ``url`` is on any Luna country site."""
    host = (urlparse(url).hostname or "").lower()
    return host in _BY_HOST


def region_for_cookie_hosts(hosts: set[str]) -> LunaRegion | None:
    """The region whose Amazon domain is among ``hosts`` (bare, lower-case).

    The longest matching cookie domain wins, so a sign-in on ``amazon.com.be``
    is not mistaken for ``amazon.com``. Pure.
    """
    best: LunaRegion | None = None
    for region in _REGIONS:
        suffix = region.cookie_suffix
        if any(h == suffix or h.endswith("." + suffix) for h in hosts) and (
            best is None or len(suffix) > len(best.cookie_suffix)
        ):
            best = region
    return best


def detect_region(profile_dir: str) -> LunaRegion | None:
    """The region whose Amazon domain holds the profile's sign-in, or ``None``.

    Reads cookie names and hosts only, never values. ``None`` means no
    signed-in Amazon session in ``profile_dir`` yet.
    """
    from unifideck.auth.edge_browser.cookie_reader import cookie_hosts

    region = region_for_cookie_hosts(cookie_hosts(profile_dir, _AUTH_COOKIE_PREFIXES))
    if region is not None:
        logger.debug("[LunaRegion] signed in on %s", region.luna_host)
    return region
