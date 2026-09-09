"""6-pass title→game_id search ladder.

Composes :mod:`match` primitives with the SGDB autocomplete API to
resolve a free-form game title to an SGDB game ID, with strict
franchise-confusion guards.

Pass strategy
=============

1. **Cleaned-query exact match** — send :func:`clean_search_query`
   output to ``/search/autocomplete/{q}``, then normalised exact match
   against the returned names.
2. **Edition-stripped match** — strip suffixes ("Deluxe Edition" etc.)
   from both query and candidates before comparing.
3. **Scored match @ 0.85** — Jaccard word-set overlap above the
   franchise-confusion threshold.
4. **Retry with stripped base** — re-query SGDB using the
   edition-stripped title (sometimes the SGDB entry is indexed
   without the suffix, so the autocomplete returned the wrong
   substring match).
5. **Publisher prefix strip** — for each known prefix ("ea sports",
   "tom clancys", …), if the title starts with it, retry without.
6. **Fuzzy fallback @ 0.50** — accept the best-scoring candidate from
   any pass if it clears the lower threshold. Logged at INFO so
   regressions in match quality are visible.

Every pass scores only candidates that survive
:func:`_reject_version_mismatch`, which drops any result whose sequel
number differs from the original title's. Without it the 0.50 fuzzy
floor accepted siblings — all of "The Settlers 2/5/6 - History
Edition" resolved to Settlers 7's artwork.

If all 6 passes fail, returns ``None`` and the caller falls through
to Steam CDN.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from unifideck.utils.title_match import (
    clean_search_query,
    normalize_for_match,
    score_match,
    strip_edition_suffix,
    version_tokens,
)

from .constants import PUBLISHER_PREFIXES

if TYPE_CHECKING:
    import aiohttp

logger = logging.getLogger(__name__)


async def _autocomplete(
    session: aiohttp.ClientSession,
    base: str,
    api_key: str,
    query: str,
    timeout_sec: int,
) -> list[dict[str, Any]]:
    """Single SGDB ``/search/autocomplete/{query}`` call.

    Returns the raw ``data`` list (each item has at least ``id`` +
    ``name``). Empty list on any failure — never raises.
    """
    import aiohttp

    url = f"{base}/search/autocomplete/{query}"
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        async with session.get(
            url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout_sec),
        ) as resp:
            if resp.status != 200:
                # 401/403 (auth/Cloudflare block), 429 (rate limit) and
                # 5xx are systemic and worth surfacing; a stray 404 is
                # routine, so keep it quiet.
                level = (
                    logging.WARNING
                    if resp.status in (401, 403, 429) or resp.status >= 500
                    else logging.DEBUG
                )
                logger.log(
                    level, "[sgdb.search] autocomplete(%r) → HTTP %d",
                    query, resp.status,
                )
                return []
            payload = await resp.json()
    except (TimeoutError, aiohttp.ClientError, OSError, ValueError) as e:
        # Promoted from DEBUG: a network/TLS/DNS failure talking to SGDB
        # makes the whole search return None → no SGDB art for the game.
        # On the Deck this was an unverified-cert SSL error that went
        # silently swallowed for the entire library.
        logger.warning(
            "[sgdb.search] autocomplete(%r) failed: %s: %s",
            query, type(e).__name__, e,
        )
        return []
    if not payload.get("success"):
        return []
    data = payload.get("data") or []
    return data if isinstance(data, list) else []


def _reject_version_mismatch(
    results: list[dict[str, Any]],
    want: frozenset[str],
) -> list[dict[str, Any]]:
    """Drop candidates whose sequel number differs from the query's.

    A hard gate, applied to every result set before any pass scores it.
    The scoring passes cannot do this themselves: "the settlers 5
    history edition" vs "the settlers 7 history edition" is a Jaccard
    0.67 — comfortably above pass 6's 0.50 fuzzy floor — so the fuzzy
    fallback happily returned Settlers 7's art for Settlers 2, 5 and 6.

    ``want`` MUST come from the original title, never from the current
    pass's query string: pass 4 re-queries with the edition-stripped
    base, which no longer carries the number, and anchoring there just
    reintroduces the bug one layer down (it collapsed 2/5/6 onto plain
    "The Settlers" in testing).

    Absence is itself a value — a query with no number keeps only
    candidates with no number, so "The Settlers" stops matching "The
    Settlers 7". Callers prefer no art over another game's art.

    Both sides are measured on the *edition-stripped* base, the same
    way :func:`~unifideck.utils.title_match.titles_match` does it, so a
    tag that merely contains a number is not read as a sequel number.
    Measuring the raw title instead cost real artwork: "Rise of the Tomb
    Raider: 20 Year Celebration" wanted version "20" and "A New Frontier
    - Episode 1" wanted version "1", so both filtered away the very
    entry they had been matching correctly.
    """
    return [
        item for item in results
        if _versions_of(str(item.get("name", ""))) == want
    ]


def _versions_of(title: str) -> frozenset[str]:
    """Version tokens of a title, measured on its edition-stripped base."""
    return version_tokens(strip_edition_suffix(normalize_for_match(title)))


def _without_publisher(norm: str) -> set[str]:
    """Words of a normalised title, minus any leading publisher prefix."""
    for prefix in PUBLISHER_PREFIXES:
        if norm.startswith(prefix + " "):
            return set(norm[len(prefix):].split())
    return set(norm.split())


def _shares_a_distinctive_word(
    forms: list[tuple[str, str]],
    item: dict[str, Any],
) -> bool:
    """Whether a candidate overlaps the query beyond a publisher prefix.

    Guard for the 0.50 fuzzy floor only. "EA SPORTS WRC" and "EA Sports
    UFC" share two of four words, which is enough to clear that floor —
    but both words are the publisher, and the parts naming the actual
    game ("wrc" / "ufc") have nothing in common. Requiring one shared
    non-publisher word keeps the fallback useful for truncated or noisy
    titles while refusing a match built purely on branding.
    """
    cand = _without_publisher(normalize_for_match(str(item.get("name", ""))))
    return any(_without_publisher(norm) & cand for norm, _base in forms)


def _query_forms(title: str) -> list[tuple[str, str]]:
    """Normalised ``(norm, base)`` match forms for a title.

    Two forms: the full normalised title, and the *cleaned-query* form
    (``clean_search_query`` strips DLC / edition / platform noise the
    autocomplete query already drops). Matching against BOTH means a
    candidate that equals the cleaned query — e.g. "Besiege" for the
    shortcut "Besiege + The Splintered Sea DLC" — counts as an exact
    match even though the raw title is far noisier. Without this, the
    autocomplete returned the right game but the scorer rejected it
    (Jaccard 0.2 against the noisy raw title). Deduped, full form first;
    only ever widens the net (never rejects a prior match).
    """
    forms: list[tuple[str, str]] = []
    for source in (title, clean_search_query(title)):
        norm = normalize_for_match(source)
        if not norm:
            continue
        pair = (norm, strip_edition_suffix(norm))
        if pair not in forms:
            forms.append(pair)
    return forms


def _best_exact_or_edition(
    results: list[dict[str, Any]],
    forms: list[tuple[str, str]],
) -> int | None:
    """Passes 1 + 2 combined — exact match then edition-stripped.

    A candidate matches if it equals ANY query form's norm (exact) or
    base (edition-stripped). Splitting exact / edition across two loops
    keeps exact matches strictly preferred over edition ones.
    """
    norms = {n for n, _ in forms}
    bases = {b for _, b in forms}
    for item in results:
        item_norm = normalize_for_match(str(item.get("name", "")))
        if item_norm in norms:
            logger.debug(
                "[sgdb.search] exact match: %r → id=%s",
                item_norm, item.get("id"),
            )
            return _to_id(item.get("id"))
    for item in results:
        item_base = strip_edition_suffix(
            normalize_for_match(str(item.get("name", ""))),
        )
        if item_base in bases:
            logger.debug(
                "[sgdb.search] edition match: %r → id=%s",
                item_base, item.get("id"),
            )
            return _to_id(item.get("id"))
    return None


def _best_scored(
    results: list[dict[str, Any]],
    forms: list[tuple[str, str]],
    threshold: float,
) -> tuple[float, int | None]:
    """Pass 3 / 6 — best Jaccard score across results and query forms.

    Returns ``(best_score, best_id_or_None)``. Caller compares against
    ``threshold`` to decide whether to accept.
    """
    best_score = 0.0
    best_id: int | None = None
    for item in results:
        name = str(item.get("name", ""))
        item_norm = normalize_for_match(name)
        item_base = strip_edition_suffix(item_norm)
        score = max(
            max(score_match(norm, item_norm), score_match(base, item_base))
            for norm, base in forms
        )
        if score > best_score:
            best_score = score
            best_id = _to_id(item.get("id"))
    if best_id is not None and best_score >= threshold:
        return best_score, best_id
    return best_score, None


def _to_id(raw: Any) -> int | None:
    """Coerce SGDB ``id`` (sometimes int, sometimes numeric string)."""
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


async def _pass4_retry_base(
    session: aiohttp.ClientSession,
    base: str,
    api_key: str,
    query_base: str,
    forms: list[tuple[str, str]],
    timeout_sec: int,
    want: frozenset[str],
) -> tuple[int | None, list[dict[str, Any]]]:
    """Pass 4: re-query SGDB with the edition-stripped title.

    Returns ``(matched_id_or_None, retry_results)`` — the second value
    is forwarded to the fuzzy fallback in pass 6 so we don't waste the
    extra round-trip.
    """
    retry = _reject_version_mismatch(
        await _autocomplete(session, base, api_key, query_base, timeout_sec),
        want,
    )
    found = _best_exact_or_edition(retry, forms)
    if found is not None:
        logger.debug(
            "[sgdb.search] retry base match: %r → id=%d",
            query_base, found,
        )
        return found, retry
    score, hit = _best_scored(retry, forms, 0.85)
    if hit is not None:
        logger.debug(
            "[sgdb.search] retry scored: %r → id=%d (score=%.2f)",
            query_base, hit, score,
        )
        return hit, retry
    return None, retry


async def _pass5_publisher_prefix(
    session: aiohttp.ClientSession,
    base: str,
    api_key: str,
    query_base: str,
    timeout_sec: int,
    want: frozenset[str],
) -> int | None:
    """Pass 5: strip a known publisher prefix and re-query.

    Only one prefix is tried per call — they're mutually exclusive at
    the start of a title, and trying every prefix would waste API
    budget on cold matches.
    """
    for prefix in PUBLISHER_PREFIXES:
        if not query_base.startswith(prefix + " "):
            continue
        short = query_base[len(prefix):].strip()
        if not short:
            return None
        prefix_results = _reject_version_mismatch(
            await _autocomplete(session, base, api_key, short, timeout_sec),
            want,
        )
        for item in prefix_results:
            name = str(item.get("name", ""))
            item_norm = normalize_for_match(name)
            item_base = strip_edition_suffix(item_norm)
            if item_base in (short, query_base):
                coerced = _to_id(item.get("id"))
                if coerced is not None:
                    logger.debug(
                        "[sgdb.search] prefix-strip match: %r → id=%d",
                        short, coerced,
                    )
                    return coerced
        return None
    return None


def _pass6_fuzzy_fallback(
    results: list[dict[str, Any]],
    forms: list[tuple[str, str]],
    title: str,
) -> int | None:
    """Pass 6: fuzzy fallback @ 0.50 across the last result set we have.

    Candidates that overlap the query ONLY through a shared publisher
    prefix are dropped first — "EA SPORTS WRC" vs "EA Sports UFC" is a
    Jaccard 0.5 on the strength of "ea sports" alone, which cleared the
    floor and put UFC artwork on a rally game.
    """
    score, hit = _best_scored(
        [i for i in results if _shares_a_distinctive_word(forms, i)],
        forms, 0.50,
    )
    if hit is not None:
        logger.info(
            "[sgdb.search] fuzzy match: %r → id=%d (score=%.2f)",
            title, hit, score,
        )
        return hit
    logger.debug(
        "[sgdb.search] no match for %r (best score=%.2f)", title, score,
    )
    return None


async def search_game_id(
    session: aiohttp.ClientSession,
    base: str,
    api_key: str,
    title: str,
    *,
    timeout_sec: int,
) -> int | None:
    """6-pass SGDB game-id resolution. Returns ``None`` on miss.

    Logs each pass at DEBUG so ``[sgdb.search]`` greps in the Decky
    log show the full match trail when debugging artwork misses.
    """
    if not title:
        return None
    cleaned = clean_search_query(title)
    if not cleaned:
        return None
    # Match forms: the raw title AND the cleaned query (see _query_forms).
    forms = _query_forms(title)
    # query_base drives the pass-4 re-query string (edition-stripped).
    query_base = forms[0][1]
    # Sequel discriminator from the ORIGINAL title — see
    # _reject_version_mismatch for why it must not come from a pass's
    # own (already stripped) query string. forms[0][1] is that title's
    # edition-stripped base, which is what keeps an edition tag that
    # contains a number ("20 Year Celebration") from reading as one.
    want = version_tokens(forms[0][1])

    # Pass 1+2: cleaned-query autocomplete → exact + edition match
    results = _reject_version_mismatch(
        await _autocomplete(session, base, api_key, cleaned, timeout_sec),
        want,
    )
    found = _best_exact_or_edition(results, forms)
    if found is not None:
        return found

    # Pass 3: scored match @ 0.85
    score3, id3 = _best_scored(results, forms, 0.85)
    if id3 is not None:
        logger.debug(
            "[sgdb.search] scored match: %r → id=%d (score=%.2f)",
            title, id3, score3,
        )
        return id3

    # Pass 4: retry with edition-stripped query
    if query_base and query_base != cleaned.lower():
        hit, retry = await _pass4_retry_base(
            session, base, api_key, query_base, forms, timeout_sec, want,
        )
        if hit is not None:
            return hit
        if retry:
            results = retry  # Carry forward for fuzzy fallback

    # Pass 5: publisher-prefix strip
    prefix_hit = await _pass5_publisher_prefix(
        session, base, api_key, query_base, timeout_sec, want,
    )
    if prefix_hit is not None:
        return prefix_hit

    return _pass6_fuzzy_fallback(results, forms, title)
