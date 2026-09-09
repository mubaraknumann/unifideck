"""
Wine prefix path enumeration helpers.

``UbisoftPrefixPaths`` knows how to walk a Wine prefix and list the user
home directories inside it. Wine prefixes commonly contain multiple
"users" under ``drive_c/users/`` (e.g. ``steamuser``, ``Public``, plus
optionally per-Steam-user folders); the order in which they're visited
matters because UPC payload files are picked up from the *first* user
home that contains them.

Key method: ``iter_user_homes(prefix, pfx_first=False)`` which yields
``(root, user_home)`` tuples. The ``pfx_first`` flag is used by the
session-propagation code to ensure the prefix-default user is tried
before the Steam users — required for DPAPI credential matching.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from pathlib import Path

from .config import UbisoftConfig


class UbisoftPrefixPaths:
    """Ubisoft prefix paths."""

    def __init__(self, config: UbisoftConfig) -> None:
        """Initialize the instance."""
        self._config = config
        # Wired post-construction by ``build_ubisoft_specialists`` once the
        # id_map exists (avoids a paths→id_map→sources→paths import cycle).
        # ``_prefix_resolver`` maps space_id → recorded absolute prefix path;
        # ``_recorded_lister`` returns all recorded external prefix paths.
        self._prefix_resolver: Callable[[str], str | None] | None = None
        self._recorded_lister: Callable[[], list[str]] | None = None

    def set_prefix_registry(
        self,
        *,
        resolver: Callable[[str], str | None],
        lister: Callable[[], list[str]],
    ) -> None:
        """Wire the per-game prefix-location registry (from the id_map)."""
        self._prefix_resolver = resolver
        self._recorded_lister = lister

    def find_upc_exe(self, prefix_path: str) -> str | None:
        """Find UPC exe."""
        return self._find_in_prefix(
            prefix_path,
            self._config.upc_relative_path,
        )

    def find_connect_exe(self, prefix_path: str) -> str | None:
        """Find connect exe."""
        return self._find_in_prefix(
            prefix_path,
            self._config.upc_connect_relative_path,
        )

    def find_configurations(
        self,
        prefix_path: str,
    ) -> str | None:
        """Find configurations."""
        return self._find_in_prefix(
            prefix_path,
            self._config.configurations_relative_path,
        )

    def iter_user_homes(
        self,
        prefix_path: str,
        pfx_first: bool = False,
    ) -> Iterator[tuple[str, str]]:
        """Iter user homes."""
        roots = [
            prefix_path,
            str(Path(prefix_path) / "pfx"),
        ]
        if pfx_first:
            roots = list(reversed(roots))
        skip = set(self._config.wine_system_users)
        for prefix_root in roots:
            users_dir = Path(prefix_root) / "drive_c" / "users"
            if not users_dir.is_dir():
                continue
            try:
                entries = list(users_dir.iterdir())
            except OSError:
                continue
            for entry in entries:
                if entry.name in skip:
                    continue
                if entry.is_dir():
                    yield prefix_root, str(entry)

    def get_prefix_path(self, space_id: str) -> str:
        """Resolve a game's Wine-prefix path.

        Prefers the per-game location recorded at install time (games can
        be installed to SD / custom storage); falls back to the fixed
        internal ``prefixes_dir/<space_id>`` for games installed before
        this existed.
        """
        if self._prefix_resolver is not None:
            recorded = self._prefix_resolver(space_id)
            if recorded:
                return recorded
        return str(
            Path(self._config.prefixes_dir_expanded) / space_id,
        )

    def iter_all_game_prefix_paths(self) -> list[str]:
        """Every per-game prefix dir — fixed-base scan ∪ recorded externals.

        ``config.iter_game_prefix_paths`` only scans the internal
        ``prefixes_dir``; prefixes relocated to SD / custom storage live
        elsewhere and are tracked in the id_map. Dangling entries (e.g. an
        unmounted SD card) are skipped via the ``is_dir`` guard.
        """
        result = list(self._config.iter_game_prefix_paths())
        seen: set[str] = set()
        for path in result:
            with contextlib.suppress(OSError):
                seen.add(str(Path(path).resolve()))
        if self._recorded_lister is not None:
            for path in self._recorded_lister():
                try:
                    resolved = str(Path(path).resolve())
                except OSError:
                    continue
                if resolved in seen:
                    continue
                if Path(path).is_dir():
                    seen.add(resolved)
                    result.append(path)
        return result

    @staticmethod
    def _find_in_prefix(
        prefix_path: str,
        relative: str,
    ) -> str | None:
        """Find in prefix."""
        prefix = Path(prefix_path)
        for candidate in (
            prefix / relative,
            prefix / "pfx" / relative,
        ):
            if candidate.is_file() or candidate.is_dir():
                return str(candidate)
        return None
