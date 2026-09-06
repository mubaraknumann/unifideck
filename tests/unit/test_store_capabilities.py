"""Capability sets must match the code that implements them.

Audit register items 26 and 31. The 2026-08 review found **sixteen**
hand-maintained per-store lists in ``src/`` with exactly one machine-checked
pair between them (``CLIENT_STOREFRONTS`` ↔ ``WRAPPER_STORES``). A set is
only worth having if something proves it true, and §3.1's lesson is sharper
than that: a *checked* duplicate is not a *linked* one — the hard gate on
``uses_wine`` was worthless because the value it compared had no reader.

So these tests do not compare two lists. Each one pins a set against the
**implementation it describes**: the strategies actually registered, the
methods actually defined, the RPCs actually exposed.

The concrete failure this prevents is already on the record.
``supports_cloud_saves`` was a ``StoreInfo`` field that only Battle.net
declared, as ``False``. Every other store took the default — also ``False`` —
so GOG and Epic, the only two stores with cloud saves, advertised that they
had none. Nothing read it, so nothing broke; wiring a UI to it would have
hidden the feature on exactly the two stores that support it.
"""
from __future__ import annotations

import inspect

from unifideck.core.store_capabilities import (
    ACHIEVEMENT_STORES,
    BROWSER_STOREFRONT_STORES,
    CLOUD_SAVE_STORES,
    LANGUAGE_PICKER_STORES,
    capability_flags,
)

ALL_STORES = frozenset(
    {"epic", "gog", "amazon", "ubisoft", "battlenet", "microsoft"},
)


def test_every_capability_set_names_only_real_stores() -> None:
    """A typo'd store id silently disables a capability forever."""
    for name, members in (
        ("ACHIEVEMENT_STORES", ACHIEVEMENT_STORES),
        ("CLOUD_SAVE_STORES", CLOUD_SAVE_STORES),
        ("LANGUAGE_PICKER_STORES", LANGUAGE_PICKER_STORES),
        ("BROWSER_STOREFRONT_STORES", BROWSER_STOREFRONT_STORES),
    ):
        unknown = members - ALL_STORES
        assert unknown == set(), f"{name} names non-existent store(s): {unknown}"


def test_cloud_save_set_matches_the_registered_strategies() -> None:
    """The set must equal what ``CloudSaveService`` actually registers.

    Read out of the constructor's source rather than by building the service,
    which would need a config, a cache and a writable root. What matters is
    that the two cannot drift, not how the dict is obtained.
    """
    from unifideck.services.cloud_save import service as svc_mod

    src = inspect.getsource(svc_mod.CloudSaveService.__init__)
    registered = {
        store for store in ALL_STORES if f'"{store}":' in src
    }
    assert registered == set(CLOUD_SAVE_STORES), (
        f"CLOUD_SAVE_STORES={sorted(CLOUD_SAVE_STORES)} but "
        f"CloudSaveService registers {sorted(registered)}"
    )


def test_achievement_set_matches_the_stores_defining_the_method() -> None:
    """Every listed store must really implement ``get_game_achievements``.

    A store in the set without the method shows an achievements button that
    raises; a store with the method but not in the set shows no button. Both
    directions are silent, which is why this is asserted rather than trusted.
    """
    from unifideck.stores.shared.store_base import StoreBase

    base_impl = getattr(StoreBase, "get_game_achievements", None)
    implementers = set()
    for store in sorted(ALL_STORES):
        cls = _store_class(store)
        if cls is None:
            continue
        impl = getattr(cls, "get_game_achievements", None)
        if impl is not None and impl is not base_impl:
            implementers.add(store)
    assert implementers == set(ACHIEVEMENT_STORES), (
        f"ACHIEVEMENT_STORES={sorted(ACHIEVEMENT_STORES)} but the stores "
        f"defining get_game_achievements are {sorted(implementers)}"
    )


def test_language_picker_set_matches_the_exposed_rpcs() -> None:
    """One ``get_<store>_game_languages`` RPC per listed store."""
    from unifideck.rpc.mixins.download import DownloadRPCMixin

    exposed = {
        store
        for store in ALL_STORES
        if hasattr(DownloadRPCMixin, f"get_{store}_game_languages")
    }
    assert exposed == set(LANGUAGE_PICKER_STORES), (
        f"LANGUAGE_PICKER_STORES={sorted(LANGUAGE_PICKER_STORES)} but the "
        f"exposed RPCs cover {sorted(exposed)}"
    )


def test_browser_and_wrapper_storefronts_do_not_overlap() -> None:
    """A store's storefront is either in the browser or in a prefix."""
    from unifideck.launcher.wrapper_stores import WRAPPER_STORES

    overlap = BROWSER_STOREFRONT_STORES & set(WRAPPER_STORES)
    assert overlap == set(), (
        f"{sorted(overlap)} claim both a browser storefront and a client "
        f"that runs in a prefix"
    )


def test_the_achievements_mixin_reads_the_shared_set() -> None:
    """Not a second copy — register item 31 was exactly that."""
    from unifideck.rpc.mixins import achievements as mixin

    assert set(mixin._ACHIEVEMENT_STORES) == set(ACHIEVEMENT_STORES)


def test_capability_flags_covers_every_set() -> None:
    """A new set must reach the payload, or the frontend cannot read it."""
    flags = capability_flags("gog")
    assert flags == {
        "supports_achievements": True,
        "supports_cloud_saves": True,
        "has_language_picker": True,
        "has_browser_storefront": True,
    }


def test_capability_flags_are_all_false_for_an_unknown_store() -> None:
    """A payload must render without buttons, not fail the request."""
    flags = capability_flags("not_a_store")
    assert flags and not any(flags.values())


def test_a_wrapper_store_gets_no_browser_storefront_flag() -> None:
    assert capability_flags("ubisoft")["has_browser_storefront"] is False
    assert capability_flags("battlenet")["has_browser_storefront"] is False


def test_store_info_no_longer_declares_a_cloud_save_field() -> None:
    """A re-added literal must raise, not silently disagree again.

    This is the §3.1 pattern: the durable fix removes every copy but the one
    a machine checks, and here the copy became impossible instead.
    """
    import dataclasses

    from unifideck.core.types.domain import StoreInfo

    fields = {f.name for f in dataclasses.fields(StoreInfo)}
    assert "supports_cloud_saves" not in fields


def _store_class(store: str):
    """The ``*Store`` class for *store*, or ``None`` if not importable."""
    import importlib

    for module_suffix in (f"{store}.store", f"{store}.{store}_store"):
        try:
            mod = importlib.import_module(f"unifideck.stores.{module_suffix}")
        except ImportError:
            continue
        for attr in vars(mod).values():
            if (
                isinstance(attr, type)
                and attr.__name__.lower().endswith("store")
                and attr.__module__ == mod.__name__
            ):
                return attr
    return None
