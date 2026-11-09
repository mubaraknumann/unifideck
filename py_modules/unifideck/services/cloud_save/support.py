"""Does a given store's copy of a game support cloud saves?

One answer, two consumers: the cloud-save button's ``cloud_supported`` field
(installed games) and the App-Details "Cloud saves" line (which must work
BEFORE a game is installed, so the user can pick the storefront whose copy
actually syncs).

Source precedence — most authoritative first:

1. **The store itself.** Epic ships ``customAttributes.CloudSaveFolder`` in the
   per-game metadata legendary already caches on disk for every OWNED game, so
   this is definitive, needs no install, and costs one local file read.
2. **The unifiDB catalog.** A ``{store: bool}`` map derived from
   PCGamingWiki/Ludusavi. Broad but incomplete and community-maintained — it
   records only POSITIVE support, so a store missing from a populated map is
   "not recorded", not "no".
3. **Unknown** (``None``).

GOG has an equivalent authoritative signal (the Galaxy ``clientId`` from the
build manifest plus the ``cloudStorage`` block in remote-config), but unlike
Epic's it costs two network round-trips per game and cannot be answered from
local state — so it is deliberately NOT done here on the metadata path. See
``gog_cloud_api`` for that machinery.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from unifideck.launcher.xbox_pc.layout import is_xodus_store

logger = logging.getLogger(__name__)

_LEGENDARY_METADATA_DIR = "~/.config/legendary/metadata"


def epic_cloud_support(game_id: str) -> bool | None:
    """Epic's own answer for ``game_id``, or ``None`` if it has no metadata.

    ``CloudSaveFolder`` is the Epic-side declaration of a cloud-save path;
    present ⇒ the title syncs saves, absent ⇒ it does not. Legendary caches
    this for every owned game at login/sync, so the file exists long before
    the game is installed.

    ``None`` (rather than ``False``) when there is no metadata file at all —
    that means "we have not synced Epic", not "no cloud saves".
    """
    path = Path(_LEGENDARY_METADATA_DIR).expanduser() / f"{game_id}.json"
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    attrs = payload.get("metadata", {})
    if not isinstance(attrs, dict):
        return None
    custom = attrs.get("customAttributes")
    if not isinstance(custom, dict):
        return False
    folder = custom.get("CloudSaveFolder")
    # Present-but-empty counts as absent: a blank path syncs nothing.
    if isinstance(folder, dict):
        return bool(folder.get("value"))
    return bool(folder)


def xbox_pc_cloud_support(game_id: str) -> bool | None:
    """True when an installed Xbox PC game has save containers.

    The runtime syncs them, and a runtime without cloud saves refuses to launch.
    None otherwise: no containers says nothing about the game.
    """
    from unifideck.stores.microsoft.xodus.ids import record_for_game
    from unifideck.stores.microsoft.xodus.save_status import has_local_saves

    record = record_for_game(game_id)
    if record is not None and has_local_saves(str(record["productId"])):
        return True
    return None


def catalog_cloud_support(
    enriched: dict[str, Any] | None, store: str,
) -> bool | None:
    """The unifiDB catalog's answer, or ``None`` when it has no opinion."""
    if not isinstance(enriched, dict):
        return None
    cloud = enriched.get("cloud")
    if not isinstance(cloud, dict) or store not in cloud:
        return None
    return bool(cloud[store])


def resolve_cloud_support(
    store: str, game_id: str, enriched: dict[str, Any] | None,
) -> bool | None:
    """Best available answer for ``store``'s copy of ``game_id``.

    Store-authoritative data wins over the catalog: the catalog is a
    third-party aggregation and is demonstrably wrong in both directions for
    individual titles, while Epic's own metadata is the thing the store
    actually acts on.
    """
    if store == "epic":
        native = epic_cloud_support(game_id)
        if native is not None:
            return native
    if is_xodus_store(store):
        native = xbox_pc_cloud_support(game_id)
        if native is not None:
            return native
    return catalog_cloud_support(enriched, store)
