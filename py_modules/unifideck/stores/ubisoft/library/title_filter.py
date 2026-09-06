"""
Clean and admit-filter raw UPC launcher titles.

Split out of ``game_builder.py`` (was pushing it over the volumetry file
cap). Handles per-title admission: strip mojibake/quoting, then decide
whether a cleaned title looks like a real game (vs. a UPC placeholder,
a `[STEAM]`/`[Uplay` marker row, a beta/test build, or a DLC/expansion
row identifiable purely by keyword — separator-based DLC detection
lives in ``identity_resolver.py`` instead).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .game_builder import _GameBuilder

_MOJIBAKE_REPLACEMENTS = (
    # The replacement strings on the right-hand side intentionally
    # contain "ambiguous" Unicode characters (typographic apostrophe
    # U+2019, trade mark U+2122, registered U+00AE) because the
    # whole purpose of this table is to map mojibake byte sequences
    # back to their correct Unicode glyphs. RUF001 has no signal
    # here.
    ("Â®", "®"),
    ("â¢", "™"),
    ("â¢", "™"),
    ("â", "’"),  # noqa: RUF001 — intentional: mapping mojibake → correct glyph
    ("Â", ""),
)
_SKIP_TITLE_KEYWORDS = re.compile(
    r"\b(test\b|beta|alpha|closed|preorder|pre-order|promotion|"
    r"internal|dev/qc|pts|test server|demo|trial|"
    # iArtorias legacy-list noise rows that aren't ownable games.
    r"subscription|company logo|secured)\b",
    re.IGNORECASE,
)
_SKIP_DLC_KEYWORDS = re.compile(
    r"\b(dlc|season pass|expansion|pack|bonus|soundtrack|"
    r"art ?book|skins?|outfit|costume|weapon|map|mission|"
    r"episode|revolver|kukri|cane-sword|hammer|knife|dagger|"
    r"conspiracy|runaway train|texture|language|starter edition|"
    r"battle pass|car shipment|full stock|full ownership|"
    r"master unlock|paint|perk|club|credit pack|currency pack|"
    r"ownership|ubicollectibles|legion of the dead|"
    r"calling all units)\b",
    re.IGNORECASE,
)
_STORE_MARKER_PATTERN = re.compile(
    r"\[STEAM\]|\[Uplay",
    re.IGNORECASE,
)
_CYRILLIC_PATTERN = re.compile(r"[Ѐ-ӿ]")
_PLACEHOLDER_L_PATTERN = re.compile(r"(l\d+|[A-Z0-9_]+)")
_PLACEHOLDER_LITERALS = frozenset({"a ubisoft game"})

def clean_launcher_title(title: Any) -> str:
    """Clean launcher title."""
    if not isinstance(title, str):
        return ""
    cleaned = title.strip().strip('"').strip("'")
    for bad, good in _MOJIBAKE_REPLACEMENTS:
        cleaned = cleaned.replace(bad, good)
    return cleaned

class _TitleFilter:
    """Admission filter for cleaned launcher titles."""

    def __init__(self, parent: _GameBuilder) -> None:
        """Initialize the instance."""
        self._parent = parent

    def _is_placeholder_title(self, title: str) -> bool:
        """Is launcher placeholder title."""
        cleaned = clean_launcher_title(title)
        if not cleaned:
            return True
        normalized = self._parent._id_map.normalize_for_matching(
            cleaned,
        )
        if normalized in _PLACEHOLDER_LITERALS:
            return True
        return bool(_PLACEHOLDER_L_PATTERN.fullmatch(cleaned))

    def should_skip_launcher_title(self, title: str) -> bool:
        """Should skip launcher title."""
        cleaned = clean_launcher_title(title)
        if not cleaned or len(cleaned.strip()) <= 2:
            return True
        if self._is_placeholder_title(cleaned):
            return True
        if _STORE_MARKER_PATTERN.search(cleaned):
            return True
        if _SKIP_TITLE_KEYWORDS.search(cleaned):
            return True
        if _CYRILLIC_PATTERN.search(cleaned):
            return True
        return bool(_SKIP_DLC_KEYWORDS.search(cleaned))
