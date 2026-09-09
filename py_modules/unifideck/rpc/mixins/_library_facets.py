"""rpc/mixins/_library_facets.py — assemble per-shortcut library facets.

Reshapes the metadata / compat caches the sync already populates
into one ``FacetRecord`` per Unifideck shortcut. The frontend uses
the result to:

* drive Steam's **native** library Sort menu + Library Filters by
  enriching the live ``AppOverview`` (metacritic, per-device compat
  categories, store categories/tags, release date, reviews,
  date-added), and
* resolve the compatibility tab by shortcut AppID with zero title
  matching (``compat_status`` / ``protondb_tier``).

Compat is emitted twice on purpose: ``compat_category``/
``compat_status`` are already resolved for *this* device, and
``compat_categories`` carries every track's raw int for the packed
bitfield Steam's own filters read.

Pure cache reads — this module never issues a network fetch. The
two genuinely-new sources (Steam reviews, first-seen timestamp)
are read from their own cache namespaces, which the sync phase
populates; both degrade to ``None`` / ``0`` when cold.

Underscore-prefixed: internal to ``library_facets.LibraryFacetsRPCMixin``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from unifideck.core.compat_bridge import appid_candidates
from unifideck.rpc.mixins._compat_payload import (
    active_track,
    compat_categories,
    compat_category,
    compat_status,
)
from unifideck.rpc.mixins._metadata_display import (
    read_cache_store,
    read_compat_entry,
    read_steam_metadata,
)

if TYPE_CHECKING:
    from unifideck.core.types import Game

# Cache namespaces the sync phase fills with the two facets that are
# NOT already derivable from steam_metadata / compat.
STEAM_REVIEWS_NS = (
    "steam_reviews"  # {str(real_steam_appid): {review_score, review_percentage, total_reviews}}
)
DATE_ADDED_NS = "shortcut_added"  # {str(shortcut_app_id): int unix seconds}


def _extract_ids(raw: Any) -> list[int]:
    """Pull integer ``id``s from a Steam appdetails ``categories`` /
    ``genres`` list (``[{"id": int, "description": str}, ...]``)."""
    if not isinstance(raw, list):
        return []
    out: list[int] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        cid = item.get("id")
        if isinstance(cid, int):
            out.append(cid)
        elif isinstance(cid, str) and cid.isdigit():
            out.append(int(cid))
    return out


def _metacritic(
    steam_meta: dict[str, Any],
    meta_entry: dict[str, Any] | None,
) -> int | None:
    """Metacritic critic score.

    Steam's own ``appdetails`` only carries a score for titles Metacritic
    rated *and* Steam surfaces — many games (esp. non-AAA) miss it there.
    The sync's metacritic backfill (``metadata_backfill`` →
    ``metacritic.com``) fills the gap into the composite ``metadata``
    cache keyed by ``store:game_id``. We read that entry directly (the
    backfill's native key), which avoids the fragile ``steam_appid``
    join that stranded backfilled scores whose entry lacked a resolved
    ``steam_appid``.
    """
    mc = steam_meta.get("metacritic")
    if isinstance(mc, dict):
        score = mc.get("score")
        if isinstance(score, int):
            return score
    if isinstance(meta_entry, dict):
        score = meta_entry.get("metacritic_score")
        if isinstance(score, int) and score > 0:
            return score
    return None


def _release_date_str(steam_meta: dict[str, Any]) -> str:
    """Raw Steam release-date string (e.g. ``"12 Sep, 2023"``).

    Returned verbatim; the frontend converts to the unix timestamp
    Steam's overview expects, reusing the same ``new Date(...)`` path
    ``buildOverview`` already used — keeps date parsing in one place.
    """
    rd = steam_meta.get("release_date")
    if isinstance(rd, dict):
        date = rd.get("date")
        if isinstance(date, str):
            return date
    return ""


def _recommendations_total(steam_meta: dict[str, Any]) -> int | None:
    rec = steam_meta.get("recommendations")
    if isinstance(rec, dict):
        total = rec.get("total")
        if isinstance(total, int):
            return total
    return None


def _compat_fields(compat: dict[str, Any]) -> dict[str, Any]:
    """This shortcut's rating for the active device, plus every track.

    ``compat_category``/``compat_status`` are resolved for the device
    actually running, so the tab filter and our badges need no device
    branch of their own. ``compat_categories`` carries all four raw
    ints because Steam's *own* library filters read them out of the
    packed bitfield, and on a Steam Machine that is a different pair of
    bits than the Deck's.
    """
    track = active_track()
    return {
        "compat_category": compat_category(compat, track),
        "compat_status": compat_status(compat, track),
        "compat_categories": compat_categories(compat),
    }


def _read_int(data: dict[str, Any], shortcut_app_id: int) -> int:
    """Read an int from a per-shortcut namespace, trying both appid forms."""
    for key in appid_candidates(shortcut_app_id):
        raw = data.get(key)
        if raw is None:
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 0
    return 0


def build_facet_record(
    cache: Any,
    shortcut_app_id: int,
    steam_app_id: int,
    reviews_data: dict[str, Any],
    added_data: dict[str, Any],
    meta_entry: dict[str, Any] | None,
) -> dict[str, Any]:
    """Assemble one shortcut's facet record from the warm caches.

    All inputs are plain dicts already read once by the caller so this
    stays O(1) per shortcut (no per-call store lookups). ``meta_entry``
    is the composite ``metadata[store:game_id]`` record for this
    shortcut (or ``None``) — the metacritic source the backfill writes.
    """
    steam_meta = read_steam_metadata(cache, steam_app_id)
    compat = read_compat_entry(cache, shortcut_app_id, steam_app_id)

    review = reviews_data.get(str(steam_app_id)) if steam_app_id else None
    review = review if isinstance(review, dict) else {}

    return {
        "steam_app_id": steam_app_id,
        # Sort dimensions
        "metacritic": _metacritic(steam_meta, meta_entry),
        "release_date": _release_date_str(steam_meta),
        "recommendations_total": _recommendations_total(steam_meta),
        "review_score": review.get("review_score"),
        "review_percentage": review.get("review_percentage"),
        "date_added_unix": _read_int(added_data, shortcut_app_id),
        # Filter dimensions
        **_compat_fields(compat),
        "store_category": _extract_ids(steam_meta.get("categories")),
        "store_tag": _extract_ids(steam_meta.get("genres")),
        # Shortcut-keyed compat — no title matching
        "protondb_tier": compat.get("protondb_tier"),
    }


def build_enrichment_map(
    cache: Any,
    games: list[Game] | None = None,
) -> dict[str, dict[str, Any]]:
    """Build ``{shortcut_app_id: FacetRecord}`` for every shortcut.

    Preferred path (``games`` provided): iterate the unified library
    list — each game carries ``app_id`` + ``store`` + ``store_game_id``.
    This (a) gives a record to **every** shortcut, including ones whose
    real Steam AppID never resolved, and (b) reads metacritic from the
    composite ``metadata[store:game_id]`` entry (the backfill's native
    key) instead of the fragile ``steam_appid`` join that stranded
    backfilled scores whose entry lacked a resolved ``steam_appid``.

    Fallback (``games is None`` — sync service unavailable): enumerate
    the ``steam_real_appid`` cache; metacritic then comes only from
    Steam's own appdetails (no composite recovery).

    Emits **both** the signed and unsigned 32-bit string forms of each
    shortcut AppID so the frontend (unsigned via ``overview.appid``) and
    the sync layer (signed) can both look up.
    """
    real_appid_data = read_cache_store(cache, "steam_real_appid")
    reviews_data = read_cache_store(cache, STEAM_REVIEWS_NS)
    added_data = read_cache_store(cache, DATE_ADDED_NS)
    metadata_data = read_cache_store(cache, "metadata")

    out: dict[str, dict[str, Any]] = {}

    if games:
        for game in games:
            _add_game_facet(
                out, cache, game,
                real_appid_data, reviews_data, added_data, metadata_data,
            )
        return out

    # Fallback: no games list — enumerate the resolved-appid cache.
    for raw_key, raw_real in real_appid_data.items():
        _add_cached_facet(
            out, cache, raw_key, raw_real, reviews_data, added_data,
        )
    return out


def _add_game_facet(
    out: dict[str, dict[str, Any]],
    cache: Any,
    game: Game,
    real_appid_data: dict[str, Any],
    reviews_data: dict[str, Any],
    added_data: dict[str, Any],
    metadata_data: dict[str, Any],
) -> None:
    """Emit the facet record for one unified-library game (preferred path).

    Reads metacritic from the composite ``metadata[store:game_id]`` entry and
    writes the record under both signed/unsigned AppID forms. No-ops when the
    game has no usable shortcut AppID.
    """
    try:
        shortcut_app_id = int(game.app_id)
    except (TypeError, ValueError):
        return
    if not shortcut_app_id:
        return
    steam_app_id = _read_int(real_appid_data, shortcut_app_id)
    entry = metadata_data.get(f"{game.store}:{game.store_game_id}")
    record = build_facet_record(
        cache,
        shortcut_app_id,
        steam_app_id,
        reviews_data,
        added_data,
        entry if isinstance(entry, dict) else None,
    )
    for key in appid_candidates(shortcut_app_id):
        out[key] = record


def _add_cached_facet(
    out: dict[str, dict[str, Any]],
    cache: Any,
    raw_key: Any,
    raw_real: Any,
    reviews_data: dict[str, Any],
    added_data: dict[str, Any],
) -> None:
    """Emit the facet record for one ``steam_real_appid`` cache entry
    (fallback path); metacritic comes only from Steam's appdetails."""
    try:
        shortcut_app_id = int(raw_key)
        steam_app_id = int(raw_real)
    except (TypeError, ValueError):
        return
    record = build_facet_record(
        cache,
        shortcut_app_id,
        steam_app_id,
        reviews_data,
        added_data,
        None,
    )
    for key in appid_candidates(shortcut_app_id):
        out[key] = record
