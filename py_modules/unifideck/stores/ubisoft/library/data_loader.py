"""
Load installed-state from Unifideck install markers.

``_DataLoader`` walks every per-game install directory under
``UbisoftConfig.default_install_base_expanded`` and reads each
``.unifideck-id`` marker into a dict keyed by ``install_id``.

These markers are written by the installer when a game install
completes and serve as the authoritative source of "what Unifideck has
installed". A game without a marker is considered uninstalled even if
its files exist on disk (typically a leftover from a failed install).

Refactor history (2026-05-14): ``_discover_ownership_file`` was a
single method at CC=17 — a nested ``for prefix / for layout`` walk
with stacked guards (prefix exists, layout valid, ownership dir
exists, listdir doesn't raise, entries non-empty). Pulled the
inner search into ``_search_in_prefix`` and the single-directory
probe into ``_first_ownership_entry`` so the outer scan reads as
"try each prefix, return first hit".
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from unifideck.stores.ubisoft.config import UbisoftConfig
    from unifideck.stores.ubisoft.parser import GameConfig
    from unifideck.stores.ubisoft.paths import UbisoftPrefixPaths

    ParseConfigurationsFn = Callable[[str], list[GameConfig]]
    ParseOwnershipFn = Callable[[str], list[int]]
logger = logging.getLogger(__name__)

class _DataLoader:
    """Data loader."""

    def __init__(
        self,
        *,
        config: UbisoftConfig,
        paths: UbisoftPrefixPaths,
    ) -> None:
        """Initialize the instance."""
        self._config = config
        self._paths = paths

    async def load_configurations(
        self,
        parse_configurations: ParseConfigurationsFn,
    ) -> list[GameConfig] | None:
        """Load configurations."""
        cfg_path = await asyncio.to_thread(
            self._find_library_configurations_path,
        )
        if not cfg_path:
            logger.info(
                "[UbisoftLibrary] no configurations binary found",
            )
            return None
        configs = await asyncio.to_thread(
            parse_configurations,
            cfg_path,
        )
        if not configs:
            logger.warning(
                "[UbisoftLibrary] configurations binary parsed but empty",
            )
            return None
        return configs

    async def load_ownership_set(
        self,
        parse_ownership: ParseOwnershipFn,
    ) -> set[int] | None:
        """Load ownership set."""
        ownership_path, user_id = await asyncio.to_thread(
            self._discover_ownership_file,
        )
        if not ownership_path:
            return None
        owned_ids = await asyncio.to_thread(
            parse_ownership,
            ownership_path,
        )
        owned_set = set(owned_ids)
        user_display = user_id[:8] if user_id else "?"
        logger.info(
            "[UbisoftLibrary] ownership: %d unique IDs (userId=%s…)",
            len(owned_set),
            user_display,
        )
        return owned_set

    async def load_ownership_uuids(self) -> set[str]:
        """Owned product UUIDs from the ownership binary (modern namespace).

        Complements :meth:`load_ownership_set` (numeric install_ids): UPC's
        ownership binary records each entitlement under both a numeric id and
        a product UUID (= Algolia ``appId``/``spaceId``). The UUIDs resolve to
        names via unifiDB's ``uuid_catalog.json`` — the only way to name modern
        games the legacy install_id list lacks. Empty set when no ownership
        file is present.
        """
        ownership_path, _user_id = await asyncio.to_thread(
            self._discover_ownership_file,
        )
        if not ownership_path:
            return set()
        from unifideck.stores.ubisoft.parser import parse_ownership_uuids
        uuids = await asyncio.to_thread(parse_ownership_uuids, ownership_path)
        return set(uuids)

    def _find_library_configurations_path(self) -> str | None:
        """Find library configurations path."""
        for prefix_dir in (
            self._config.auth_prefix_dir_expanded,
            self._config.template_dir_expanded,
        ):
            cfg_path = self._paths.find_configurations(prefix_dir)
            if cfg_path:
                return cfg_path
        return None

    def _discover_ownership_file(self) -> tuple[str | None, str]:
        """Locate a usable Ubisoft ownership file across known prefixes.

        Walks the two candidate Wine-prefix directories (the auth
        prefix and the template prefix) in priority order ; for
        each, delegates to :py:meth:`_search_in_prefix` which
        handles the two-layout fallback (root-mounted vs ``pfx/``-
        nested install). Returns ``(path, user_id)`` for the
        first hit or ``(None, "")`` when nothing is found.

        The caller (``load_ownership_set``) interprets ``user_id``
        as the filename of the ownership file ; an empty string
        means "no file" and short-circuits the subsequent parse.
        """
        for prefix_dir in (
            self._config.auth_prefix_dir_expanded,
            self._config.template_dir_expanded,
        ):
            result = self._search_in_prefix(Path(prefix_dir))
            if result is not None:
                return result
        return (None, "")

    # ─────────────────────────────────────────────────────────────
    # Helpers extracted from the former CC=17 _discover_ownership_file
    # ─────────────────────────────────────────────────────────────

    def _search_in_prefix(
        self, prefix_p: Path,
    ) -> tuple[str, str] | None:
        """Probe both Wine-prefix layouts under ``prefix_p`` for ownership.

        Wine prefixes ship in two flavours :

            * **Root-mounted** — ``prefix/drive_c/...``. Used by
              older builds and most upstream Proton layouts.
            * **``pfx``-nested** — ``prefix/pfx/drive_c/...``. The
              umu-launcher default since 2025.

        We try both and return the first hit. Returns ``None``
        when neither layout contains an ownership file (caller
        moves on to the next prefix).
        """
        if not prefix_p.is_dir():
            return None
        for layout_sub in ("", "pfx"):
            base = prefix_p / layout_sub if layout_sub else prefix_p
            ownership_dir = base / self._config.ownership_relative_path
            result = self._first_ownership_entry(ownership_dir)
            if result is not None:
                return result
        return None

    @staticmethod
    def _first_ownership_entry(
        ownership_dir: Path,
    ) -> tuple[str, str] | None:
        """Pick the first file inside ``ownership_dir``.

        Ubisoft's user-id naming scheme guarantees at most one
        file in this directory, but we defensively pick
        ``entries[0]`` rather than asserting uniqueness — a stale
        file from a previous account left behind by an aborted
        cleanup would otherwise raise here.

        Returns ``(absolute_path, filename)`` or ``None`` for
        missing-dir / empty / unreadable cases (all three are
        the same "no usable file" outcome from the caller's
        perspective).
        """
        if not ownership_dir.is_dir():
            return None
        try:
            entries = [e for e in ownership_dir.iterdir() if e.is_file()]
        except OSError:
            return None
        if not entries:
            return None
        entry = entries[0]
        return str(entry), entry.name
