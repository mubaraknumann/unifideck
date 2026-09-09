"""Epic Selective Downloads (SDL) — resolve legendary install tags.

legendary supports *selective downloads* for a set of titles: the
manifest tags optional files (language/voice packs, HD textures,
Fortnite's Save the World) and the CLI asks which of them to include.
That question is a bare ``input()`` inside
``legendary/utils/cli.py::sdl_prompt`` and — unlike every other prompt
in the install path — ``--yes`` does **not** suppress it. Spawned from
the Decky backend with no usable stdin it raises ``EOFError``, which
aborted the install outright for every SDL title (UD-026: Fallout 3
GOTY and Fallout: New Vegas, Hogwarts Legacy, Cyberpunk 2077, …).

So we answer the question ourselves, up front, and pass explicit
``--install-tag`` values instead:

* ``""`` — the base game's untagged files, always;
* every ``__required`` tag the config declares;
* every **non-language** extra (``hd_textures``, Fortnite's ``stw``) —
  content a user would want, and the only two such tags in the whole
  catalog;
* exactly **one** language pack, matched to the user's language, so a
  Deck doesn't swallow nine voice packs it will never play.

Which titles are SDL titles is **not** the hardcoded table in
legendary's ``utils/selective_dl.py`` — that is only a fallback.
``LegendaryCore`` injects every app_name from the remote
``game_overrides.sdl_config`` into its table, so the authoritative
list is legendary's own cached ``version.json`` (rewritten on any
legendary run). Reading that file is also why this needs no extra
subprocess. Mistaking the fallback table for the real list is what
made UD-026 rule SDL out and misdiagnose the bug for two releases.

Every lookup degrades quietly: a non-SDL title, a cache miss with no
network, or a malformed payload all yield "no tags", and the caller
falls back to ``--skip-sdl`` (required data only) rather than failing
the install.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from unifideck.utils.lang_normalize import normalize_language, smart_match_language

from .legendary import legendary_config_dir

logger = logging.getLogger(__name__)

_SDL_API = "https://api.legendary.gl/v1/sdl/{app_name}.json"
# api.legendary.gl answers the default urllib/aiohttp User-Agent with a
# blanket 403; any explicit UA is accepted.
_USER_AGENT = "unifideck/1.0 (legendary SDL config lookup)"
_CACHE_DIR = "~/.local/share/unifideck/cache/epic_sdl"
# legendary's own key for the tags a title cannot be installed without.
_REQUIRED_KEY = "__required"
# The install tag standing for "every file with no install tag" — i.e.
# the base game. Must be passed explicitly, or legendary would install
# only the tagged optional packs.
_BASE_TAG = ""
_FALLBACK_LANG = "en-US"
# legendary's SDL configs list only the *additional* languages a title can
# download; the base game's own language lives in the untagged files and is
# therefore absent from the option list. Every title in the catalog ships an
# English base (the ones that don't offer English as a pack, at least), so we
# insert a stand-in for it. Without one the picker showed no English at all
# and pre-selected whatever happened to be listed first — an English user who
# pressed Install got a German voice pack. Selecting it adds no tag, which is
# exactly right: the base files already are English.
_BASE_LANGUAGE_KEY = "en"
_BASE_LANGUAGE_NAME = "English"

def _cache_path(app_name: str) -> Path:
    """Return the on-disk cache path for one app's SDL config."""
    safe = "".join(c for c in app_name if c.isalnum() or c in "-_")
    return Path(_CACHE_DIR).expanduser() / f"{safe}.json"

def read_cached_sdl_data(app_name: str) -> dict[str, Any] | None:
    """Return this app's cached SDL config, or None."""
    try:
        with _cache_path(app_name).open() as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) and data else None

def _write_cache(app_name: str, data: dict[str, Any]) -> None:
    """Persist one app's SDL config; a failed write is not fatal."""
    path = _cache_path(app_name)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as f:
            json.dump(data, f)
    except OSError as e:
        logger.debug("[epic_sdl] cache write failed for %s: %s", app_name, e)

def sdl_app_names() -> set[str]:
    """Return the app_names legendary treats as SDL titles.

    Read from legendary's cached ``version.json``
    (``game_overrides.sdl_config``) — the same source
    ``LegendaryCore`` merges into its hardcoded table.
    """
    path = legendary_config_dir() / "version.json"
    try:
        with path.open() as f:
            blob = json.load(f)
    except (OSError, json.JSONDecodeError):
        return set()
    if not isinstance(blob, dict):
        return set()
    # The file wraps the payload in {"data": …, "last_update": …}, but
    # tolerate an unwrapped payload too.
    data = blob.get("data") if isinstance(blob.get("data"), dict) else blob
    overrides = data.get("game_overrides") if isinstance(data, dict) else None
    config = overrides.get("sdl_config") if isinstance(overrides, dict) else None
    return set(config.keys()) if isinstance(config, dict) else set()

