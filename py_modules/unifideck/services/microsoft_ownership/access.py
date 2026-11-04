"""What each Microsoft row lets the user do now: the ribbon's access gate.

The Steam Store ribbon must not call a game installable or streamable on
the strength of a row alone. Rows outlive the facts behind them: a failed
sync keeps the last xCloud rows, and a failed Xodus read serves the last
good PC titles. So every claim is checked against evidence that expires on
its own:

- **Owned on PC / installable through Game Pass.** The row's Xbox PC
  entitlement (``metadata["xbox_pc"]``) must be ``owned`` or ``gamepass``,
  installable (``ready`` or ``choice``), and its ``end_date`` must be in the
  future. A Game Pass entitlement ends with the subscription period, so a
  lapsed plan stops counting by itself. EA Play, Ubisoft+ and unconfirmed
  installs never qualify.
- **Streamable.** The row is an xCloud row from a ``/v2/titles`` read at
  most :data:`STREAM_EVIDENCE_MAX_AGE` old, and not out of time-capped
  play. A successful read lists only what the account may stream at that
  moment (a lapsed plan's Game Pass titles drop out), and a failed read
  keeps the old rows with their old time, so the age bounds a stale claim.
  A Game Pass row whose own Game Pass PC entitlement has ended does not
  stream either: that end date is the plan's.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from unifideck.core.cross_store_ownership import GAME_PASS_KEY, RowAccess
from unifideck.core.types import Game, GameTag
from unifideck.stores.microsoft.microsoft_catalog import (
    XCLOUD_CHECKED_AT_KEY,
    XCLOUD_PLAYABLE_KEY,
)
from unifideck.stores.microsoft.pc_library.entitlements import parse_rfc3339
from unifideck.stores.microsoft.pc_library.rows import META_KEY, row_product_ids

#: How old the last ``/v2/titles`` answer for a row may be before the ribbon
#: stops calling it streamable. A sync runs at least daily on a used Deck.
STREAM_EVIDENCE_MAX_AGE = 7 * 24 * 3600
#: Clock skew tolerated on ``xcloud_checked_at`` (a time slightly ahead).
_SKEW_S = 300.0
_INSTALLABLE_STATES = frozenset({"ready", "choice"})


def xbox_access(games: Iterable[Game], now: float) -> dict[str, RowAccess]:
    """:class:`RowAccess` for every Microsoft row, by UPPER-case ``store_game_id``.

    Args:
        games: the unified library.
        now: epoch seconds to judge end dates and evidence age against.
    """
    return {
        game.store_game_id.upper(): row_access(game, now)
        for game in games
        if game.store == "microsoft" and game.store_game_id
    }


def row_access(game: Game, now: float) -> RowAccess:
    """What *game* (a Microsoft row) lets the user do at *now*."""
    pc_access, pc_live = _pc_entitlement(game, now)
    return RowAccess(
        owned_pc=pc_access == "owned" and pc_live,
        pass_pc=pc_access == "gamepass" and pc_live,
        streams=_streams(game, now, pass_lapsed=pc_access == "gamepass" and not pc_live),
        product_ids=frozenset(row_product_ids(game)),
    )


def _pc_entitlement(game: Game, now: float) -> tuple[str | None, bool]:
    """``(access, live)`` of the row's Xbox PC entitlement.

    ``access`` is ``owned`` or ``gamepass`` (else None); ``live`` is True
    when it is installable and its end date is still ahead. An unreadable
    end date is not proof, so it reads as ended.
    """
    meta = game.metadata.get(META_KEY) if GameTag.XBOX_PC in game.tags else None
    if not isinstance(meta, dict):
        return None, False
    access = meta.get("access")
    if access not in ("owned", "gamepass"):
        return None, False
    end = parse_rfc3339(meta.get("end_date") or "")
    live = meta.get("state") in _INSTALLABLE_STATES and end is not None and end > now
    return access, live


def _streams(game: Game, now: float, *, pass_lapsed: bool) -> bool:
    """Whether the row streams now (see the module docstring)."""
    if GameTag.XCLOUD not in game.tags:
        return False
    metadata: dict[str, Any] = game.metadata or {}
    checked = metadata.get(XCLOUD_CHECKED_AT_KEY)
    if isinstance(checked, bool) or not isinstance(checked, (int, float)):
        return False
    if not -_SKEW_S <= now - checked <= STREAM_EVIDENCE_MAX_AGE:
        return False
    if metadata.get(XCLOUD_PLAYABLE_KEY) is False:
        return False
    if metadata.get(GAME_PASS_KEY) is False:
        return True
    return not pass_lapsed
