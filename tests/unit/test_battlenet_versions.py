"""Battle.net: launch the version that is installed, read at launch time.

tests/unit/test_battlenet_versions.py

The id map learns a title's ``--exec`` family only at sync. Classic Era
installed under the ``wow`` tile after the last sync kept ``family: WoW``,
and ``launch WoW`` resets the client to Retail, offering Install over the
finished Classic install. These pin the launch-time read that replaces it,
and the install-time write that keeps the id map honest.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from tests.unit.test_battlenet_launch import _arm, _install_client, _Plan, _record_exec
from unifideck.launcher.proton.handlers import battlenet as handler
from unifideck.launcher.proton.handlers import battlenet_exec as bnet_exec
from unifideck.launcher.proton.handlers import battlenet_versions as versions
from unifideck.stores.battlenet import library
from unifideck.stores.battlenet.ownership import InstalledGame
from unifideck.stores.battlenet.ownership.pub_catalog import CatalogEntry, MergedCatalog


def _row(uid: str, last_played: int | None = None) -> InstalledGame:
    return InstalledGame(code=uid, uid=uid, is_ready=True, last_played_ms=last_played)


def _wow_entry() -> CatalogEntry:
    return CatalogEntry(
        product_id="WoW",
        program_id="WoW",
        type_uids={"retail": "wow", "wow_classic_era": "wow_classic_era"},
        type_launch_families={"wow_classic_era": "WoWC"},
    )


def _catalog() -> MergedCatalog:
    catalog = MergedCatalog()
    catalog.entries["WoW"] = _wow_entry()
    return catalog


# --------------------------------------------------------------------------
# resolution
# --------------------------------------------------------------------------


def _installed(monkeypatch: pytest.MonkeyPatch, *rows: InstalledGame) -> None:
    monkeypatch.setattr(
        versions, "_ready_versions", lambda prefix, uid: (_wow_entry(), list(rows)),
    )


def test_classic_alone_selects_its_own_family(monkeypatch: pytest.MonkeyPatch) -> None:
    _installed(monkeypatch, _row("wow_classic_era"))
    found = versions.resolve(Path("/p"), "wow")
    assert (found.family, found.selects) == ("WoWC", True)


def test_retail_alone_auto_launches(monkeypatch: pytest.MonkeyPatch) -> None:
    _installed(monkeypatch, _row("wow"))
    found = versions.resolve(Path("/p"), "wow")
    assert (found.family, found.selects) == ("WoW", False)


def test_several_versions_select_the_most_recently_played(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _installed(monkeypatch, _row("wow", 10), _row("wow_classic_era", 20))
    found = versions.resolve(Path("/p"), "wow")
    assert (found.family, found.selects) == ("WoWC", True)


def test_an_unreadable_prefix_answers_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(prefix: Path, uid: str) -> None:
        raise OSError("unreadable")

    monkeypatch.setattr(versions, "_ready_versions", boom)
    assert versions.resolve(Path("/p"), "wow") == versions.Resolution()


# --------------------------------------------------------------------------
# install time: the id map learns the installed version's family
# --------------------------------------------------------------------------


def test_install_records_the_installed_versions_family(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Classic installed under the wow tile kept family WoW until a sync."""
    monkeypatch.setattr(library, "read_catalog", lambda _drive_c: _catalog())
    assert library.installed_version_fields(tmp_path, "wow", _row("wow_classic_era")) == {
        "family": "WoWC", "client_selects": True,
    }
    assert library.installed_version_fields(tmp_path, "wow", _row("wow")) == {
        "family": "WoW", "client_selects": False,
    }
    assert library.installed_version_fields(None, "wow", _row("wow")) == {}


# --------------------------------------------------------------------------
# routing in battlenet_launch
# --------------------------------------------------------------------------


@pytest.fixture
def plan(tmp_path: Path) -> _Plan:
    _install_client(tmp_path)
    return _Plan(tmp_path)


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    seen: dict[str, Any] = {"exec": [], "toasts": []}

    async def fake_spawn(*_args: Any) -> None:
        return None

    monkeypatch.setattr(handler, "_start_client_detached", fake_spawn)
    monkeypatch.setattr(handler, "launcher_toast", lambda key, **_kw: seen["toasts"].append(key))
    monkeypatch.setattr(bnet_exec, "issue_exec", _record_exec(seen))
    # The id map is stale on purpose: what a Classic install left behind.
    monkeypatch.setattr(handler, "resolve_family", lambda uid: "WoW")
    monkeypatch.setattr(handler, "client_selects_version", lambda uid: False)
    return seen


def _resolved(monkeypatch: pytest.MonkeyPatch, found: versions.Resolution) -> None:
    monkeypatch.setattr(handler.versions, "resolve", lambda prefix, uid, chosen=None: found)