def _sdl_key_for(app_name: str) -> str | None:
    """Return the SDL config key covering ``app_name``, if any.

    legendary matches by prefix (``get_sdl_appname``) so that e.g.
    ``Fortnite`` covers its regional variants. Longest match wins, so a
    more specific entry beats a shorter prefix.
    """
    if not app_name:
        return None
    matches = [
        key for key in sdl_app_names()
        if not key.endswith("_Mac") and app_name.startswith(key)
    ]
    return max(matches, key=len) if matches else None

async def fetch_sdl_data(
    app_name: str, *, timeout: float = 10.0,  # noqa: ASYNC109 — timeout forwarded to aiohttp ClientTimeout, not an asyncio.timeout context
) -> dict[str, Any] | None:
    """Return the SDL config for ``app_name``, or None if it has none.

    None means "not an SDL title, or unresolvable" — both of which the
    caller handles the same way (``--skip-sdl``). A cached copy is
    served whenever the network fails, so a title stays installable
    offline once it has been resolved once.
    """
    key = _sdl_key_for(app_name)
    if key is None:
        # Logged because its absence reads as a bug: a user who saw the
        # language picker on Fallout: New Vegas (an SDL title) reports
        # "no language options for GTA V / RDR2 / BioShock" as broken.
        # Only ~6 titles in a 700-game Epic library are SDL titles; every
        # other one is a single package with nothing to choose, and its
        # language is applied at launch instead (``-epiclocale``, see
        # launcher/proton/handlers/epic._resolve_epic_language). One INFO
        # line makes that answerable from a support bundle.
        logger.info(
            "[epic_sdl] %s is not a Selective Downloads title — "
            "single-package install, language applied at launch", app_name,
        )
        return None
    fetched = await _get_sdl_json(key, timeout=timeout)
    if fetched is not None:
        _write_cache(key, fetched)
        return fetched
    cached = read_cached_sdl_data(key)
    if cached is not None:
        logger.info("[epic_sdl] serving cached SDL config for %s", key)
    else:
        logger.warning(
            "[epic_sdl] no SDL config for %s (network + cache both missed); "
            "install will fall back to required data only", key,
        )
    return cached

