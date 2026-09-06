"""
Per-game manifest — typed view of UPC's per-game configuration.

A UPC manifest is the YAML-like blob describing how a game is launched,
patched, and updated. ``UbisoftGameManifest`` is the typed parser for
that blob: it extracts the executable name, supported languages, save
locations, and launch arguments. ``_ManifestLoader`` walks the prefix's
configuration directory and loads the manifest for a given space_id.

The manifest is also queried by the launcher to know which executable
to actually launch (modern Ubisoft titles have a launcher shim that
forwards to the real game executable — the manifest tells us the
forwarding chain).
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from unifideck.core.types import Game
from unifideck.stores.ubisoft.config import UbisoftConfig
from unifideck.stores.ubisoft.id_map import UbisoftIdMap

logger = logging.getLogger(__name__)

def _first_non_empty(
    raw: dict[str, Any],
    keys: tuple[str, ...],
) -> str:
    """First non empty."""
    for key in keys:
        val = raw.get(key)
        if val:
            stripped = str(val).strip()
            if stripped:
                return stripped
    return ""

@dataclass
class _VisibleManifestIndex:
    """Visible manifest index."""

    by_norm: dict[str, dict[str, Any]] = field(default_factory=dict)
    by_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    norms: set[str] = field(default_factory=set)
    ids: set[str] = field(default_factory=set)

    def lookup(
        self,
        game_id: str,
        norm_title: str,
    ) -> dict[str, Any] | None:
        """Lookup."""
        return self.by_id.get(game_id) or self.by_norm.get(norm_title)

    def matches(self, game_id: str, norm_title: str) -> bool:
        """Matches."""
        return game_id in self.ids or norm_title in self.norms

class _VisibleManifestProcessor:
    """Visible manifest processor."""

    def __init__(
        self,
        config: UbisoftConfig,
        id_map: UbisoftIdMap,
        load_json_file_safe: Callable[[str], Any | None],
    ) -> None:
        """Initialize the instance."""
        self._config = config
        self._id_map = id_map
        self._load_json_file_safe = load_json_file_safe

    def load_manifest(self) -> list[dict[str, Any]]:
        """Load manifest."""
        manifest_file = self._config.visible_games_file_expanded
        if not Path(manifest_file).is_file():
            return []
        payload = self._load_json_file_safe(manifest_file)
        if payload is None:
            logger.warning(
                "[UbisoftLibrary] visible manifest load failed",
            )
            return []
        raw_games = payload.get("games", []) if isinstance(payload, dict) else payload
        if not isinstance(raw_games, list):
            return []
        manifest: list[dict[str, Any]] = []
        for raw in raw_games:
            if not isinstance(raw, dict):
                continue
            entry = self._normalize_entry(raw)
            if entry:
                manifest.append(entry)
        return manifest

    @staticmethod
    def _normalize_entry(
        raw: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Normalize entry."""
        title = _first_non_empty(raw, ("title", "name"))
        if not title:
            return None
        install_id = _first_non_empty(raw, ("install_id",))
        launch_id = _first_non_empty(raw, ("launch_id",)) or install_id
        return {
            "title": title,
            "space_id": _first_non_empty(
                raw,
                ("space_id", "spaceId"),
            ),
            "install_id": install_id,
            "launch_id": launch_id,
            "ubisoftconnect_game_id": _first_non_empty(
                raw,
                ("ubisoftconnect_game_id", "product_id"),
            ),
            "cover_image": _first_non_empty(
                raw,
                ("cover_image", "coverUrl", "thumb_url"),
            ),
            "ownership_type": (_first_non_empty(raw, ("ownership_type",)) or "owned"),
            "source": (_first_non_empty(raw, ("source",)) or "visible_manifest"),
        }

    @staticmethod
    def _game_id_for(entry: dict[str, Any]) -> str:
        """Game ID for."""
        space_id = str(entry.get("space_id") or "").strip()
        if space_id:
            return space_id
        install_id = str(entry.get("install_id") or "").strip()
        if install_id:
            return f"ubi-{install_id}"
        digest = hashlib.sha256(
            str(entry.get("title", "")).encode("utf-8"),
        ).hexdigest()[:12]
        return f"ubi-visible-{digest}"

    def _merge_into_id_map(self, entry: dict[str, Any]) -> bool:
        """Merge into ID map."""
        cache_key = self._game_id_for(entry)
        fields: dict[str, Any] = {
            "name": entry.get("title") or "",
            "source": "visible_manifest",
        }
        for field_name in ("install_id", "launch_id"):
            value = str(entry.get(field_name) or "").strip()
            if value:
                fields[field_name] = value
        # The deeplink id goes through the precedence gate so a manifest
        # product_id can't displace a registry-confirmed id (#436).
        connect_id = str(entry.get("ubisoftconnect_game_id") or "").strip()
        return self._id_map.set_connect_id(
            cache_key, connect_id, "manifest", fields,
        )

    def _build_index(
        self,
        manifest: list[dict[str, Any]],
    ) -> _VisibleManifestIndex:
        """Build index."""
        index = _VisibleManifestIndex()
        for entry in manifest:
            title = entry.get("title") or ""
            if not title:
                continue
            norm = self._id_map.normalize_for_matching(title)
            index.norms.add(norm)
            index.by_norm[norm] = entry
            entry_id = self._game_id_for(entry)
            index.ids.add(entry_id)
            index.by_id[entry_id] = entry
        return index

    def apply_filter(
        self,
        games: list[Game],
        installed: dict[str, Any],
        manifest: list[dict[str, Any]] | None,
        source_label: str,
    ) -> list[Game]:
        """Apply filter."""
        if not manifest:
            return games
        index = self._build_index(manifest)
        id_map_changed = self._merge_manifest_into_id_map(
            manifest,
        )
        filtered, seen_ids, seen_norms = self._filter_and_enrich_games(
            games,
            index,
            source_label,
        )
        injected = self._inject_unseen_manifest_entries(
            manifest,
            installed,
            filtered,
            seen_ids,
            seen_norms,
            source_label,
        )
        if id_map_changed:
            logger.debug(
                "[UbisoftLibrary] visible %s merged manifest entries into id_map",
                source_label,
            )
        logger.info(
            "[UbisoftLibrary] visible %s filter kept %d "
            "games from %d base entries (+%d injected)",
            source_label,
            len(filtered),
            len(games),
            injected,
        )
        return filtered

    def supplement(
        self,
        games: list[Game],
        installed: dict[str, Any],
        entries: list[dict[str, Any]],
        source_label: str,
    ) -> list[Game]:
        """Enrich + inject ``entries`` without filtering the base library.

        Unlike :meth:`apply_filter` (a whitelist that drops unmatched
        games), this keeps every existing game: matching entries are
        enriched (ownership_type, cover) and their ids merged into the
        id_map, and entries not already present are appended. Used for
        the free-to-play feed supplement, which must never remove an
        owned game.
        """
        if not entries:
            return games
        index = self._build_index(entries)
        self._merge_manifest_into_id_map(entries)
        seen_ids: set[str] = set()
        seen_norms: set[str] = set()
        for game in games:
            norm_title = self._id_map.normalize_for_matching(game.title)
            entry = index.lookup(game.store_game_id, norm_title)
            if entry:
                self._enrich_game_from_entry(game, entry)
            seen_ids.add(game.store_game_id)
            seen_norms.add(norm_title)
        injected = self._inject_unseen_manifest_entries(
            entries,
            installed,
            games,
            seen_ids,
            seen_norms,
            source_label,
        )
        logger.info(
            "[UbisoftLibrary] %s supplement: %d base games (+%d injected)",
            source_label,
            len(games) - injected,
            injected,
        )
        return games

    def _merge_manifest_into_id_map(
        self,
        manifest: list[dict[str, Any]],
    ) -> bool:
        """Merge manifest into ID map."""
        return any(self._merge_into_id_map(entry) for entry in manifest)

    def _enrich_game_from_entry(
        self,
        game: Game,
        entry: dict[str, Any],
    ) -> None:
        """Enrich game from entry."""
        if entry.get("title"):
            game.title = entry["title"]
            if entry.get("ownership_type"):
                game.metadata["ownership_type"] = entry["ownership_type"]
                if entry.get("cover_image"):
                    game.icon_url = entry["cover_image"]
                    if getattr(game, "metadata", None) is None:
                        game.metadata = {}
                        game.metadata.setdefault(
                            "coverUrl",
                            entry["cover_image"],
                        )

    def _filter_and_enrich_games(
        self,
        games: list[Game],
        index: _VisibleManifestIndex,
        source_label: str,
    ) -> tuple[list[Game], set[str], set[str]]:
        """Filter and enrich games."""
        filtered: list[Game] = []
        seen_norms: set[str] = set()
        seen_ids: set[str] = set()
        for game in games:
            norm_title = self._id_map.normalize_for_matching(
                game.title,
            )
            if not index.matches(game.store_game_id, norm_title):
                logger.debug(
                    "[UbisoftLibrary] visible %s skip: %s",
                    source_label,
                    game.title,
                )
                continue
            entry = index.lookup(game.store_game_id, norm_title)
            if entry:
                self._enrich_game_from_entry(game, entry)
            filtered.append(game)
            seen_norms.add(norm_title)
            seen_ids.add(game.store_game_id)
        return filtered, seen_ids, seen_norms

    def _inject_unseen_manifest_entries(
        self,
        manifest: list[dict[str, Any]],
        installed: dict[str, Any],
        filtered: list[Game],
        seen_ids: set[str],
        seen_norms: set[str],
        source_label: str,
    ) -> int:
        """Inject unseen manifest entries."""
        injected = 0
        for entry in manifest:
            game_id = self._game_id_for(entry)
            norm_title = self._id_map.normalize_for_matching(
                entry["title"],
            )
            if game_id in seen_ids or norm_title in seen_norms:
                continue
            install_meta = (
                installed.get(entry.get("space_id", "")) or installed.get(game_id) or {}
            )
            cover_image = entry.get("cover_image") or None
            game = Game(
                app_id=0,
                store="ubisoft",
                store_game_id=game_id,
                title=entry["title"],
                installed=bool(install_meta),
                icon_url=cover_image,
                install_path=install_meta.get("install_path"),
                exe_path=install_meta.get("executable"),
                metadata={
                    "ownership_type": (entry.get("ownership_type") or "owned"),
                },
            )
            if cover_image:
                game.metadata.update(
                    {
                        "coverUrl": cover_image,
                        "backgroundUrl": "",
                        "bannerUrl": "",
                    }
                )
            filtered.append(game)
            seen_ids.add(game_id)
            seen_norms.add(norm_title)
            injected += 1
            logger.info(
                "[UbisoftLibrary] visible %s injected: %s [id=%s]",
                source_label,
                entry["title"],
                game_id,
            )
        return injected
