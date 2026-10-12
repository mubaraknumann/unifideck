"""GameVault remote catalog reader."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from unifideck.core.types import Game

from .filename import extract_tags, leaf_name, parse_archive_name, strip_source_tags
from .variants import variant_metadata

if TYPE_CHECKING:
    from .auth import GameVaultAuth

logger = logging.getLogger(__name__)

STORE_NAME = "gamevault"

_PAGE_SIZE = 500          # fetch 500 per page (nestjs-paginate allows unlimited)
_MAX_GAMES = 5_000        # sanity cap — no home server has >5000 games


class GameVaultFetchError(RuntimeError):
    """The library could not be read, so the answer is unknown.

    Distinct from "the user owns nothing": an empty list is a real answer
    the reconcile acts on, and acting on a failed fetch is what removes a
    user's shortcuts.
    """


class RemoteCatalog:
    """Reads the game list from a self-hosted GameVault server."""

    def __init__(self, auth: GameVaultAuth) -> None:
        self._auth = auth

    async def fetch(self) -> list[Game]:
        headers = await self._auth.get_auth_headers()
        if not headers:
            raise GameVaultFetchError("not authenticated")
        server_url = self._auth.server_url or ""
        raw_games = await self._fetch_all_pages(
            server_url, headers, self._auth.verify_ssl,
        )
        games: list[Game] = []
        for item in raw_games:
            game = self._map_to_game(item)
            if game:
                games.append(game)
        return games

    # ── Internal helpers ────────────────────────────────────────────

    async def _fetch_all_pages(
        self,
        server_url: str,
        auth_headers: dict[str, str],
        verify_ssl: bool,
    ) -> list[dict[str, Any]]:
        all_items: list[dict[str, Any]] = []
        offset = 0
        total_pages: int | None = None

        import aiohttp
        connector = aiohttp.TCPConnector(ssl=verify_ssl)
        async with aiohttp.ClientSession(connector=connector) as session:
            while True:
                url = (
                    f"{server_url}/api/games"
                    f"?limit={_PAGE_SIZE}&offset={offset}"
                )
                data = await _read_page(session, url, auth_headers, offset)
                page, page_total = _unwrap_page(data, want_meta=total_pages is None)
                if page_total is not None:
                    total_pages = page_total

                if not page:
                    break

                all_items.extend(page)

                # Stop if we've hit the sanity cap
                if len(all_items) >= _MAX_GAMES:
                    logger.warning(
                        "[GameVaultLibrary] Hit max-games cap (%d); stopping pagination.",
                        _MAX_GAMES,
                    )
                    break

                # Stop if nestjs-paginate told us the total page count
                if total_pages is not None:
                    current_page = offset // _PAGE_SIZE + 1
                    if current_page >= total_pages:
                        break
                elif len(page) < _PAGE_SIZE:
                    # Fallback: last page is smaller than requested
                    break

                offset += _PAGE_SIZE

        return all_items

    def _map_to_game(self, item: dict[str, Any]) -> Game | None:
        """Convert a raw API game dict to a unified ``Game`` record."""
        try:
            gv_id = str(item.get("id", ""))
            if not gv_id:
                return None

            title = self._extract_title(item)
            icon_url = self._extract_cover_url(item)
            file_path = item.get("file_path") or ""

            return Game(
                app_id=0,               # filled later by sync service
                store=STORE_NAME,
                store_game_id=gv_id,
                title=title,
                installed=False,        # overridden by the reader's overlay
                icon_url=icon_url,
                metadata={
                    "file_path": item.get("file_path", ""),
                    "release_date": item.get("release_date", ""),
                    "early_access": item.get("early_access", False),
                    **variant_metadata(
                        parse_archive_name(file_path), extract_tags(file_path)
                    ),
                },
            )
        except Exception as exc:
            logger.debug("[GameVaultLibrary] map_to_game error: %s", exc)
            return None

    @staticmethod
    def _extract_title(item: dict[str, Any]) -> str:
        r"""The best display title the API offered, in curation order.

        ``metadata`` first: GameVault's ``metadata`` is the *effective* merged
        record — provider data with the owner's ``user_metadata`` layered over
        it — and the server's own clients prefer it, so a user who renamed a
        game in GameVault gets that name here.

        Then the entity's own top-level ``title``. This is the field that used
        to be missing, and its absence was the whole bug: ``metadata`` is
        ``null`` until a metadata provider has enriched the game, and on a
        self-hosted server with no IGDB credentials that is *every* game. So
        the code fell straight through to the filename parser and shipped
        ``C:\Users\numan\Vault\files\Endless Sky`` as the shortcut name — which
        also starved every title-matched enrichment source we have (SGDB, the
        Steam CDN, unifiDB, Metacritic).

        ``sort_title`` is deliberately not consulted: it is lowercased, so it
        can only ever produce a worse ``AppName``.
        """
        metadata = item.get("metadata") or {}
        if isinstance(metadata, dict):
            title = metadata.get("title") or metadata.get("name", "")
            if isinstance(title, str) and title:
                return title

        # The server derives this one from the filename, so it carries the
        # same uploader tags. ``metadata`` above is curated and left alone.
        top_level = item.get("title")
        if isinstance(top_level, str) and top_level:
            return strip_source_tags(top_level)

        # Last resort: derive from the file path.
        file_path = item.get("file_path") or item.get("path", "")
        if isinstance(file_path, str) and file_path:
            return _parse_title_from_filename(file_path)

        return f"GameVault Game #{item.get('id', '?')}"

    @staticmethod
    def _extract_cover_url(item: dict[str, Any]) -> str | None:
        """Try several known cover fields."""
        for field in ("cover_image", "cover", "thumbnail"):
            val = item.get(field)
            if isinstance(val, str) and val:
                return val
        # Structured boxart
        boxart = item.get("boxart") or item.get("metadata", {}) or {}
        if isinstance(boxart, dict):
            for field in ("url", "background_url", "cover_url"):
                val = boxart.get(field)
                if isinstance(val, str) and val:
                    return val
        return None


# ── Standalone utility ──────────────────────────────────────────────────────

def _parse_title_from_filename(file_path: str) -> str:
    """Derive a human-readable title from a GameVault archive filename.

    A thin adapter over :func:`~.filename.parse_archive_name`, which owns the
    naming grammar for both modes. Kept as a named function because it is the
    fallback the remote path reaches for when the server's metadata lookup
    found nothing, and because falling back to the *filename* — rather than to
    an empty title — is behaviour worth stating where it is used.

    The fallback is ``leaf_name``, not the raw *file_path*: a full Windows
    path as an ``AppName`` is precisely the reported symptom, and it should
    not survive even the last-ditch branch.
    """
    return parse_archive_name(file_path).display_title or leaf_name(file_path)


async def _read_page(
    session: Any, url: str, auth_headers: dict[str, str], offset: int,
) -> Any:
    """One page of ``/api/games``, or ``GameVaultFetchError``.

    Every failure becomes that one exception so the caller cannot mistake a
    dead server for a short library — see ``get_library``'s docstring.
    """
    import aiohttp
    try:
        async with session.get(
            url, headers=auth_headers, timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            if resp.status != 200:
                raise GameVaultFetchError(
                    f"server returned HTTP {resp.status} for offset={offset}",
                )
            return await resp.json()
    except GameVaultFetchError:
        raise
    except Exception as exc:
        raise GameVaultFetchError(
            f"could not read page at offset={offset}: {exc}",
        ) from exc


def _unwrap_page(
    data: Any, *, want_meta: bool,
) -> tuple[list[dict[str, Any]], int | None]:
    """``(items, total_pages)`` from either API shape.

    nestjs-paginate answers ``{data: [...], meta: {...}}``; older servers
    answer a bare list. ``total_pages`` is ``None`` unless *want_meta* and
    the payload carried it.
    """
    if isinstance(data, list):
        return data, None
    if not isinstance(data, dict):
        return [], None
    total_pages: int | None = None
    if want_meta:
        meta = data.get("meta", {})
        total_items = meta.get("totalItems", 0) if isinstance(meta, dict) else 0
        if total_items > _MAX_GAMES:
            raise GameVaultFetchError(
                f"server reports {total_items} games, over the {_MAX_GAMES} "
                f"sanity cap — check server_url, it may be a public demo server",
            )
        if isinstance(meta, dict):
            total_pages = meta.get("totalPages")
    page = data.get("data", data.get("results", []))
    return (page if isinstance(page, list) else []), total_pages
