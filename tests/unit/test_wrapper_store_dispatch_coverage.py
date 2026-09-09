"""Every hand-written per-store dispatch site covers every wrapper store.

``WRAPPER_STORES`` is the canonical set, but membership alone does nothing:
adding a store to it also requires a row in several per-store maps scattered
across the backend and the frontend. A missing row does not raise — it falls
through to the *non-wrapper* default, which is the worst kind of failure
because the store looks configured:

===============================================  ==============================
site                                             a missing row means
===============================================  ==============================
``wrapper_prefix_probe._SPECS``                  the probe never matches
                                                 (pinned in
                                                 ``test_dispatcher_wrapper_install_prefix``)
``services.prefix_bridge.resolve_prefix``        the generic prefix path, so
                                                 cloud saves, size and
                                                 forensics read an empty dir
``LauncherService._wrapper_handler``             ``None``: no client opens for
                                                 sign-in or install
``launcher.proton._STORE_LAUNCHERS``             ``generic_launch``, which
                                                 wants an exe the vendor
                                                 client owns
``StorefrontLauncher.CLIENT_STOREFRONTS``        the cart button silently does
                                                 nothing (TypeScript — checked
                                                 by ``validate_architecture``
                                                 check 9, not reachable here)
===============================================  ==============================

Audit §3.1 recorded the wrapper/CLI distinction as two hand-maintained tables
with no link. Re-deriving it found the linked pair (``uses_wine`` against
``WRAPPER_STORES``) had no reader on either side, while these five — which
decide real behaviour — had exactly one pin between them. This file adds the
rest.

Each assertion is written against the *non-wrapper default* rather than
against a specific expected value: the bug being caught is "this store
silently took the ordinary path", and naming the default is what makes the
failure message say so.
"""

from __future__ import annotations

import pytest

from unifideck.core import compat_bridge
from unifideck.launcher.proton import _STORE_LAUNCHERS, generic_launch
from unifideck.launcher.wrapper_stores import WRAPPER_STORES
from unifideck.services.launcher.service import LauncherService
from unifideck.services.prefix_bridge import resolve_prefix

STORES = sorted(WRAPPER_STORES)


@pytest.mark.parametrize("store", STORES)
def test_prefix_bridge_resolves_a_store_specific_prefix(store: str) -> None:
    """A wrapper store's prefix is per-store, never the generic root.

    ``resolve_prefix`` ends in ``return compat_bridge.PREFIX_ROOT / game_id``
    — the CLI-store layout. A wrapper store landing there points every
    prefix-scoped service at a directory its games were never installed into.
    """
    generic = compat_bridge.PREFIX_ROOT / "probe-game"
    resolved = resolve_prefix(store, "probe-game")
    assert resolved != generic, (
        f"{store} has no branch in prefix_bridge.resolve_prefix and fell "
        f"through to the generic prefix {generic}"
    )
    assert store in resolved.parts


def test_a_non_wrapper_store_still_gets_the_generic_prefix() -> None:
    """The other direction, so the test above cannot pass by accident."""
    assert resolve_prefix("epic", "probe-game") == (
        compat_bridge.PREFIX_ROOT / "probe-game"
    )


@pytest.mark.parametrize("store", STORES)
@pytest.mark.parametrize("action", ["install", "auth", "storefront"])
def test_wrapper_handler_exists_for_every_action(store: str, action: str) -> None:
    """``_wrapper_handler`` returns a callable for all three non-launch runs.

    ``storefront`` is included on purpose: it is meant to fall into the
    ``auth`` arm (the vendor client's own Store tab is already signed in),
    so ``None`` there would be a real regression rather than a design choice.
    """
    handler = LauncherService._wrapper_handler(store, action)
    assert handler is not None, (
        f"_wrapper_handler({store!r}, {action!r}) returned None — the wrapper "
        f"client will not open for that action"
    )
    assert callable(handler)


def test_wrapper_handler_is_none_for_a_non_wrapper_store() -> None:
    assert LauncherService._wrapper_handler("epic", "install") is None


def test_every_wrapper_store_has_its_own_proton_launch_handler() -> None:
    """Containment, not equality: Epic has a handler and is not a wrapper.

    Falling through to ``generic_launch`` is correct for a CLI store and
    wrong for a wrapper store, whose executable is the vendor client.
    """
    missing = WRAPPER_STORES - set(_STORE_LAUNCHERS)
    assert not missing, (
        f"{sorted(missing)} would fall through to generic_launch"
    )
    for store in STORES:
        assert _STORE_LAUNCHERS[store] is not generic_launch
