"""Which library rows and owned Steam apps are the same game.

One answer, used everywhere the plugin asks "is this the same game?":

* ``core.game_grouping`` stamps it on every library row, which powers the
  "Group duplicates" library tabs and the store switcher on a game page.
* ``core.cross_store_ownership`` asks it which rows belong to the game on
  a Steam Store page, for the "already owned" ribbon.

"Same game" means any version of it. A remaster, a definitive edition, a
platform listing and a bundle edition all group with the base game. Two
things keep different games apart:

* **A year or a sequel number in the name.** "Car Mechanic Simulator 2018"
  and "2021" are different games, as are "Thief" and "Thief II". A year in
  brackets is Steam disambiguating a reused name ("Dead Space (2008)"),
  so it is dropped instead.
* **Different Steam apps behind the same bare name.** When two rows share a
  name, carry no version word, and map to different Steam apps, they are
  different games with one name: Battlefront II (2005) and (2017), Dead
  Space and its remake. A version word explains a different Steam app
  ("Mass Effect Legendary Edition" is not app 17460, and still groups
  with "Mass Effect").

The Steam app of a library row is the ``steam_real_appid`` mapping, a fuzzy
store search. A title identical to an owned Steam game's title overrides it
("Thief" on Xbox was mapped to Thief Gold).

Pure: no I/O, no cache access. Callers pass the titles, the mapped Steam
AppIDs and the owned Steam apps. Measured on a 1,293-row library with 613
owned Steam apps (2026-10-03): 50 version groups, all correct by hand
review, and a full build under 100 ms.
"""
from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from functools import lru_cache

from unifideck.utils.title_match import (
    EDITION_SUFFIXES,
    TITLE_WORD_SUFFIXES,
    _strip_publisher_prefix,
    fold_roman_numerals,
    normalize_for_match,
    strip_celebration_suffix,
    version_tokens,
)

# Words that end real titles ("Creative Console", "Cyber Revolution") or
# name a different product ("X DLC") are never version words here.
_NOT_VERSION = TITLE_WORD_SUFFIXES | {"dlc"}

# Remaster and re-release words the edition table does not carry.
_EXTRA_VERSION = (
    "game of the year", "hd remastered", "hd remaster", "remaster", "redux",
    "re elected", "reforged", "reloaded",
)

# Longest first, so "xbox series xs edition" goes before "edition"-length
# entries and a compound suffix peels one layer per pass.
_VERSION_SUFFIXES: tuple[str, ...] = tuple(sorted(
    {s for s in (*EDITION_SUFFIXES, *_EXTRA_VERSION) if s not in _NOT_VERSION},
    key=len, reverse=True,
))

_BRACKET = re.compile(r"\s*[\(\[]([^\)\]]*)[\)\]]")
_DISAMBIGUATOR = re.compile(r"\b(?:19|20)\d{2}\b|\bclassic\b|\boriginal\b", re.I)

# "<words> Edition" may drop this many qualifier words ("Spacer's Choice").
_MAX_QUALIFIER_TOKENS = 3


@dataclass(frozen=True)
class SteamApp:
    """One owned Steam game."""

    appid: int
    title: str
    installed: bool = False


@dataclass(frozen=True)
class WorkKey:
    """A title reduced to the game it names.

    Attributes:
        key: the title with publisher prefix, platform and version words
            removed; ``""`` for a title with no usable words.
        marked: a version word was removed, so this row may legitimately be
            a different Steam app than the base game.
        edition_word: a bare trailing "edition" was removed, so the words
            before it may be an edition qualifier ("Gourmet Edition").
    """

    key: str
    marked: bool
    edition_word: bool


