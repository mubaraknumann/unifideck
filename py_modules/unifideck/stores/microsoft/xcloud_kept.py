"""stores/microsoft/xcloud_kept.py — the xCloud rows of the last sync.

When Cloud Gaming refuses the connection's location (``InvalidCountry``),
the account's cloud list cannot be read: it needs the token that the refused
login would have returned. The Xbox PC list still can. ``get_library`` must
return one list, and the reconcile deletes every Microsoft shortcut missing
from it, so the cloud rows the last sync knew are passed through again.

Rows come back in their xCloud form, without the Xbox PC tag and facts, so
``pc_library.compose`` merges today's PC list into them exactly as it merges a
fresh xCloud read.

Blocking (reads ``library_cache.json``): call it through ``asyncio.to_thread``.
"""
from __future__ import annotations

import dataclasses
import json
import logging
from typing import Any

from unifideck.core.cross_store_ownership import GAME_PASS_KEY
from unifideck.core.sync_cache_mixin import deserialize_libraries, library_cache_path
from unifideck.core.types import Game, GameTag

from .pc_library.rows import META_KEY

logger = logging.getLogger(__name__)

STORE = "microsoft"


def _cloud_form(game: Game) -> Game:
    metadata = {k: v for k, v in (game.metadata or {}).items() if k not in (META_KEY, GAME_PASS_KEY)}
    tags = [t for t in game.tags if t != GameTag.XBOX_PC]
    return dataclasses.replace(game, tags=tags, metadata=metadata)


def kept_xcloud_rows(config: Any) -> list[Game]:
    """The last synced Microsoft rows that stream, in xCloud form; ``[]`` if unreadable."""
    path = library_cache_path(config)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        libraries = data.get("libraries") if isinstance(data, dict) else None
        games = deserialize_libraries({STORE: (libraries or {}).get(STORE) or []}).get(STORE, [])
    except (OSError, ValueError, TypeError) as e:
        logger.warning(
            "[MicrosoftStore] could not read the last xCloud rows from %s (%s): "
            "only Xbox PC games are listed this sync", path, type(e).__name__,
        )
        return []
    return [_cloud_form(g) for g in games if GameTag.XCLOUD in g.tags]
