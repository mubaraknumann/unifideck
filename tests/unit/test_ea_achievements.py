"""EA achievements: the ``maxima-cli achievements`` command and its normalization.

Entries are shaped like the CLI's real output for STAR WARS Squadrons
(2026-11-16): icons at 40/208/416 px, hidden ones masked by EA.
"""
from __future__ import annotations

import json
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from unifideck.core.store_capabilities import ACHIEVEMENT_STORES
from unifideck.stores.ea.achievements import (
    EAAchievements,
    EAAchievementsError,
    normalize_ea_achievement,
)
from unifideck.stores.ea.cli import MaximaCli, MaximaCliError
from unifideck.stores.shared.achievements_error import StoreAchievementsError

GAME = "Origin.OFR.50.0004097"
ICONS = [f"https://achievements.gameservices.ea.com/achievements/icons/x-01-{s}.png" for s in (40, 208, 416)]


def _item(**over: Any) -> dict[str, Any]:
    item = {
        "set_id": "75158_317074_50844", "id": "a1", "name": "Ace",
        "description": "Completed the campaign.", "how_to": "", "awarded": False,
        "awarded_at": None, "hidden": False, "points": 10, "icons": ICONS,
    }
    item.update(over)
    return item


def test_ea_is_an_achievement_store() -> None:
    assert "ea" in ACHIEVEMENT_STORES


def test_locked_achievement() -> None:
    a = normalize_ea_achievement(_item())
    assert a["key"] == "a1"
    assert a["unlocked"] is False
    assert a["unlocked_at"] is None
    assert a["image_unlocked"] == ICONS[1] == a["image_locked"]
    assert a["rarity"] is None


def test_awarded_achievement_has_its_time() -> None:
    a = normalize_ea_achievement(_item(awarded=True, awarded_at="2026-11-15T18:59:12.000Z"))
    assert a["unlocked"] is True
    assert a["unlocked_at"] == pytest.approx(1794769152.0)


def test_locked_one_ignores_the_service_date() -> None:
    # EA returns the request time as ``date`` for locked achievements
    a = normalize_ea_achievement(_item(awarded=False, awarded_at="2026-11-15T18:59:12.000Z"))
    assert a["unlocked_at"] is None


def test_how_to_fills_an_empty_description() -> None:
    a = normalize_ea_achievement(_item(description="", how_to="Keep searching.", hidden=True))
    assert a["description"] == "Keep searching."
    assert a["hidden"] is True


def test_icon_falls_back_to_the_last() -> None:
    assert normalize_ea_achievement(_item(icons=["a.png", "b.png"]))["image_unlocked"] == "b.png"
    assert normalize_ea_achievement(_item(icons=[]))["image_unlocked"] == ""


class _StubCli:
    def __init__(self, result: list[dict[str, Any]] | Exception) -> None:
        self.result = result
        self.calls: list[str] = []

    async def achievements(self, offer: str) -> list[dict[str, Any]]:
        self.calls.append(offer)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


async def test_payload_is_summarized_and_cached() -> None:
    cli = _StubCli([
        _item(id="a1"),
        _item(id="a2", awarded=True, awarded_at="2026-11-15T18:59:12Z"),
    ])
    reader = EAAchievements(cli)  # type: ignore[arg-type]

    payload = await reader.get_game_achievements(GAME)

    assert payload["store"] == "ea"
    assert (payload["total"], payload["unlocked"]) == (2, 1)
    assert payload["achievements"][0]["key"] == "a2"
    assert cli.calls == ["Origin.OFR.50.0004097"]
    await reader.get_game_achievements(GAME)
    assert len(cli.calls) == 1
    await reader.get_game_achievements(GAME, force=True)
    assert len(cli.calls) == 2


async def test_offer_id_comes_from_the_row_id() -> None:
    cli = _StubCli([])
    payload = await EAAchievements(cli).get_game_achievements("OFB-EAST__57289")  # type: ignore[arg-type]
    assert cli.calls == ["OFB-EAST:57289"]
    assert payload["total"] == 0


@pytest.mark.parametrize(
    ("cli_code", "code"),
    [("not_signed_in", "not_authed"), ("timeout", "offline"), ("command_failed", "ea_unavailable")],
)
async def test_cli_errors_become_wire_codes(cli_code: str, code: str) -> None:
    reader = EAAchievements(_StubCli(MaximaCliError(cli_code)))  # type: ignore[arg-type]
    with pytest.raises(EAAchievementsError) as err:
        await reader.get_game_achievements(GAME)
    assert err.value.code == code
    assert isinstance(err.value, StoreAchievementsError)


async def test_cli_runs_the_achievements_command(tmp_path: Path) -> None:
    record = tmp_path / "record.json"
    script = tmp_path / "maxima-cli"
    out = json.dumps({"offer_id": GAME, "achievements": [_item()]})
    script.write_text(
        f"#!{sys.executable}\nimport json, sys\n"
        f"open({str(record)!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
        f"print({out!r})\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)

    items = await MaximaCli(str(script), data_dir=tmp_path / "data").achievements(GAME)

    assert json.loads(record.read_text()) == ["achievements", "--offer-id", GAME]
    assert items[0]["id"] == "a1"