@lru_cache(maxsize=8192)
def work_key(title: str) -> WorkKey:
    """Reduce *title* to its :class:`WorkKey`."""
    raw = _BRACKET.sub(
        lambda m: " " if _DISAMBIGUATOR.search(m.group(1)) else m.group(0), title,
    )
    normalized = normalize_for_match(raw)
    if not normalized:
        return WorkKey("", marked=False, edition_word=False)
    key = fold_roman_numerals(_strip_publisher_prefix(normalized) or normalized)
    marked = edition_word = False
    while True:
        stripped, bare_edition = _strip_one_version_layer(key)
        if stripped == key:
            return WorkKey(key, marked, edition_word)
        key, marked = stripped, True
        edition_word = edition_word or bare_edition


def _strip_one_version_layer(key: str) -> tuple[str, bool]:
    """Remove one trailing version word: ``(new key, was bare "edition")``."""
    for suffix in _VERSION_SUFFIXES:
        if key.endswith(" " + suffix):
            return key[: -(len(suffix) + 1)].strip(), False
    celebration = strip_celebration_suffix(key)
    if celebration and celebration != key:
        return celebration, False
    if key.endswith(" edition") and len(key.split()) > 1:
        return key[: -len(" edition")].strip(), True
    return key, False


@dataclass
class _Node:
    title: str
    appid: int | None
    steam: SteamApp | None  # set for an owned Steam app, None for a row
    key: str = ""
    marked: bool = False


@dataclass
class Identity:
    """The grouping of one library plus the user's owned Steam apps.

    Library rows are addressed by their index in the ``titles`` passed to
    :func:`build_identity`.
    """

    group_of: list[str]
    """Group id per library row. Stable: derived from the titles, never
    from input order."""
    steam_for: list[SteamApp | None]
    """The owned Steam app that is this row's game, if any. When several
    owned versions qualify (BioShock and BioShock Remastered), each row
    gets its own: identical title first, then the same mapped app."""
    rows_in_group: dict[str, list[int]] = field(default_factory=dict)
    groups_of_steam_app: dict[int, set[str]] = field(default_factory=dict)
    steam_in_group: dict[str, list[SteamApp]] = field(default_factory=dict)

    def steam_versions(self, group: str) -> list[SteamApp]:
        """Every owned Steam app in *group*, lowest AppID first."""
        return sorted(self.steam_in_group.get(group, ()), key=lambda app: app.appid)

    def library_count(self, group: str) -> int:
        """How many library rows share *group*."""
        return len(self.rows_in_group.get(group, ()))

    def rows_for_steam_app(self, appid: int) -> list[int]:
        """Every library row that is the same game as Steam app *appid*."""
        rows: set[int] = set()
        for group in self.groups_of_steam_app.get(appid, ()):
            rows.update(self.rows_in_group.get(group, ()))
        return sorted(rows)


def build_identity(
    titles: Sequence[str],
    appids: Sequence[int | None],
    owned: Iterable[SteamApp] = (),
) -> Identity:
    """Group library rows and owned Steam apps into games.

    Args:
        titles: one title per library row.
        appids: the row's mapped real Steam AppID (``steam_real_appid``),
            ``None`` or ``0`` when it has none. Same length as *titles*.
        owned: the user's owned Steam games.
    """
    rows = [_Node(t, a if a and a > 0 else None, None) for t, a in zip(titles, appids, strict=True)]
    steam_nodes = [_Node(s.title, s.appid, s) for s in _unique_owned(owned)]
    nodes = rows + steam_nodes
    _trust_identical_titles(rows, steam_nodes)
    _assign_keys(nodes)
    group = _group_nodes(nodes)

    identity = Identity(group_of=group[: len(rows)], steam_for=[None] * len(rows))
    for index, gid in enumerate(identity.group_of):
        identity.rows_in_group.setdefault(gid, []).append(index)
    for node, gid in zip(nodes, group, strict=True):
        if node.appid:
            identity.groups_of_steam_app.setdefault(node.appid, set()).add(gid)
    owned_in_group: dict[str, list[_Node]] = defaultdict(list)
    for node, gid in zip(steam_nodes, group[len(rows):], strict=True):
        owned_in_group[gid].append(node)
        if node.steam is not None:
            identity.steam_in_group.setdefault(gid, []).append(node.steam)
    mapped_in_group: dict[str, set[int]] = defaultdict(set)
    for row, gid in zip(rows, identity.group_of, strict=True):
        if row.appid:
            mapped_in_group[gid].add(row.appid)
    for index, row in enumerate(rows):
        gid = identity.group_of[index]
        if candidates := owned_in_group.get(gid):
            identity.steam_for[index] = _pick_owned(row, candidates, mapped_in_group[gid])
    return identity


