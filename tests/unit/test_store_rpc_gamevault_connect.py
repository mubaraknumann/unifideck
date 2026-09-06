"""``StoreRPCMixin.connect_gamevault`` — the RPC the credential form calls.

These tests exist because of a shipped defect, not a hypothetical one. The
GameVault modal was wired to ``store_auth`` with a third argument carrying the
credentials, which is the signature that RPC had on the 0.7.3 base the store
was written against. On this branch ``store_auth`` takes ``(store, action)``
and nothing else — the ``"complete"`` + ``{code}`` kwargs channel was removed
with Ubisoft's API login — so every sign-in raised

    TypeError: store_auth() takes 3 positional arguments but 4 were given

*before any network call happened*, and the frontend reported it as the
generic "connection failed". Nothing caught it: the dead-RPC gate checks that
a route has a caller, not that the caller passes arguments the method accepts,
and no test called the route the way the frontend does.

So the first test below calls it exactly as ``useStoreAuth`` does — five
positional arguments — and would fail on any arity change.
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


async def test_accepts_the_five_arguments_the_modal_sends(plugin: _Plugin) -> None:
    """Positional, in the modal's order. This is the regression guard."""
    result = await plugin.connect_gamevault(
        "https://gv.example.com", "someone", "hunter2", True, "/run/media/sd/dl",
    )

    assert result.success is True
    plugin.registry.auth_action.assert_awaited_once_with(
        "gamevault",
        "start",
        server_url="https://gv.example.com",
        username="someone",
        password="hunter2",
        verify_ssl=True,
        download_dir="/run/media/sd/dl",
    )


async def test_an_empty_download_dir_means_the_configured_default(
    plugin: _Plugin,
) -> None:
    """``""`` must not reach the store as a path — it would be ``Path("")``."""
    await plugin.connect_gamevault("https://gv.example.com", "someone", "pw", True, "")

    assert plugin.registry.auth_action.await_args.kwargs["download_dir"] is None


async def test_verify_ssl_false_survives_the_boundary(plugin: _Plugin) -> None:
    """A LAN server with a self-signed certificate is the common case."""
    await plugin.connect_gamevault("https://gv.local", "someone", "pw", False, "")

    assert plugin.registry.auth_action.await_args.kwargs["verify_ssl"] is False


async def test_the_failure_reason_reaches_the_caller() -> None:
    """The frontend shows ``error``; a swallowed one becomes 'connection failed'."""
    plugin = _Plugin(
        AuthResult(success=False, error="Invalid username or password", store="gamevault"),
    )

    result = await plugin.connect_gamevault("https://gv.local", "someone", "wrong", True, "")

    assert result.success is False
    assert result.error == "Invalid username or password"


async def test_the_password_is_never_logged(
    plugin: _Plugin, caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO")

    await plugin.connect_gamevault(
        "https://gv.example.com", "someone", "correct-horse-battery-staple", True, "",
    )

    assert "correct-horse-battery-staple" not in caplog.text
    # The rest is what makes a field report diagnosable.
    assert "gv.example.com" in caplog.text
    assert "someone" in caplog.text
