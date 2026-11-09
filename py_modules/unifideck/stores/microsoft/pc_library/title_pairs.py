"""stores/microsoft/pc_library/title_pairs.py — one Xbox game, PC and cloud.

An Xbox game can be streamed (an xCloud row), installed (a PC title) or
both. Most pairs share a product id and merge on it. Many do not: PC Game
Pass and older PC releases have their own PC product, so the same game is
listed twice under two ids (79 such pairs on the dev account, 2026-10-21).

This module pairs those by title. The rule is narrow on purpose:

- Only platform words are ignored: "(PC)", "Windows", "for Windows",
  "Windows Edition", "for Xbox", "Xbox One", "Xbox Series X|S". Edition
  words stay, so "Fallout 3: Game of the Year Edition" and "Fallout 3" are
  different games here, while "Skyrim Special Edition (PC)" and "Skyrim
  Special Edition" are one.
- A pair must be unique. Two PC titles with the same name pair with
  nothing. Several xCloud rows with the same name (an Xbox One and an
  Xbox Series X|S SKU) pair only when one ranks first: no platform word,
  then Series X|S.

Anything left unpaired stays a row of its own, and the cross-store game
grouping still shows it next to its twin, as before.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Protocol

from unifideck.core.types import Game
from unifideck.utils.title_match import fold_roman_numerals, normalize_for_match

#: Platform qualifiers, as they read after ``normalize_for_match``. Longest
#: first, so "xbox series xs edition" is removed before "xbox".
_PLATFORM_TAILS = (
    "xbox series xs edition", "xbox series x s edition", "xbox one edition",
    "windows 10 edition", "windows edition", "pc edition",
    "windows version", "pc version",
    "xbox series xs", "xbox series x s", "xbox one",
    "for windows 10", "for windows", "for xbox", "for pc",
    "windows 10", "windows", "xbox", "pc",
)
_TAIL_RE = re.compile(r"(?:^|\s)(?:" + "|".join(re.escape(t) for t in _PLATFORM_TAILS) + r")$")
_STANDARD_RE = re.compile(r"(?:^|\s)(?:digital )?standard(?: edition)?$")
#: Rank of an xCloud row's own platform words: lower is preferred.
_RANK_PLAIN, _RANK_SERIES, _RANK_OTHER = 0, 1, 2


class _Titled(Protocol):
    @property
    def title(self) -> str: ...


def _strip_tails(normalized: str) -> tuple[str, list[str]]:
    """``normalized`` without trailing platform words, and the words removed."""
    removed: list[str] = []
    while True:
        match = _TAIL_RE.search(normalized)
        if not match or match.start() == 0:
            return normalized, removed
        removed.append(match.group(0).strip())
        normalized = normalized[:match.start()].rstrip()


def game_key(title: str) -> str:
    """The name an Xbox game is known by on every platform, or ``""``."""
    base, _ = _strip_tails(normalize_for_match(title))
    base = _STANDARD_RE.sub("", base).strip() or base
    return fold_roman_numerals(base)


def _rank(title: str) -> int:
    """How plainly an xCloud row names the game (see the module docstring)."""
    _, removed = _strip_tails(normalize_for_match(title))
    if not removed:
        return _RANK_PLAIN
    return _RANK_SERIES if any("series" in word for word in removed) else _RANK_OTHER


def _unique_keys(items: Sequence[_Titled]) -> dict[str, int]:
    """Key -> index for keys that exactly one item has."""
    seen: dict[str, int] = {}
    clashes: set[str] = set()
    for i, item in enumerate(items):
        key = game_key(item.title)
        if not key:
            continue
        if key in seen:
            clashes.add(key)
        seen[key] = i
    return {k: i for k, i in seen.items() if k not in clashes}


def _best_rows(rows: Sequence[Game], free: set[int]) -> dict[str, int]:
    """Key -> the xCloud row a PC title may join, when one ranks first."""
    by_key: dict[str, list[int]] = {}
    for i in sorted(free):
        key = game_key(rows[i].title)
        if key:
            by_key.setdefault(key, []).append(i)
    best: dict[str, int] = {}
    for key, indices in by_key.items():
        ranked = sorted(indices, key=lambda i: _rank(rows[i].title))
        if len(ranked) == 1 or _rank(rows[ranked[0]].title) < _rank(rows[ranked[1]].title):
            best[key] = ranked[0]
    return best


def pair_by_title(rows: Sequence[Game], free: set[int], titles: Sequence[_Titled]) -> dict[int, int]:
    """Which unclaimed xCloud row each unmatched PC title joins.

    ``free`` holds the indices of xCloud rows no title claimed by id.
    Returns ``{row index: title index}``.
    """
    best = _best_rows(rows, free)
    return {best[key]: t for key, t in _unique_keys(titles).items() if key in best}