def test_the_launch_time_family_beats_a_stale_id_map(
    plan: _Plan, calls: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Select Classic, never reset the client to Retail."""
    _resolved(monkeypatch, versions.Resolution(
        versions=[_row("wow_classic_era")], family="WoWC", selects=True,
    ))
    _arm(monkeypatch, ready=True, game=None)
    assert asyncio.run(handler.battlenet_launch(plan)) == 0
    assert calls["exec"][0][0] == "launch WoWC"


def test_an_unreadable_prefix_falls_back_to_the_id_map(
    plan: _Plan, calls: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _resolved(monkeypatch, versions.Resolution())
    _arm(monkeypatch, ready=True, game="1")
    assert asyncio.run(handler.battlenet_launch(plan)) == 0
    assert calls["exec"][0][0] == "launch WoW"


def test_the_client_action_sends_nothing_and_follows_the_client(
    plan: _Plan, calls: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """For a retail title ``launch WoW`` starts the game, and the client
    refuses to install another version while one runs. Opening the client
    must therefore send no command and end only when the client closes."""
    waited: list[Path] = []

    async def fake_wait(prefix: Path, *_a: Any, **_k: Any) -> None:
        waited.append(prefix)

    _arm(monkeypatch, ready=True, game=None)
    monkeypatch.setattr(handler.watch, "wait_while_client_running", fake_wait)
    assert asyncio.run(handler.battlenet_client_launch(plan)) == 0
    assert calls["exec"] == []
    assert waited == [plan.prefix_path]


# --------------------------------------------------------------------------
# the launch popup's pick
# --------------------------------------------------------------------------


def test_a_picked_retail_auto_launches_beside_classic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The user answered the popup, so no client picker is needed."""
    _installed(monkeypatch, _row("wow", 10), _row("wow_classic_era", 20))
    found = versions.resolve(Path("/p"), "wow", chosen="wow")
    assert (found.family, found.selects) == ("WoW", False)


def test_a_picked_classic_still_waits_for_play_in_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The client starts only the retail version by itself."""
    _installed(monkeypatch, _row("wow", 20), _row("wow_classic_era", 10))
    found = versions.resolve(Path("/p"), "wow", chosen="wow_classic_era")
    assert (found.family, found.selects) == ("WoWC", True)


def test_a_pick_that_is_no_longer_installed_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _installed(monkeypatch, _row("wow"))
    found = versions.resolve(Path("/p"), "wow", chosen="wow_classic")
    assert (found.family, found.selects) == ("WoW", False)


def test_the_handler_passes_the_popup_choice_to_resolution(
    plan: _Plan, calls: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The popup records its pick; the launcher consumes it at launch."""
    from unifideck.stores.battlenet import launch_choice

    seen: list[str | None] = []

    def fake_resolve(prefix: Path, uid: str, chosen: str | None = None) -> versions.Resolution:
        seen.append(chosen)
        return versions.Resolution(versions=[_row("wow")], family="WoW", target_uid="wow")

    plan.context.game_id = "wow"
    launch_choice.record("wow", "wow")
    monkeypatch.setattr(handler.versions, "resolve", fake_resolve)
    _arm(monkeypatch, ready=True, game="1")
    assert asyncio.run(handler.battlenet_launch(plan)) == 0
    assert seen == ["wow"]
    assert launch_choice.consume("wow") is None, "a choice is used only once"


def test_the_client_choice_opens_the_client_only(
    plan: _Plan, calls: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unifideck.stores.battlenet import launch_choice

    opened: list[bool] = []

    async def fake_client_launch(plan_: Any) -> int:
        opened.append(True)
        return 0

    plan.context.game_id = "wow"
    launch_choice.record("wow", launch_choice.CLIENT)
    monkeypatch.setattr(handler, "battlenet_client_launch", fake_client_launch)
    assert asyncio.run(handler.battlenet_launch(plan)) == 0
    assert opened == [True]
    assert calls["exec"] == []


def test_a_non_retail_version_is_started_by_pressing_play(
    plan: _Plan, calls: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--exec`` only selects Classic; the client's own Play starts it."""
    pressed: list[str] = []

    async def fake_press(plan_: Any, exe: Path, uid: str, **_kw: Any) -> tuple[str, set[str]]:
        pressed.append(uid)
        return "4242", set()

    _resolved(monkeypatch, versions.Resolution(
        versions=[_row("wow_classic_era")], family="WoWC", selects=True,
        target_uid="wow_classic_era", non_retail=True,
    ))
    monkeypatch.setattr(handler.play, "press_play", fake_press)
    _arm(monkeypatch, ready=True, game=None)
    assert asyncio.run(handler.battlenet_launch(plan)) == 0
    assert pressed == ["wow_classic_era"]
    assert calls["exec"] == [], "no launch command for a non-retail version"


def test_retail_still_goes_through_exec(
    plan: _Plan, calls: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _resolved(monkeypatch, versions.Resolution(
        versions=[_row("wow")], family="WoW", target_uid="wow", non_retail=False,
    ))
    _arm(monkeypatch, ready=True, game="1")
    assert asyncio.run(handler.battlenet_launch(plan)) == 0
    assert calls["exec"][0][0] == "launch WoW"


def test_resolution_flags_the_version_to_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    _installed(monkeypatch, _row("wow", 10), _row("wow_classic_era", 20))
    found = versions.resolve(Path("/p"), "wow", chosen="wow_classic_era")
    assert (found.target_uid, found.non_retail) == ("wow_classic_era", True)
    found = versions.resolve(Path("/p"), "wow", chosen="wow")
    assert (found.target_uid, found.non_retail) == ("wow", False)


def test_launch_target_falls_back_to_the_id_map(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(versions, "_ready_versions", lambda prefix, uid: (None, []))
    target = versions.launch_target(
        Path("/p"), "wow", None,
        family_fallback=lambda uid: "WoW", selects_fallback=lambda uid: True,
    )
    assert target == versions.LaunchTarget(family="WoW", selects=True)
    assert versions.launch_target(
        Path("/p"), "wow", None,
        family_fallback=lambda uid: None, selects_fallback=lambda uid: False,
    ) is None
