"""Unit tests for the Ubisoft phantom-game guards.

Two layers protect against unlinked-account games appearing as
"installed" (the beta-tester bug where Assassin's Creed IV, Child of
Light, etc. showed up with no Ubisoft account signed in):

1. ``UbisoftStore.get_library`` refuses to enumerate when not signed in —
   ``[]`` when the auth prefix is gone, ``None`` when its vault is merely
   signed out (see the tests below for why those differ).
2. ``_GameBuilder.cross_reference_ownership`` no longer falls back to
   *all* local configs when the ownership binary is missing — it keeps
   only configs that are actually installed on disk.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from unifideck.stores.ubisoft.library.game_builder import _GameBuilder
from unifideck.stores.ubisoft.parser import GameConfig


def _cfg(install_id: int, space_id: str, name: str) -> GameConfig:
    c = GameConfig()
    c.install_id = install_id
    c.launch_id = install_id
    c.space_id = space_id
    c.name = name
    return c


# ── cross_reference_ownership fallback ────────────────────────────


def test_owned_set_present_trusts_ownership():
    """With an ownership binary, only owned ids survive."""
    owned = _cfg(1, "space-1", "Owned Game")
    unowned = _cfg(2, "space-2", "Unowned Game")
    configs = [owned, unowned]
    by_id = _GameBuilder.build_config_lookup(configs)

    result = _GameBuilder.cross_reference_ownership(
        configs, by_id, owned_set={1}, installed={},
    )

    assert result == [owned]


def test_no_ownership_keeps_only_installed():
    """Missing ownership binary → only installed configs survive.

    This is the phantom-game fix: pre-change this returned *every*
    config, inventing owned games for unlinked accounts.
    """
    installed_game = _cfg(1, "space-1", "Installed Game")
    not_installed = _cfg(2, "space-2", "Catalogue Only")
    configs = [installed_game, not_installed]
    by_id = _GameBuilder.build_config_lookup(configs)
    # install scan keyed by space_id (see _build_one_game)
    installed = {"space-1": {"install_path": "/games/ac4"}}

    result = _GameBuilder.cross_reference_ownership(
        configs, by_id, owned_set=None, installed=installed,
    )

    assert result == [installed_game]


def test_no_ownership_no_installs_returns_empty():
    """Missing ownership + nothing installed → no phantom games."""
    configs = [_cfg(1, "space-1", "A"), _cfg(2, "space-2", "B")]
    by_id = _GameBuilder.build_config_lookup(configs)

    result = _GameBuilder.cross_reference_ownership(
        configs, by_id, owned_set=None, installed={},
    )

    assert result == []


def test_no_ownership_matches_install_id_key():
    """A config with no space_id is keyed by its install id."""
    cfg = _cfg(42, "", "Spaceless Game")
    configs = [cfg]
    by_id = _GameBuilder.build_config_lookup(configs)

    result = _GameBuilder.cross_reference_ownership(
        configs, by_id, owned_set=None, installed={"42": {}},
    )

    assert result == [cfg]


# ── get_library auth gate ─────────────────────────────────────────


def _store_with_state(state: str):
    """A bare store whose auth facade reports ``state``."""
    from unifideck.stores.ubisoft import store as store_mod

    store = store_mod.UbisoftStore.__new__(store_mod.UbisoftStore)
    store._auth = MagicMock()
    store._auth.credential_state = MagicMock(return_value=state)
    store._library = AsyncMock()
    return store


@pytest.mark.asyncio
async def test_get_library_empty_when_never_signed_in():
    """No auth vault at all → [] without touching the facade.

    ``[]`` is authoritative: it lets the post-sync reconcile sweep the
    phantom rows a signed-out account leaves behind.
    """
    store = _store_with_state("absent")

    result = await store.get_library()

    assert result == []
    store._library.get_library.assert_not_called()


@pytest.mark.asyncio
async def test_get_library_unreadable_when_vault_signed_out():
    """A vault UPC signed itself out of → None, never [].

    Regression: the token died server-side, so we cannot enumerate the
    library — but the user still owns those games. ``[]`` here would make
    the store sweepable and delete every Ubisoft shortcut they have
    (``shortcut/events._sweepable_stores``); ``None`` becomes
    ``library_unreadable`` and their tiles survive.
    """
    store = _store_with_state("signed_out")

    result = await store.get_library()

    assert result is None
    store._library.get_library.assert_not_called()


@pytest.mark.asyncio
async def test_get_library_delegates_when_authenticated():
    """Authenticated store delegates to the library facade."""
    sentinel = ["game"]
    store = _store_with_state("signed_in")
    store._library.get_library = AsyncMock(return_value=sentinel)

    result = await store.get_library()

    assert result is sentinel
