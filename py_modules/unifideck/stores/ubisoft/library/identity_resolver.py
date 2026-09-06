"""
Canonical identity grouping and DLC/edition dedup.

Split out of ``game_builder.py`` (was pushing it over the volumetry file
cap). Groups cleaned UPC entries into canonical ``(base_game,
edition_tag)`` identities so a game reported under multiple space_ids
(or as "X" and "X Gold Edition") surfaces once, and separates true DLC
rows (dash/colon-suffixed names of an owned base game) from standalone
games that merely share the naming convention (Rainbow Six: Siege,
Watch Dogs: Legion) via catalog-gated matching.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from unifideck.stores.ubisoft.parser import GameConfig

    from .game_builder import _GameBuilder

logger = logging.getLogger(__name__)

# Trailing edition qualifier ("Assassin's Creed Valhalla Gold Edition",
# "Anno 1602 - History Edition"). Used to (a) recognise an entry as an
# *edition of a base game* — a real game, never DLC — and (b) derive the
# base title + edition tag for identity dedup. The leading ``\s+`` matches
# the space in both " Gold Edition" and " - History Edition" forms.
_EDITION_SUFFIX_PATTERN = re.compile(
    r"\s+(gold|complete|ultimate|deluxe|premium|special|"
    r"collector'?s?|limited|digital|standard|history|definitive|"
    r"remastered?|anniversary|goty|game of the year|enhanced|"
    r"legendary)\s*(edition)?$",
    re.IGNORECASE,
)
# Edition keywords that denote the *base* SKU (no distinct edition), so an
# owned "X Standard Edition" dedups together with plain "X".
_BASE_EDITION_WORDS = frozenset({"standard"})
# Minimum parent length before the substring fallback in
# :meth:`_IdentityResolver._parent_matches` is allowed to fire — short
# prefixes ("the", "tom") match far too eagerly.
_MIN_SUBSTRING_PARENT_LEN = 5

class _IdentityResolver:
    """Canonical identity grouping and DLC/edition classification."""

    def __init__(self, parent: _GameBuilder) -> None:
        """Initialize the instance."""
        self._parent = parent

    def group_by_identity(
        self,
        cleaned: list[tuple[GameConfig, str, bool]],
        db_names: set[str],
        base_catalog_norms: set[str],
    ) -> tuple[
        dict[tuple[str, str], list[tuple[GameConfig, str]]],
        list[tuple[str, str]],
    ]:
        """Pass 2: drop separator-DLC, then group by canonical identity.

        Returns ``(groups, order)`` — ``groups`` maps each canonical
        ``(base_game, edition_tag)`` key to its member ``(cfg, title)`` pairs,
        and ``order`` preserves first-seen insertion for a stable display.
        """
        # Base titles = edition-stripped, normalised names of every kept
        # entry. Used to recognise an entry as a DLC of a game we surface.
        base_norms = {
            self._parent._id_map.normalize_for_matching(self._strip_edition(title))
            for _, title, _ in cleaned
        }
        groups: dict[tuple[str, str], list[tuple[GameConfig, str]]] = {}
        order: list[tuple[str, str]] = []
        for cfg, title, known in cleaned:
            if not known and self._is_dlc_by_separator(
                title, base_norms, db_names, base_catalog_norms,
            ):
                logger.debug(
                    "[UbisoftLibrary] dedup skip (DLC of base): %s",
                    title,
                )
                continue
            key = self._canonical_key(title, base_catalog_norms)
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append((cfg, title))
        return groups, order

    def is_known_base_game(
        self, title: str, base_catalog_norms: set[str],
    ) -> bool:
        """True if ``title`` (edition-stripped) is a known catalog base game.

        Exact normalised match only — substring would let DLC whose name
        starts with a base title ("X - Some Expansion") masquerade as a
        game. Such entries fall through to the DLC heuristics instead.
        """
        if not base_catalog_norms:
            return False
        norm = self._parent._id_map.normalize_for_matching(
            self._strip_edition(title),
        )
        return bool(norm) and norm in base_catalog_norms

    def _canonical_key(
        self, title: str, base_catalog_norms: set[str],
    ) -> tuple[str, str]:
        """Canonical identity ``(base_game, edition_tag)`` for dedup."""
        base_norm = self._parent._id_map.normalize_for_matching(
            self._strip_edition(title),
        )
        canonical = self._resolve_canonical_base(base_norm, base_catalog_norms)
        return (canonical, self._edition_tag(title))

    @staticmethod
    def _edition_tag(title: str) -> str:
        """The edition qualifier ("gold", "history", …) or "" for the base.

        ``Standard`` maps to the base tag so "X Standard Edition" dedups
        with plain "X".
        """
        match = _EDITION_SUFFIX_PATTERN.search(title)
        if not match:
            return ""
        word = " ".join(match.group(1).lower().split())
        return "" if word in _BASE_EDITION_WORDS else word

    @staticmethod
    def _resolve_canonical_base(
        base_norm: str, base_catalog_norms: set[str],
    ) -> str:
        """Map an owned base title to its catalog identity when possible.

        Exact match wins. Otherwise bridge a *prepended* publisher/brand
        prefix ("Tom Clancy's The Division 2" → "The Division 2") by
        requiring the catalog title to be a whole-word **suffix** of the
        owned name. Suffix-only is deliberate: it bridges prefixes
        without collapsing sequels — "Assassin's Creed II" must NOT fold
        into "Assassin's Creed" (the extra token is a suffix, not a
        prefix). Falls back to ``base_norm`` when nothing matches.
        """
        if not base_norm or base_norm in base_catalog_norms:
            return base_norm
        best = ""
        for cat in base_catalog_norms:
            if (
                len(cat) > _MIN_SUBSTRING_PARENT_LEN
                and base_norm.endswith(" " + cat)
                and len(cat) > len(best)
            ):
                best = cat
        return best or base_norm

    @staticmethod
    def select_group_winner(
        members: list[tuple[GameConfig, str]],
        connect_ids: dict[str, str],
    ) -> tuple[GameConfig, str]:
        """Pick the surviving ``(cfg, title)`` for a canonical group.

        The winning ``cfg`` decides ``store_game_id`` (and thus the
        shortcut's stable ``LaunchOptions``); deterministic selection
        kills the cross-sync id flip that stranded orphan duplicate
        shortcuts. Priority: a ``space_id`` with a leveldb connect id
        (best ``uplay://`` launch) > any ``space_id`` > lowest numeric
        ``install_id``. The display title is the shortest member title
        (the plain base form beats wordier variants).
        """
        def rank(item: tuple[GameConfig, str]) -> tuple[int, int, str]:
            cfg, title = item
            space = cfg.space_id or ""
            if space and connect_ids.get(space):
                tier = 0
            elif space:
                tier = 1
            else:
                tier = 2
            return (tier, cfg.install_id or 0, title)

        winner_cfg = min(members, key=rank)[0]
        display_title = min(
            (t for _, t in members), key=lambda t: (len(t), t),
        )
        return winner_cfg, display_title

    @staticmethod
    def _strip_edition(title: str) -> str:
        """Strip a trailing edition qualifier (``Gold Edition`` …)."""
        match = _EDITION_SUFFIX_PATTERN.search(title)
        return title[: match.start()].strip() if match else title

    def _is_dlc_by_separator(
        self,
        title: str,
        base_norms: set[str],
        db_names: set[str],
        base_catalog_norms: set[str],
    ) -> bool:
        """True if ``title`` is a named DLC/expansion of a base we keep.

        Two separators drive parent detection, with very different
        safety profiles:

        * ``" - "`` (``"Base - Expansion Name"``) — the part before the
          dash must match an owned base title or a community-DB title.
          This dash form is DLC-specific enough to trust broadly.

        * ``": "`` (``"Base: Subtitle"``) — used **only** under strict
          catalog gating (below). Ubisoft ships a great many *standalone*
          games as ``"Franchise: Subtitle"`` (Rainbow Six: Siege, Ghost
          Recon: Wildlands, Watch Dogs: Legion, Splinter Cell:
          Blacklist), so a bare colon parent-match would delete real
          owned games. The gate exploits the fact that the Algolia base
          catalog is base-games-only: a standalone subtitled game is
          *itself* a catalog entry, whereas a DLC (Trials Fusion: Riders
          of the Rustlands) is not — so we only drop a colon title whose
          full name is absent from the catalog while its base is present.

        An edition variant ("Base - History Edition", "Base - Gold
        Edition") is a real game, not DLC — the separator here joins the
        base to an edition qualifier, not to an add-on name. We bail out
        before any parent check so editions are kept (this is what made
        "Anno 1602 - History Edition" vanish).
        """
        if _EDITION_SUFFIX_PATTERN.search(title):
            return False
        self_norm = self._parent._id_map.normalize_for_matching(
            self._strip_edition(title),
        )
        if " - " in title:
            parent = self._parent._id_map.normalize_for_matching(
                title.split(" - ", 1)[0],
            )
            if self._parent_matches(
                parent,
                base_norms | db_names,
                base_norms,
                exclude=self_norm,
            ):
                return True
        if ": " in title:
            return self._is_colon_dlc(
                title, self_norm, base_norms, base_catalog_norms,
            )
        return False

    def _is_colon_dlc(
        self,
        title: str,
        self_norm: str,
        base_norms: set[str],
        base_catalog_norms: set[str],
    ) -> bool:
        """Catalog-gated ``"Base: Subtitle"`` DLC test.

        Drops the entry only when *all* hold: the pre-colon base is a
        known Algolia base game (``base_catalog_norms``), that base is
        *separately owned* (``base_norms``), and the full title is **not
        itself** a catalog base game. The last clause is the discriminator
        that keeps standalone subtitled games — "Prince of Persia: The
        Sands of Time" and "Watch Dogs: Legion" are catalog entries in
        their own right; "Trials Fusion: Riders of the Rustlands" is not.
        """
        parent = self._parent._id_map.normalize_for_matching(
            title.split(": ", 1)[0],
        )
        return (
            bool(parent)
            and parent != self_norm
            and parent in base_catalog_norms
            and parent in base_norms
            and self_norm not in base_catalog_norms
        )

    @staticmethod
    def _parent_matches(
        parent: str,
        exact_set: set[str],
        substring_set: set[str],
        *,
        exclude: str = "",
    ) -> bool:
        """Exact membership first, then a length-guarded substring match.

        ``parent`` here is a candidate title's normalised base string (not
        to be confused with ``self._parent``, this class's back-reference
        to the owning ``_GameBuilder`` — this is a staticmethod, so there
        is no ``self`` in scope and no ambiguity at runtime).

        ``exclude`` is the candidate's own normalised title — it is
        skipped so the substring fallback never matches an entry against
        itself (the parent is always a prefix of its own full title).
        """
        if not parent:
            return False
        if parent in exact_set and parent != exclude:
            return True
        if len(parent) > _MIN_SUBSTRING_PARENT_LEN:
            return any(
                (parent in known or known in parent) and known != exclude
                for known in substring_set
            )
        return False