async def _get_sdl_json(
    key: str, *, timeout: float,  # noqa: ASYNC109 — timeout forwarded to aiohttp ClientTimeout, not an asyncio.timeout context
) -> dict[str, Any] | None:
    """GET one SDL config from legendary's API, or None on any failure."""
    import aiohttp
    url = _SDL_API.format(app_name=key)
    try:
        # ssl=False — SteamOS's bundled cert store is outdated and
        # default verification fails inside the Decky plugin process
        # for several third-party hosts (see library.search_store).
        connector = aiohttp.TCPConnector(ssl=False)
        async with (
            aiohttp.ClientSession(
                connector=connector,
                headers={"User-Agent": _USER_AGENT},
            ) as session,
            session.get(
                url, timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp,
        ):
            if resp.status != 200:
                logger.warning(
                    "[epic_sdl] %s returned HTTP %s", url, resp.status,
                )
                return None
            data = await resp.json(content_type=None)
    except (TimeoutError, asyncio.CancelledError):
        raise
    except Exception as e:
        logger.warning("[epic_sdl] SDL fetch failed for %s: %r", key, e)
        return None
    return data if isinstance(data, dict) and data else None

def _strip_leading_parenthetical(name: str) -> str:
    """Drop a leading ``(...)`` qualifier from an SDL option name.

    legendary labels packs ``"(Language Pack) čeština"`` /
    ``"(Additional Languages) Deutsch"``; the language word is what
    identifies them, so the qualifier has to come off before matching.
    """
    text = name.strip()
    if text.startswith("(") and ")" in text:
        return text[text.index(")") + 1 :].strip()
    return text

def _language_of(key: str, name: str) -> str | None:
    """Return the ISO base code an SDL option denotes, else None.

    None marks a *non-language* extra (``hd_textures``, ``stw``) — the
    packs we always take. Both the option key and its display name are
    tried, because legendary is inconsistent: the key is usually a
    language code, but ``cr`` (its typo for Czech) only resolves from
    the name.
    """
    for candidate in (key, name, _strip_leading_parenthetical(name)):
        if candidate and (hit := normalize_language(candidate)):
            return hit
    return None

def _options(sdl_data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return the selectable options, dropping the ``__required`` block."""
    return {
        key: value for key, value in sdl_data.items()
        if key != _REQUIRED_KEY and isinstance(value, dict)
    }

def _tags_of(option: dict[str, Any]) -> list[str]:
    """Return one option's install tags."""
    tags = option.get("tags")
    return [t for t in tags if isinstance(t, str)] if isinstance(tags, list) else []

def _display_name(key: str, option: dict[str, Any]) -> str:
    """Return an option's display name, falling back to its key."""
    name = option.get("name")
    return name if isinstance(name, str) and name else key

def language_options(sdl_data: dict[str, Any]) -> dict[str, str]:
    """Return ``{option_key: display_name}`` for the language packs only.

    This is what the install-time picker offers. Non-language extras
    are excluded because they are taken unconditionally — there is
    nothing for the user to decide about them.

    The base game's own language is prepended as a synthetic English
    entry (see :data:`_BASE_LANGUAGE_KEY`) whenever the title offers no
    English pack of its own, so the picker can show — and default to —
    the language the user actually gets when no pack is downloaded.
    """
    packs: dict[str, str] = {}
    for key, option in _options(sdl_data).items():
        name = _display_name(key, option)
        if _language_of(key, name) is not None:
            packs[key] = name
    if not packs:
        # Extras-only title (e.g. HD textures): nothing to choose.
        return {}
    if any(_language_of(k, v) == _BASE_LANGUAGE_KEY for k, v in packs.items()):
        return packs
    return {_BASE_LANGUAGE_KEY: _BASE_LANGUAGE_NAME, **packs}

def _pick_language_key(
    sdl_data: dict[str, Any], requested: str | None,
) -> str | None:
    """Choose which language option to install, or None for none.

    Matching widens in four steps, then repeats the lot for English:

    1. an exact option-key hit — the user's own pick from the modal,
       which lists the title's own keys, so it is taken verbatim and
       never re-matched into a different variant (the rule GOG's
       ``_pick_explicit_lang`` follows);
    2. an exact hit on the key with ``_`` normalized to ``-``, so a
       request for ``es-MX`` lands on ``es_mx`` and not on ``es_es``;
    3. ``smart_match_language`` over those normalized keys;
    4. base-language equality against the code each option *resolves*
       to — the only step that catches legendary's odd keys, where the
       language is identifiable solely from the display name
       (``lang_de`` → "(Language Pack) Deutsch", ``cr`` → "čeština").
    """
    options = language_options(sdl_data)
    if not options:
        return None
    if requested and requested in options:
        return requested
    by_key = {key.replace("_", "-").lower(): key for key in options}
    codes = {
        key: _language_of(key, name) for key, name in options.items()
    }
    for target in (requested, _FALLBACK_LANG):
        if not target:
            continue
        if hit := _match_one(target, by_key, codes):
            return hit
    return None

def _match_one(
    target: str, by_key: dict[str, str], codes: dict[str, str | None],
) -> str | None:
    """Resolve one language request against a title's options."""
    if exact := by_key.get(target.lower()):
        return exact
    if key_hit := smart_match_language(target, list(by_key.keys())):
        return by_key[key_hit]
    wanted = normalize_language(target)
    if wanted:
        for key, code in codes.items():
            if code == wanted:
                return key
    return None

def select_install_tags(
    sdl_data: dict[str, Any], requested_lang: str | None,
) -> list[str]:
    """Return the ``--install-tag`` values for one install.

    Order is stable and duplicates are dropped, so the resulting
    command line is deterministic (and therefore testable).
    """
    tags: list[str] = [_BASE_TAG]
    required = sdl_data.get(_REQUIRED_KEY)
    if isinstance(required, dict):
        tags.extend(_tags_of(required))
    options = _options(sdl_data)
    # Every non-language extra: content the user wants, and cheap —
    # `hd_textures` and Fortnite's `stw` are the only two in existence.
    for key, option in options.items():
        if _language_of(key, _display_name(key, option)) is None:
            tags.extend(_tags_of(option))
    chosen = _pick_language_key(sdl_data, requested_lang)
    if chosen is not None:
        # ``.get`` because the base-language entry is synthetic: it has no
        # row in the SDL config, and contributing no tag is precisely what
        # "install the base game's own language" means.
        tags.extend(_tags_of(options.get(chosen, {})))
        logger.info(
            "[epic_sdl] language %r selected for requested=%r (tags=%s)",
            chosen, requested_lang, _tags_of(options.get(chosen, {})) or "base only",
        )
    return list(dict.fromkeys(tags))

async def resolve_install_tags(
    app_name: str, requested_lang: str | None, *, timeout: float = 10.0,  # noqa: ASYNC109 — timeout forwarded to aiohttp ClientTimeout, not an asyncio.timeout context
) -> list[str]:
    """Return install tags for ``app_name``, or ``[]`` if it has no SDL.

    ``[]`` tells the caller to pass ``--skip-sdl`` alone.
    """
    sdl_data = await fetch_sdl_data(app_name, timeout=timeout)
    if not sdl_data:
        return []
    return select_install_tags(sdl_data, requested_lang)

async def resolve_language_options(
    app_name: str, *, timeout: float = 10.0,  # noqa: ASYNC109 — timeout forwarded to aiohttp ClientTimeout, not an asyncio.timeout context
) -> dict[str, str]:
    """Return ``{tag: display_name}`` language choices for a title.

    Empty for a non-SDL title or a single-language one — the frontend
    then skips the picker entirely.
    """
    sdl_data = await fetch_sdl_data(app_name, timeout=timeout)
    return language_options(sdl_data) if sdl_data else {}
