"""What GameVault tells the store switcher about its own builds of a game.

A vault can hold several builds of one game under the same title, such as a
Linux port beside the Windows original. The switcher labels those builds from
``disambiguators`` and orders them by ``default_rank``, both read from a
game's metadata (see ``src/lib/variant-labels.ts`` and
``src/lib/game-grouping.ts``). This module says what they are for GameVault.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .filename import ParsedName

# The plugin runs a Linux build natively; a Windows build needs Proton.
_RANK_LINUX = 0
_RANK_OTHER = 1


def variant_metadata(parsed: ParsedName, tags: Sequence[str]) -> dict[str, Any]:
    """``default_rank`` always, plus ``disambiguators`` when there are any.

    Entries run from most to least telling: platform, the bracket tags as one
    entry, then the version as written in the filename.
    """
    entries: list[str] = []
    if parsed.game_type is not None:
        entries.append("Linux" if parsed.is_linux else "Windows")
    if tags:
        entries.append(", ".join(tags))
    if parsed.version:
        entries.append(parsed.version)

    meta: dict[str, Any] = {
        "default_rank": _RANK_LINUX if parsed.is_linux else _RANK_OTHER,
    }
    if entries:
        meta["disambiguators"] = entries
    return meta
