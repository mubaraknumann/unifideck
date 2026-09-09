"""SGDB artwork fetcher — pure functions for the network layer.

Pure async functions: no ``self``, each takes its inputs
explicitly so HTTP and filesystem mechanics stay testable
independent of the service orchestrator.

Steam grid/ layout accepts both JPG and PNG for grid, hero
and icon, but the on-disk extension MUST match the actual
byte content — Steam's CEF readers fail silently when a PNG
payload is saved with a ``.jpg`` name (and vice versa).
``logo`` is the strict exception: Steam requires PNG for the
overlay because it relies on alpha transparency.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from unifideck.core.compat_bridge import to_unsigned

if TYPE_CHECKING:
    from unifideck.config import ConfigManager
logger = logging.getLogger(__name__)

# Steam-preferred suffix per artwork kind. Used as the
# fallback when the URL gives no format hint, and as the
# "must-have" anchor for logos (where PNG is mandatory).
_KIND_SUFFIX = {
    "grid": "p.jpg",        # portrait box art (600x900)
    "grid_l": ".jpg",       # landscape header (920x430)
    "hero": "_hero.jpg",    # widescreen banner
    "logo": "_logo.png",    # transparent logo
    "icon": "_icon.jpg",    # small square icon
}

# Kinds for which Steam Deck accepts both .jpg and .png.
# Logo is excluded: Steam needs PNG (alpha overlay).
_FORMAT_FLEXIBLE_KINDS = frozenset({"grid", "grid_l", "hero", "icon"})

# The five canonical Steam-grid artwork kinds, in display order.
_ALL_KINDS = ("grid", "grid_l", "hero", "logo", "icon")


def _candidate_names(kind: str, unsigned: int) -> tuple[str, ...]:
    """On-disk filename candidates for ``kind`` under the unsigned appid.

    Both extensions are listed for every flexible kind because
    ``download_and_save`` picks the extension from the served MIME type,
    so a previous sync may have saved either flavour. Logo is PNG-first
    (Steam mandates PNG) but we also accept a stray ``.jpg`` so a
    store-provided logo isn't re-fetched forever. Mirrors staging's
    ``get_missing_artwork_types`` glob patterns.
    """
    if kind == "grid":
        return (f"{unsigned}p.jpg", f"{unsigned}p.png")
    if kind == "grid_l":
        return (f"{unsigned}.jpg", f"{unsigned}.png")
    if kind == "hero":
        return (f"{unsigned}_hero.jpg", f"{unsigned}_hero.png")
    if kind == "logo":
        return (f"{unsigned}_logo.png", f"{unsigned}_logo.jpg")
    if kind == "icon":
        return (f"{unsigned}_icon.jpg", f"{unsigned}_icon.png")
    return ()


async def get_missing_kinds(grid_dir: str, app_id: int) -> set[str]:
    """Return the set of the five artwork kinds NOT present on disk.

    Checks each kind individually (both ``.jpg`` and ``.png`` variants)
    rather than the coarse grid+hero gate :func:`has_artwork` used to
    apply. This is what lets a sync *backfill* the kinds a previous sync
    missed (logo / icon / landscape) instead of treating a game as
    "done" the moment grid+hero land — the regression that left the
    library with almost no icons and many partial covers.

    Args:
        grid_dir: absolute path to Steam's ``grid/`` directory.
        app_id: shortcut appid (signed or unsigned — normalised here).

    Returns:
        Subset of ``{"grid", "grid_l", "hero", "logo", "icon"}`` whose
        files are absent. Empty set means the game is fully covered.
        On an unreadable directory every kind reads as missing
        (``aio.is_file`` returns False on OSError), so the next sync
        retries — fail-open, never fail-silent.
    """
    from unifideck.core.io import async_file_ops as aio

    # Steam stores shortcut art under the *unsigned* 32-bit appid.
    unsigned = to_unsigned(app_id)
    grid_path = Path(grid_dir)
    missing: set[str] = set()
    for kind in _ALL_KINDS:
        present = False
        for name in _candidate_names(kind, unsigned):
            if await aio.is_file(str(grid_path / name)):
                present = True
                break
        if not present:
            missing.add(kind)
    return missing


def _url_extension(url: str) -> str:
    """Extract the lowercase file extension from a URL's path component.

    Pipeline:

    1. ``urlparse`` splits the URL into scheme/host/path/
       query/fragment — we only care about ``path``;
    2. ``Path(path).suffix`` returns the trailing extension
       *with* the leading dot (e.g. ``".PNG"``);
    3. ``.lower()`` normalises case so ``.PNG`` and
       ``.png`` are treated identically;
    4. ``.lstrip(".")`` drops the dot for clean
       equality checks downstream (``ext == "png"``).

    Query strings + fragments are silently ignored, which
    matters for signed CDN URLs like
    ``https://cdn.steamgriddb.com/grid.png?token=abc&v=2``
    where a naive ``url.endswith(".png")`` would return
    ``False`` and break the format-aware suffix logic.

    Args:
        url: full URL string (any scheme).

    Returns:
        Lowercase extension without the dot, e.g. ``"png"``,
        ``"jpg"``, ``"jpeg"``. Empty string when the URL
        path has no extension at all.
    """
    path = urlparse(url).path
    return Path(path).suffix.lower().lstrip(".")


def _suffix_for(kind: str, url: str) -> str:
    """Resolve the on-disk filename suffix matching an artwork download.

    Pipeline:

    1. ``kind == "logo"`` short-circuits to
       ``_logo.png`` — Steam mandates PNG for logos
       because the library renderer composites them over
       the hero with alpha blending, and a JPG would render
       as a solid white rectangle;
    2. Look up the Steam-preferred suffix from
       ``_KIND_SUFFIX``; unknown kind → ``.jpg`` (safe
       defensive default);
    3. If the kind is in ``_FORMAT_FLEXIBLE_KINDS``
       (grid/hero/icon) AND the URL extension is ``png``,
       swap the trailing ``.jpg`` for ``.png`` so the
       saved filename's extension matches the actual byte
       content. Otherwise keep the JPG default.

    The ``base.replace(".jpg", ".png")`` step is safe
    because every flexible-kind entry in ``_KIND_SUFFIX``
    ends in ``.jpg`` — there's no other ``.jpg``
    substring that could be accidentally matched.

    This function is the regression guard for the silent-
    skip bug: Steam's CEF artwork reader rejects files
    whose extension doesn't match their MIME signature,
    with no error logged anywhere — covers and heroes
    just don't render.

    Args:
        kind: artwork kind (``"grid"``, ``"hero"``,
            ``"logo"``, ``"icon"``, or arbitrary unknown).
        url: download URL — only inspected for its
            extension; never fetched here.

    Returns:
        On-disk suffix including the leading character
        (``p`` for grid, ``_hero`` for hero, etc) and
        the format-correct extension. Always a
        non-empty string.
    """
    if kind == "logo":
        return _KIND_SUFFIX["logo"]
    base = _KIND_SUFFIX.get(kind, ".jpg")
    if kind in _FORMAT_FLEXIBLE_KINDS and _url_extension(url) == "png":
        return base.replace(".jpg", ".png")
    return base


async def has_artwork(grid_dir: str, app_id: int) -> bool:
    """Predicate: is the *complete* artwork set already present?

    Thin wrapper over :func:`get_missing_kinds` — True iff none of the
    five kinds is missing. This replaces the old grid+hero-only gate:
    treating a game as "done" once just grid+hero landed permanently
    stranded its logo / icon / landscape (the icon directory held 7
    files for 1196 shortcuts). Callers that need to fetch *only* the
    gaps should call :func:`get_missing_kinds` directly.

    Args:
        grid_dir: absolute path to Steam's ``grid/`` directory.
        app_id: Steam application id (signed or unsigned).

    Returns:
        True iff all five kinds are present on disk (jpg or png).
    """
    return not await get_missing_kinds(grid_dir, app_id)


async def delete_artwork_files(grid_dir: str, app_id: int) -> int:
    """Delete every grid artwork file for ``app_id`` — exist or not.

    All five kinds (grid, grid_l, hero, logo, icon) are written as
    ``<grid_dir>/<unsigned><suffix>`` (see ``_KIND_SUFFIX``), so a single
    ``<unsigned>*`` glob captures them regardless of which extensions
    were actually saved. No existence check is needed — the glob simply
    yields nothing when there's no art. Every Unifideck shortcut appid
    has bit ``0x80000000`` set, so the unsigned form is ≥ 2³¹ and never
    collides with a real Steam appid sharing the directory.

    Args:
        grid_dir: absolute path to Steam's ``grid/`` directory.
        app_id: shortcut appid (signed or unsigned — normalised here).

    Returns:
        Count of files unlinked.
    """
    import asyncio

    # Steam stores shortcut art under the *unsigned* 32-bit appid.
    unsigned = to_unsigned(app_id)

    def _sweep() -> int:
        base = Path(grid_dir)
        if not base.is_dir():
            return 0
        count = 0
        for match in base.glob(f"{unsigned}*"):
            if not match.is_file():
                continue
            try:
                match.unlink(missing_ok=True)
                count += 1
            except OSError:
                logger.exception(
                    "[artwork] unlink(%s) failed", match,
                )
        return count

    return await asyncio.to_thread(_sweep)


async def find_artwork_url(
    title: str,
    kind: str,
    api_key: str,
    config: ConfigManager | None,
) -> str | None:
    """Resolve the best SGDB artwork URL for a given game title + kind.

    Thin delegation wrapper over ``steam.steamgriddb.search_artwork``
    Two responsibilities:

    1. **Lazy import** of the SGDB client to keep this
       module's import graph clean — the SGDB module pulls
       in aiohttp + config helpers, neither of which we
       want loaded just because the artwork service was
       instantiated;
    2. **Exception barrier** — SGDB outages must NEVER
       block a library sync. Any exception coming out of
       ``search_artwork`` (network, JSON parse, auth, rate
       limit, malformed response) is caught, logged at
       DEBUG (not ERROR — this is expected behaviour
       during SGDB hiccups), and translated to ``None``.

    The caller treats ``None`` as "no artwork available
    right now, try again next sync" — there's no retry
    here because the service-level sync loop already
    drives the retry cadence.

    Args:
        title: game title to search for (caller is
            responsible for any normalisation —
            we pass it through verbatim).
        kind: artwork kind to fetch (``"grid"``,
            ``"hero"``, ``"logo"``, ``"icon"``); SGDB
            endpoints are resolved inside ``search_artwork``.
        api_key: SGDB API key (Bearer token). Empty
            string is acceptable — ``search_artwork``
            handles the "no key" case by returning None.
        config: optional ``ConfigManager`` for overriding
            the SGDB base URL (used in tests + when
            self-hosting an SGDB mirror).

    Returns:
        Absolute HTTPS URL of the highest-ranked artwork
        of the requested kind, or ``None`` on any failure
        mode (no key, no match, network error, malformed
        response, etc).
    """
    try:
        from unifideck.steam import steamgriddb
        return await steamgriddb.search_artwork(title, kind, api_key, config=config)
    except Exception as e:
        logger.debug("[ArtworkService] search failed (%s/%s): %s", title, kind, e)
        return None


async def _fetch_url_bytes(url: str, timeout: int) -> bytes | None:  # noqa: ASYNC109 — timeout is API value passed to underlying lib (urllib/aiohttp/subprocess), not an asyncio.timeout() wrapper
    """Download ``url`` and return its body bytes, or None on failure.

    Pure network slice extracted from ``download_and_save``:

    * Fresh ``aiohttp.ClientSession`` per call — artwork
      downloads are infrequent enough that pooling overhead
      isn't worth the lifecycle complexity. The combined
      ``async with`` closes both session and response even
      on exception.
    * Non-200 response → ``None`` with no log (404 from
      SGDB CDN is routine during reindex events).
    * Any HTTP-side exception (timeout, DNS, TLS, partial
      read, reset) → ``None`` at DEBUG level — transient
      and retryable next sync cycle.

    Caller responsibility: distinguishing a fetch failure
    from an empty body. Both manifest as ``None`` here.
    """
    try:
        import aiohttp
        # Per staging: the Deck's cert store is regularly out
        # of date and HTTPS downloads fail on TLS verification
        # if we don't explicitly opt out. Every image-fetch path
        # in staging (``download_image``, all store_metadata
        # helpers) uses ``ssl=False`` for this reason.
        connector = aiohttp.TCPConnector(ssl=False)
        client_timeout = aiohttp.ClientTimeout(total=timeout)
        async with (
            aiohttp.ClientSession(connector=connector, timeout=client_timeout) as session,
            session.get(url, timeout=client_timeout) as resp,
        ):
            if resp.status != 200:
                return None
            return await resp.read()
    except Exception as e:
        logger.debug("[ArtworkService] download failed: %s", e)
        return None


async def download_and_save(
    grid_dir: str, app_id: int, kind: str, url: str, timeout: int,  # noqa: ASYNC109 — timeout is API value passed to underlying lib (urllib/aiohttp/subprocess), not an asyncio.timeout() wrapper
) -> bool:
    """Download an artwork URL and save it to Steam's grid directory.

    Three-stage pipeline:

    1. **Suffix resolution** via ``_suffix_for(kind, url)``
       so the saved filename's extension matches the
       actual byte content — Steam's CEF artwork reader
       rejects files with mismatched extension + MIME.
    2. **HTTP fetch** via ``_fetch_url_bytes`` (extracted
       helper handling status check + HTTP exception
       barrier at DEBUG level).
    3. **Atomic write** via ``async_file_ops.write_bytes``
       which handles the tmp-file + ``os.replace`` +
       fsync dance. Filesystem exceptions logged at WARN
       — these typically indicate a real config problem
       (permission, disk full, RO mount) the user should
       see in the Decky log.

    Artwork is intentionally best-effort: any failure
    returns False without raising. The next sync cycle
    will retry the same URL.

    Args:
        grid_dir: absolute path to Steam's ``grid/``
            directory. Must already exist — directory
            bootstrap is the caller's responsibility.
        app_id: Steam application id (unsigned 32-bit).
        kind: artwork kind for suffix resolution.
        url: HTTPS URL to download from (typically an
            SGDB CDN URL with a signed query string).
        timeout: total HTTP timeout in seconds.

    Returns:
        True iff fetch returned 200 AND bytes persisted to
        disk. False on any failure mode. Never raises.
    """
    suffix = _suffix_for(kind, url)
    # Steam stores grid filenames under the *unsigned* 32-bit
    # AppID. Convert here so callers can pass either signed
    # (``Game.app_id`` as produced by ``generate_app_id``) or
    # unsigned interchangeably — and so the filenames match what
    # ``has_artwork`` checks for. Mismatch caused every cover to
    # be re-fetched on every sync and Steam's UI to find none of
    # them on disk.
    unsigned = to_unsigned(app_id)
    target = str(Path(grid_dir) / f"{unsigned}{suffix}")
    data = await _fetch_url_bytes(url, timeout)
    if data is None:
        return False
    from unifideck.core.io import async_file_ops as aio
    try:
        await aio.write_bytes(target, data)
        return True
    except Exception as e:
        logger.warning("[ArtworkService] save failed: %s", e)
        return False