def _unique_owned(owned: Iterable[SteamApp]) -> list[SteamApp]:
    seen: set[int] = set()
    unique: list[SteamApp] = []
    for app in owned:
        if app.appid > 0 and app.appid not in seen:
            seen.add(app.appid)
            unique.append(app)
    return unique


def _trust_identical_titles(rows: list[_Node], steam_nodes: list[_Node]) -> None:
    """A row titled exactly like an owned Steam game takes its AppID."""
    by_title: dict[str, int] = {}
    for node in steam_nodes:
        if node.appid:
            by_title.setdefault(normalize_for_match(node.title), node.appid)
    for row in rows:
        exact = by_title.get(normalize_for_match(row.title))
        if exact:
            row.appid = exact


def _assign_keys(nodes: list[_Node]) -> None:
    """Work keys, then fold "<qualifier> Edition" onto a key that exists."""
    keys: list[WorkKey] = [work_key(n.title) for n in nodes]
    known = {k.key for k in keys if k.key}
    for node, wk in zip(nodes, keys, strict=True):
        node.key, node.marked = wk.key, wk.marked
        if wk.edition_word:
            node.key = _fold_qualifier(wk.key, known)


def _fold_qualifier(key: str, known: set[str]) -> str:
    """"hollow knight voidheart" → "hollow knight" when that game exists."""
    tokens = key.split()
    for drop in range(1, _MAX_QUALIFIER_TOKENS + 1):
        if len(tokens) - drop < 1:
            break
        shorter = " ".join(tokens[:-drop])
        if shorter in known and version_tokens(shorter) == version_tokens(key):
            return shorter
    return key


def _group_nodes(nodes: list[_Node]) -> list[str]:
    """Group id per node: buckets by key, split, then joined by AppID."""
    buckets: dict[str, list[int]] = defaultdict(list)
    for index, node in enumerate(nodes):
        buckets[node.key or f"#{normalize_for_match(node.title)}"].append(index)
    cluster = [""] * len(nodes)
    for key, members in buckets.items():
        unmarked = {nodes[i].appid for i in members if nodes[i].appid and not nodes[i].marked}
        split = len(unmarked) > 1
        for i in members:
            cluster[i] = _cluster_id(key, nodes[i], split=split)
    return _join_by_appid(nodes, cluster)


def _cluster_id(key: str, node: _Node, *, split: bool) -> str:
    if not split:
        return key
    if node.appid:
        return f"{key}#{node.appid}"
    return f"{key}#?{normalize_for_match(node.title)}"


def _join_by_appid(nodes: list[_Node], cluster: list[str]) -> list[str]:
    """Union clusters that share a Steam AppID; the smallest id names it."""
    parent = {c: c for c in cluster}

    def find(c: str) -> str:
        while parent[c] != c:
            parent[c] = parent[parent[c]]
            c = parent[c]
        return c

    first_for_appid: dict[int, str] = {}
    for node, c in zip(nodes, cluster, strict=True):
        if not node.appid:
            continue
        a, b = find(c), find(first_for_appid.setdefault(node.appid, c))
        if a != b:
            parent[max(a, b)] = min(a, b)
    return [find(c) for c in cluster]


def _pick_owned(row: _Node, candidates: list[_Node], group_appids: set[int]) -> SteamApp | None:
    """The owned Steam app that is *row*'s own version."""
    own_title = normalize_for_match(row.title)
    best = min(candidates, key=lambda s: (
        normalize_for_match(s.title) != own_title,
        s.appid != row.appid,
        s.appid not in group_appids,
        not (s.steam and s.steam.installed),
        -(s.appid or 0),
    ))
    return best.steam
