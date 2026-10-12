"""``StoreRPCMixin``: argument forwarding and secret hygiene for the RomM modal.

These routes log the host, the auth method and the outcome, never the token,
the device code, or credentials embedded in the server URL: a logged token
outlives the connection it belongs to.
"""
from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock

import pytest

from unifideck.core.types import AuthResult
from unifideck.rpc.mixins.store import StoreRPCMixin


class _Plugin(StoreRPCMixin):
    def __init__(self, result: Any) -> None:
        self.registry = AsyncMock()
        self.registry.auth_action = AsyncMock(return_value=result)


@pytest.fixture
def plugin() -> _Plugin:
    return _Plugin(AuthResult(success=True, store="romm"))


async def test_connect_forwards_every_argument_to_start(plugin: _Plugin) -> None:
    await plugin.connect_romm("a1", "https://r.example", "tok", False, "/dl")

    plugin.registry.auth_action.assert_awaited_once_with(
        "romm", "start", attempt_id="a1", server_url="https://r.example",
        token="tok", verify_ssl=False,
        download_dir="/dl",
    )


async def test_complete_and_cancel_reach_the_registry(plugin: _Plugin) -> None:
    await plugin.complete_romm_connection("a1", ["win"])
    await plugin.cancel_romm_connection("a1")

    calls = plugin.registry.auth_action.await_args_list
    assert calls[0].args == ("romm", "complete")
    assert calls[0].kwargs == {"attempt_id": "a1", "excluded_fs_slugs": ["win"]}
    assert calls[1].args == ("romm", "cancel")
    assert calls[1].kwargs == {"attempt_id": "a1"}


async def test_secrets_never_reach_the_log(
    plugin: _Plugin, caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG):
        await plugin.connect_romm("a1", "https://r.example", "SECRET-TOKEN")

    assert "SECRET-TOKEN" not in caplog.text
    assert "SECRET-PW" not in caplog.text


async def test_url_credentials_never_reach_the_log(
    plugin: _Plugin, caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG):
        await plugin.connect_romm("a1", "https://user:URL-SECRET@romm.example", "")

    assert "URL-SECRET" not in caplog.text
