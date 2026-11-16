"""Which language Luna should use, derived from the unifideck language.

Luna offers a fixed set of languages on every country site (its own
Settings → Language page). The web client keeps the choice in
``localStorage["locale_override"]`` (``de_DE`` style) and the catalog API
takes the same tag in ``x-amz-locale``; both were verified on a Steam Deck.

:func:`luna_locale_for` maps the unifideck UI language onto that set: an
exact match first, then the same language (preferring the country site's own
variant), else the country site's default.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from unifideck.config import ConfigManager
    from unifideck.stores.luna.luna_api.regions import LunaRegion

#: The languages Luna's language page lists, in Luna's own order.
LUNA_LANGUAGES: tuple[str, ...] = (
    "en_US", "en_CA", "en_GB", "de_DE", "fr_FR", "fr_CA", "it_IT",
    "es_ES", "es_US", "nl_NL", "pl_PL", "pt_PT", "sv_SE",
)

#: Luna's own localStorage key for the chosen language.
LUNA_LOCALE_STORAGE_KEY = "locale_override"


def _normalize(tag: str | None) -> str:
    """``de-DE`` / ``de_de`` / ``de`` → ``de_DE`` / ``de_DE`` / ``de``."""
    parts = (tag or "").replace("-", "_").split("_")
    lang = parts[0].strip().lower()
    if len(parts) > 1 and parts[1].strip():
        return f"{lang}_{parts[1].strip().upper()}"
    return lang


def luna_locale_for(ui_locale: str | None, region: LunaRegion | None) -> str:
    """The Luna language for ``ui_locale`` on ``region``'s site. Pure."""
    default = region.locale if region else "en_US"
    tag = _normalize(ui_locale)
    if tag in LUNA_LANGUAGES:
        return tag
    lang = tag.split("_")[0]
    if not lang:
        return default
    if default.split("_")[0] == lang:
        return default
    if region and f"{lang}_{region.country}" in LUNA_LANGUAGES:
        return f"{lang}_{region.country}"
    for candidate in LUNA_LANGUAGES:
        if candidate.split("_")[0] == lang:
            return candidate
    return default


def luna_locale_from_config(
    config: ConfigManager | None, region: LunaRegion | None,
) -> str:
    """The Luna language for the current unifideck UI language."""
    from unifideck.utils.locale import get_unifideck_locale

    try:
        ui_locale = get_unifideck_locale(config)
    except Exception:  # locale lookup must never break Luna
        ui_locale = None
    return luna_locale_for(ui_locale, region)
