"""Per-store capability sets — the single source of truth.

Before this module, every per-store capability was a hand-written list in
two languages with nothing linking them. The 2026-08 audit found **sixteen**
such lists in ``src/`` alone, of which exactly one pair was machine-checked
(``CLIENT_STOREFRONTS`` ↔ ``WRAPPER_STORES``, check 9). The failure mode is
quiet in both directions: a store added to the backend only shows no button,
and one added to the frontend only shows a button that raises.

Two of those lists were duplicated inside TypeScript as well —
``CLOUD_SAVE_STORES`` existed in both ``useCloudSaveStatus.ts`` and
``PlayMeta.tsx``, the second with a comment admitting it mirrored the first.

**Why this lives in ``core/``.** The consumers are spread across layers that
cannot import each other. ``rpc/`` is a leaf (``.importlinter``: nothing
inside ``unifideck.*`` may import it), so a set defined in an RPC mixin was
unreachable from ``stores/``; and ``stores/`` sits *below* ``services/``, so
``store_registry`` cannot read ``CloudSaveService``. ``core/`` is the one
place all three can reach. No imports here on purpose — pure data.

**Why not on ``StoreInfo``.** The same reason ``uses_wine`` was removed from
it in audit §3.1: a per-store literal is a second copy that drifts, and the
gate that checks it becomes the only thing keeping it alive. These sets are
injected into the ``get_store_infos`` payload instead, alongside ``available``
and ``client_runs_in_prefix``, so a store cannot declare an answer that
disagrees with the code that implements it.

``supports_cloud_saves`` proved that concretely. It *was* a ``StoreInfo``
field, and only Battle.net ever declared it — as ``False``. Every other store
took the dataclass default, also ``False``, so **the two stores that actually
have cloud saves both advertised that they did not**. Nothing read it, which
is the only reason it never broke anything: wiring the frontend to it would
have hidden cloud saves for GOG and Epic.
"""
from __future__ import annotations

#: Stores with a ``get_game_achievements`` implementation. Display only —
#: unlocking is each store's own concern (GOG via Comet, Epic via the EOS
#: overlay). Consumed by ``rpc/mixins/achievements.py`` and, through the
#: payload, by the App-Details achievements button.
ACHIEVEMENT_STORES = frozenset({"gog", "epic"})

#: Stores with a ``CloudSaveStrategy``. Must equal the keys of
#: ``CloudSaveService._strategies`` — pinned by
#: ``tests/unit/test_store_capabilities.py``, because this set describing a
#: strategy that does not exist is worse than no set at all.
CLOUD_SAVE_STORES = frozenset({"gog", "epic"})

#: Stores that expose a per-game language list at install time, i.e. those
#: with a ``get_<store>_game_languages`` RPC.
LANGUAGE_PICKER_STORES = frozenset({"gog", "epic"})

#: Stores whose storefront opens in the embedded browser rather than in a
#: vendor client running inside a Wine prefix. The complement of
#: ``WRAPPER_STORES`` among the stores that have a storefront at all; kept as
#: its own set because "has a browser storefront" and "is not a wrapper
#: store" are not the same claim and a future store could be neither.
BROWSER_STOREFRONT_STORES = frozenset({"epic", "gog", "amazon", "microsoft"})


def capability_flags(store: str) -> dict[str, bool]:
    """The capability booleans for *store*, for the store-info payload.

    Keys are stable wire names the frontend reads; adding one here is the
    whole change on the backend side. Returns ``False`` for every capability
    of an unknown store rather than raising — this feeds a UI payload, and a
    store the sets have not heard of should render without buttons, not fail
    the whole request.
    """
    return {
        "supports_achievements": store in ACHIEVEMENT_STORES,
        "supports_cloud_saves": store in CLOUD_SAVE_STORES,
        "has_language_picker": store in LANGUAGE_PICKER_STORES,
        "has_browser_storefront": store in BROWSER_STOREFRONT_STORES,
    }
