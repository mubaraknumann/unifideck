"""GOG library facade — owned games + installed-state + display metadata.

``GOGLibrary`` is the public entry point of the library logic for the
GOG store. Responsibilities:

* fetch the owned-games list from GOG.com via the ``embed.gog.com``
  account endpoint (REST, JSON);
* scan ``download_dir`` for installed games (via ``.unifideck-id``
  markers);
* merge owned-list + install-state into uniform ``GameRecord`` entries
  ready for display in the UI;
* trigger marker migration (``library_migration.py``) on first
  run to upgrade pre-v6 markers to the canonical JSON format.

In-memory cached; invalidated on auth state change, install/uninstall,
or manual user refresh.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from unifideck.core.types import Game
from unifideck.utils.paths import get_all_game_directories

from .config import GOGConfig
from .http import build_ssl_context, fetch_json_get
from .library_migration import _MarkerMigration
from .tokens import GOGTokenManager

if TYPE_CHECKING:
    from unifideck.config import ConfigManager

logger = logging.getLogger(__name__)
_INSTALL_MARKER = ".unifideck-id"
_GOG_LIBRARY_TIMEOUT_S = 15.0

class GOGLibrary:
    """Goglibrary."""

    def __init__(
        self,
        config: GOGConfig,
        tokens: GOGTokenManager,
        exe_finder: Callable[[str], str | None] | None = None,
        config_manager: ConfigManager | None = None,
    ) -> None:
        """Initialize the instance."""
        self._config = config
        self._tokens = tokens
        self._find_exe = exe_finder
        # Used to enumerate EVERY install location (internal + per-store
        # + custom + SD/external mounts) when scanning for installed
        # games — not just the single default ``download_dir``. Without
        # it, games installed on the SD card or a custom path can't be
        # found, so uninstall silently no-ops and leaves the install
        # (and its ``goggame-*.info``) on disk.
        self._config_manager = config_manager
        self._migration = _MarkerMigration(self)

    def _install_scan_dirs(self) -> list[str]:
        """Every directory that may hold an installed GOG game.

        ``get_all_game_directories`` covers internal storage, per-store
        dirs, the user's custom path and external (SD) mounts. We append
        the GOG ``download_dir`` defensively in case it's been pointed
        somewhere outside that set.
        """
        dirs = list(get_all_game_directories(self._config_manager))
        seen = {str(Path(d).expanduser()) for d in dirs}
        dd = str(Path(self._config.download_dir).expanduser())
        if dd not in seen:
            dirs.append(dd)
        return dirs

    def migrate_old_markers(self) -> dict[str, int]:
        """Migrate old markers."""
        return self._migration.migrate_old_markers()

    async def is_available(self) -> bool:
        """Check whether available."""
        if not self._tokens.has_tokens:
            loaded = await self._tokens.load()
            if not loaded:
                logger.info(
                    "[GOGLibrary] no tokens — not authenticated",
                )
                return False
        status = await self._probe_userdata()
        if status == 200:
            return True
        if status == 401:
            logger.warning(
                "[GOGLibrary] token expired (401), refreshing",
            )
            ok = await self._tokens.refresh_if_stale()
            if ok:
                status = await self._probe_userdata()
                return status == 200
            logger.warning(
                "[GOGLibrary] GOG token refresh failed - clearing dead credentials",
            )
            await self._tokens.clear()
            return False
        if status == 0:
            # Network/timeout blip — NOT a real 401/403 (HTTPError
            # returns the actual code, so this is unreachable for a
            # genuine auth failure). Don't drop GOG from the sync on a
            # transient probe failure right after login: we still hold
            # tokens (checked above), and fetch_library refreshes and
            # fails visibly if the token is actually dead (UD-005).
            logger.warning(
                "[GOGLibrary] userdata probe unreachable "
                "(timeout/network); assuming available — tokens present",
            )
            return True
        logger.warning(
            "[GOGLibrary] userdata probe returned unexpected status %s "
            "— treating GOG as unavailable",
            status,
        )
        return False

    async def _probe_userdata(self) -> int:
        """Probe userdata."""
        url = f"{self._config.base_url}/userData.json"
        access = self._tokens.access_token
        if not access:
            return 0
        if not url.startswith("https://"):
            logger.error(
                "[GOGLibrary] refusing non-https probe URL: %s",
                url,
            )
            return 0

        def _probe_sync() -> int:
            """Probe sync."""
            try:
                ctx = build_ssl_context()
                req = urllib.request.Request(
                    url,
                    headers={
                        "Authorization": f"Bearer {access}",
                        "User-Agent": self._config.user_agent,
                    },
                )
                with urllib.request.urlopen(
                    req,
                    timeout=5.0,
                    context=ctx,
                ) as response:
                    return cast("int", response.status)
            except urllib.request.HTTPError as e:
                return e.code
            except Exception as e:
                logger.warning(
                    "[GOGLibrary] userdata probe error (returning 0): %s",
                    e,
                )
                return 0

        return await asyncio.to_thread(_probe_sync)

    async def fetch_library(self) -> list[Game]:
        """Fetch library."""
        # Refresh a stale token BEFORE the fetch loop (mirrors
        # get_game_slug). Without this, an in-memory-but-expired
        # token makes page 1 return 401 → _fetch_json returns None →
        # the loop breaks → we hand back an empty library that reads
        # as "0 games" with only a warning (UD-005).
        if not await self._tokens.refresh_if_stale():
            logger.warning(
                "[GOGLibrary] fetch aborted — no valid token "
                "(refresh failed/absent)",
            )
            return []
        if not self._tokens.access_token:
            logger.warning("[GOGLibrary] not authenticated")
            return []
        games: list[Game] = []
        current_page = 1
        total_pages = 1
        base_url = self._config.base_url
        while current_page <= total_pages:
            url = (
                f"{base_url}/account/getFilteredProducts?"
                f"mediaType=1&page={current_page}"
            )
            data = await self._fetch_json(url)
            if data is None:
                logger.error(
                    "[GOGLibrary] page %d failed, stopping",
                    current_page,
                )
                break
            if current_page == 1:
                total_pages = int(
                    data.get("totalPages", 1) or 1,
                )
                total_results = int(
                    data.get("totalGamesFound", 0) or 0,
                )
                logger.info(
                    "[GOGLibrary] library has %d games across %d pages",
                    total_results,
                    total_pages,
                )
            for product in data.get("products", []):
                game_id = str(product.get("id", ""))
                if not game_id:
                    continue
                games.append(
                    Game(
                        app_id=0,
                        store="gog",
                        store_game_id=game_id,
                        title=product.get("title", "") or "",
                        installed=False,
                    )
                )
            current_page += 1
        logger.info(
            "[GOGLibrary] fetched %d games total",
            len(games),
        )
        return games

    async def get_game_slug(self, game_id: str) -> str | None:
        """Get game slug."""
        if not await self._tokens.refresh_if_stale():
            return None
        access = self._tokens.access_token
        if not access:
            return None
        url = f"{self._config.api_gog_url}/products/{game_id}?locale=en-US"
        data = await self._fetch_json(
            url,
            headers={
                "Authorization": f"Bearer {access}",
                "User-Agent": self._config.user_agent,
            },
        )
        if not isinstance(data, dict):
            return None
        slug = data.get("slug")
        if isinstance(slug, str) and slug:
            return slug
        links = data.get("links", {})
        if isinstance(links, dict):
            product_card = links.get("product_card", "")
            if isinstance(product_card, str) and "/game/" in product_card:
                return product_card.split("/game/")[-1].rstrip("/")
        return None

    def get_installed(self) -> list[str]:
        """Get installed."""
        installed: list[str] = []
        for base in self._install_scan_dirs():
            base_path = Path(base).expanduser()
            if not base_path.is_dir():
                continue
            try:
                for entry in base_path.iterdir():
                    if not entry.is_dir():
                        continue
                    game_id = self._read_marker(str(entry))
                    if game_id:
                        installed.append(game_id)
            except OSError:
                logger.exception(
                    "[GOGLibrary] get_installed scan failed at %s", base,
                )
        # Dedupe (a game id could appear under more than one scanned dir).
        installed = list(dict.fromkeys(installed))
        logger.info(
            "[GOGLibrary] found %d installed games",
            len(installed),
        )
        return installed

    def get_installed_map(self) -> dict[str, dict[str, str | None]]:
        """All installed GOG games, keyed by game id, in one disk walk.

        Returns ``{game_id: {"install_path": <dir>, "executable": <exe>}}``.

        Used by the full-library sync to overlay install status onto the
        owned list (``get_library`` →
        ``shared/install_status.merge_install_status``). A
        single pass over every scan dir — vs ``get_installed_game_info``'s
        per-game rescan — so the merge is O(dirs × entries), not
        O(installed × dirs × entries). First match per game id wins (a
        game id could appear under more than one scanned dir).

        Keys off the ``.unifideck-id`` marker only: every install this
        plugin performs writes one, so it's authoritative for the bulk
        overlay. The ``goggame-{id}.info`` fallback in
        :meth:`_match_install_dir` is intentionally NOT used here — it can
        only be driven from a known target id, which would reintroduce the
        per-game rescan cost; it stays reserved for the single-game callers
        (uninstall, DLC, App-Details size, update checks).
        """
        found: dict[str, dict[str, str | None]] = {}
        for base in self._install_scan_dirs():
            base_path = Path(base).expanduser()
            if not base_path.is_dir():
                continue
            try:
                for entry in base_path.iterdir():
                    if not entry.is_dir():
                        continue
                    game_id = self._read_marker(str(entry))
                    if game_id and game_id not in found:
                        found[game_id] = self._install_info(str(entry))
            except OSError:
                logger.exception(
                    "[GOGLibrary] get_installed_map scan failed at %s", base,
                )
        logger.info(
            "[GOGLibrary] install map: %d installed games", len(found),
        )
        return found

    def get_installed_game_info(self, game_id: str) -> dict[str, str | None] | None:
        """Get installed game info.

        Scans EVERY install location (internal, per-store, custom, SD /
        external mounts), not just the default ``download_dir`` — so a
        game installed on the SD card or a custom path is found and can
        actually be uninstalled.
        """
        for base in self._install_scan_dirs():
            found = self._scan_base_for_game(base, game_id)
            if found is not None:
                return found
        return None

    def _scan_base_for_game(
        self, base: str, game_id: str,
    ) -> dict[str, str | None] | None:
        """Scan one install-base dir for ``game_id``'s install dir."""
        base_path = Path(base).expanduser()
        if not base_path.is_dir():
            return None
        try:
            for entry in base_path.iterdir():
                if not entry.is_dir():
                    continue
                info = self._match_install_dir(str(entry), game_id)
                if info is not None:
                    return info
        except OSError:
            logger.exception(
                "[GOGLibrary] get_installed_game_info scan failed at %s",
                base,
            )
        return None

    def _match_install_dir(
        self, game_dir: str, game_id: str,
    ) -> dict[str, str | None] | None:
        """Install info if ``game_dir`` is ``game_id``'s install — by marker,
        else by goggame-info fallback — or None."""
        found = self._read_marker(game_dir)
        if found == game_id:
            return self._install_info(game_dir)
        if found is None and self._has_goggame_info(game_dir, game_id):
            logger.info(
                "[GOGLibrary] found %s via goggame info fallback at %s",
                game_id,
                game_dir,
            )
            return self._install_info(game_dir)
        return None

    def _install_info(self, game_dir: str) -> dict[str, str | None]:
        """The ``{install_path, executable}`` record for an install dir."""
        return {
            "install_path": game_dir,
            "executable": self._resolve_exe(game_dir),
        }

    @staticmethod
    def _read_marker(game_dir: str) -> str | None:
        """Read marker."""
        marker_path = Path(game_dir) / _INSTALL_MARKER
        if not marker_path.is_file():
            return None
        try:
            content = marker_path.read_text(
                encoding="utf-8",
            ).strip()
        except OSError as e:
            logger.warning(
                "[GOGLibrary] marker read failed: %s",
                e,
            )
            return None
        if not content:
            return None
        with contextlib.suppress(json.JSONDecodeError):
            data = json.loads(content)
            if isinstance(data, dict):
                return data.get("game_id") or data.get("gameId")
            if isinstance(data, (str, int)):
                return str(data)
        return content

    @staticmethod
    def _has_goggame_info(game_dir: str, game_id: str) -> bool:
        """Has goggame info."""
        for candidate in (
            game_dir,
            str(Path(game_dir) / "game"),
        ):
            try:
                if not Path(candidate).is_dir():
                    continue
                target = f"goggame-{game_id}.info"
                if (Path(candidate) / target).is_file():
                    return True
            except OSError:
                continue
        return False

    def _resolve_exe(self, install_path: str) -> str | None:
        """Resolve exe."""
        if self._find_exe is None:
            return None
        try:
            return self._find_exe(install_path)
        except Exception as e:
            logger.warning(
                "[GOGLibrary] exe resolution failed: %s",
                e,
            )
            return None

    async def _fetch_json(
        self,
        url: str,
        headers: dict[str, str] | None = None,
    ) -> Any | None:
        """Fetch JSON."""
        return await fetch_json_get(
            url,
            bearer=self._tokens.access_token,
            user_agent=self._config.user_agent,
            timeout=_GOG_LIBRARY_TIMEOUT_S,
            extra_headers=headers,
            log_prefix="[GOGLibrary]",
        )
