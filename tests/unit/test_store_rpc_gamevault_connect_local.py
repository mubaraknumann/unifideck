"""``StoreRPCMixin.connect_gamevault_local`` — the RPC the vault form calls.

Mirrors ``test_store_rpc_gamevault_connect.py``, and for the same reason: the
dead-RPC gate checks that a route *has* a caller, not that the caller passes
arguments the method accepts. The sibling route shipped broken once because
nothing called it the way the frontend does, so the first test here calls
this one exactly as ``connectGameVault`` does and would fail on any arity
change.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from unifideck.core.types import AuthResult
from unifideck.rpc.mixins.store import StoreRPCMixin


class _Plugin(StoreRPCMixin):
    """Minimal host: the mixin reaches its registry through ``self``."""

    def __init__(self, result: Any) -> None:
        self.registry = AsyncMock()
        self.registry.auth_action = AsyncMock(return_value=result)


@pytest.fixture
def plugin() -> _Plugin:
    return _Plugin(AuthResult(success=True, store="gamevault"))


async def test_accepts_the_one_argument_the_modal_sends(plugin: _Plugin) -> None:
    """Positional, as the modal sends it. This is the regression guard."""
    result = await plugin.connect_gamevault_local(
        "/home/deck/Games/UnifideckVault",
    )

    assert result.success is True
    plugin.registry.auth_action.assert_awaited_once_with(
        "gamevault",
        "start",
        mode="local",
        vault_dir="/home/deck/Games/UnifideckVault",
    )


async def test_the_argument_is_optional(plugin: _Plugin) -> None:
    """An untouched form is a complete action: the backend picks the default.

    The store resolves an empty value to its configured ``default_vault_dir``
    and creates the folder, which is what "create the vault for me" means
    from the user's side.
    """
    await plugin.connect_gamevault_local()

    plugin.registry.auth_action.assert_awaited_once_with(
        "gamevault", "start", mode="local", vault_dir="",
    )


async def test_there_is_no_install_location_argument() -> None:
    """The redundancy this route exists without.

    Where a game installs is asked per install by the shared storage picker
    (``useInstallFlow`` → ``pickStorageForInstall`` → ``install_game``'s
    ``install_path``), which already handles SD cards and USB drives for all
    seven stores. A GameVault-only copy of that setting would be a second
    answer to a question already answered.
    """
    import inspect

    params = inspect.signature(
        StoreRPCMixin.connect_gamevault_local,
    ).parameters
    assert list(params) == ["self", "vault_dir"]


async def test_it_always_asks_for_local_mode(plugin: _Plugin) -> None:
    """The mode is the route's whole purpose; it is never caller-supplied."""
    await plugin.connect_gamevault_local("/vault")

    _, kwargs = plugin.registry.auth_action.call_args
    assert kwargs["mode"] == "local"


async def test_an_error_is_propagated_unchanged() -> None:
    plugin = _Plugin(
        AuthResult(
            success=False,
            error="The archive folder must be an absolute path",
            store="gamevault",
        ),
    )

    result = await plugin.connect_gamevault_local("relative/path")

    assert result.success is False
    assert "absolute" in result.error


async def test_it_reaches_the_same_auth_entry_point_as_remote(
    plugin: _Plugin,
) -> None:
    """One auth path underneath, two typed routes on top.

    A separate route because the parameter sets are disjoint — not because
    local mode gets its own plumbing.
    """
    await plugin.connect_gamevault_local("/vault")

    args, _ = plugin.registry.auth_action.call_args
    assert args == ("gamevault", "start")
