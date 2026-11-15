"""Tests for ``stores.ea.library`` and ``stores.ea.auth``."""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from unifideck.core.types import AuthResult, Events
from unifideck.stores.ea.auth import EA_REDIRECT, EAAuthFlow
from unifideck.stores.ea.cli import MaximaCliError
from unifideck.stores.ea.library import EALibraryReader

# The shape `maxima-cli list-games --json` printed on 2026-11-15.
_ENTRY: dict[str, Any] = {
    "offer_id": "OFB-EAST:59474",
    "content_id": "1002980",
    "slug": "dragon-age-2",
    "name": "Dragon Age 2",
    "ownership": ["XGP_VAULT"],
    "has_cloud_save": True,
    "install_check": "[HKEY_LOCAL_MACHINE\\SOFTWARE\\BioWare\\Dragon Age II\\Install Dir]bin_ship\\DragonAge2.exe",
    "execute_path": None,
    "extra_offers": [],
}


async def test_library_rows_use_the_row_id_and_keep_the_offer(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    cli = MagicMock()
    steam_owned = {**_ENTRY, "offer_id": "Origin.OFR.50.0001456", "ownership": ["STEAM"]}
    cli.list_games = AsyncMock(return_value=[_ENTRY, steam_owned, {}])
    games = await EALibraryReader(cli).get_library()
    assert games is not None
    # Steam-owned EA games are shown too: EA serves their build and licence.
    assert [g.store_game_id for g in games] == ["OFB-EAST__59474", "Origin.OFR.50.0001456"]
    assert games[1].metadata["ownership"] == ["STEAM"]
    game = games[0]
    assert game.store == "ea"
    assert game.title == "Dragon Age 2"
    assert game.metadata["offer_id"] == "OFB-EAST:59474"
    assert game.metadata["content_id"] == "1002980"
    assert game.metadata["has_cloud_save"] is True


async def test_unreadable_library_is_none_never_empty() -> None:
    cli = MagicMock()
    cli.list_games = AsyncMock(side_effect=MaximaCliError("not_signed_in"))
    assert await EALibraryReader(cli).get_library() is None


def _flow(*, signed_in: bool) -> tuple[EAAuthFlow, MagicMock, MagicMock, MagicMock]:
    bus = MagicMock()
    bus.emit = AsyncMock()
    orch = MagicMock()
    orch.run_flow = AsyncMock(return_value=AuthResult(success=True, store="ea"))
    cli = MagicMock()
    cli.cli_path = "/bin/maxima-cli"
    cli.signed_in = AsyncMock(return_value=signed_in)
    cli.auth_exchange = AsyncMock(return_value="42")
    return EAAuthFlow(bus, orch, cli), bus, orch, cli


async def test_a_valid_stored_sign_in_skips_the_window() -> None:
    flow, bus, orch, _ = _flow(signed_in=True)
    result = await flow.start_auth()
    assert result.success
    orch.run_flow.assert_not_called()
    bus.emit.assert_awaited_with(Events.STORE_AUTH_COMPLETE, store="ea")


async def test_sign_in_intercepts_the_qrc_redirect() -> None:
    flow, _, orch, _ = _flow(signed_in=False)
    await flow.start_auth()
    kwargs = orch.run_flow.await_args.kwargs
    assert kwargs["intercept"] is EA_REDIRECT
    assert kwargs["write_url_file"].endswith("ea_auth_url.txt")
    assert EA_REDIRECT.location_prefix == "qrc:"


async def test_exchange_failure_is_a_failed_auth_result() -> None:
    flow, _, orch, cli = _flow(signed_in=False)
    await flow.start_auth()
    exchange = orch.run_flow.await_args.kwargs["exchange_code"]
    assert (await exchange("CODE")).success
    cli.auth_exchange.assert_awaited_with("CODE")
    cli.auth_exchange.side_effect = MaximaCliError("command_failed")
    failed = await exchange("CODE")
    assert not failed.success
    assert failed.error == "ea_exchange_failed: command_failed"

