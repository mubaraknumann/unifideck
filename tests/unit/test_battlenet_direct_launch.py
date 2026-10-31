"""Battle.net direct launch: start the installed version's own binary.

tests/unit/test_battlenet_direct_launch.py

``--exec="launch <family>"`` auto-starts only a title's retail version, and
``launch WoW`` resets the client to Retail over a finished Classic install.
These pin the replacement: read the installed version from the prefix at
launch time and run the binary the client itself would run.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from tests.unit.test_battlenet_launch import _arm, _install_client, _Plan, _record_exec
from unifideck.launcher.proton.handlers import battlenet as handler
from unifideck.launcher.proton.handlers import battlenet_direct as direct
from unifideck.launcher.proton.handlers import battlenet_exec as bnet_exec
from unifideck.launcher.types.errors import GameFailedError
from unifideck.stores.battlenet import library
from unifideck.stores.battlenet.ownership import InstalledGame
from unifideck.stores.battlenet.ownership.pub_catalog import (
    CatalogEntry,
    MergedCatalog,
    merge_fragments,
)

WOW_LOG = (
    "I 2026-10-31 13:40:21.515410 [GameLaunchController] {Main} LaunchBinary: "
    "uid=wow_classic_era selectedRegion=US binaryType=game\r\n"
    "I 2026-10-31 13:40:22.026257 [InstallManager] {Main} Launched "
    "C:/Program Files (x86)/World of Warcraft/_classic_era_/WoWClassic.exe "
    "with args: -launcherlogin -uid wow_classic_era -initialgamemode=cera\r\n"
)
W3_LOG = (
    "I 2026-10-17 17:16:09.495795 [InstallManager] {Main} Launched "
    "C:/Program Files (x86)/Warcraft III/_retail_/x86_64/Warcraft III.exe "
    "with args: -launch -uid w3, pid: 1888\r\n"
)


# --------------------------------------------------------------------------
# the client's own record
# --------------------------------------------------------------------------


def test_launched_line_without_a_pid_suffix_and_crlf() -> None:
    hit = direct.launched_line(WOW_LOG, "wow_classic_era")
    assert hit == (
        "C:/Program Files (x86)/World of Warcraft/_classic_era_/WoWClassic.exe",
        ("-launcherlogin", "-uid", "wow_classic_era", "-initialgamemode=cera"),
    )


def test_launched_line_with_a_pid_suffix() -> None:
    hit = direct.launched_line(W3_LOG, "w3")
    assert hit is not None
    assert hit[1] == ("-launch", "-uid", "w3")


def test_launched_line_ignores_other_versions() -> None:
    assert direct.launched_line(WOW_LOG, "wow") is None
    assert direct.launched_line(WOW_LOG + W3_LOG, "w3") is not None


def test_the_newest_matching_line_wins() -> None:
    older = WOW_LOG.replace("-initialgamemode=cera", "-old")
    hit = direct.launched_line(older + WOW_LOG, "wow_classic_era")
    assert hit is not None
    assert hit[1][-1] == "-initialgamemode=cera"


def test_match_case_finds_a_differently_cased_file(tmp_path: Path) -> None:
    """The client logs WoWClassic.exe for a file that is WowClassic.exe."""
    real = tmp_path / "_classic_era_" / "WowClassic.exe"
    real.parent.mkdir()
    real.write_bytes(b"MZ")
    assert direct.match_case(tmp_path / "_classic_era_" / "WoWClassic.exe") == real
    assert direct.match_case(tmp_path / "_classic_era_" / "Missing.exe") is None


# --------------------------------------------------------------------------
# discovery, for the first Play after an install
# --------------------------------------------------------------------------


def _wow_install(tmp_path: Path, *extra_exes: str) -> Path:
    root = tmp_path / "World of Warcraft"
    flavor = root / "_classic_era_"
    flavor.mkdir(parents=True)
    (flavor / ".flavor.info").write_text("Product Flavor!STRING:0\nwow_classic_era\n")
    for name in ("WowClassic.exe", "BlizzardError.exe", *extra_exes):
        (flavor / name).write_bytes(b"MZ")
    (root / "World of Warcraft Launcher.exe").write_bytes(b"MZ")
    return root


def _row(root: Path, uid: str = "wow_classic_era") -> InstalledGame:
    return InstalledGame(
        code=uid, uid=uid, host_install_path=str(root), is_ready=True,
    )


def _wow_entry() -> CatalogEntry:
    return CatalogEntry(
        product_id="WoW",
        program_id="WoW",
        type_uids={"retail": "wow", "wow_classic_era": "wow_classic_era"},
        type_launch_families={"wow_classic_era": "WoWC"},
        type_launch_args={"wow_classic_era": ("-initialgamemode=cera",)},
    )


def test_discovery_uses_the_flavor_dir_and_catalog_args(tmp_path: Path) -> None:
    root = _wow_install(tmp_path)
    target = direct._discover(_row(root), _wow_entry())
    assert target is not None
    assert target.exe == root / "_classic_era_" / "WowClassic.exe"
    assert target.args == (
        "-launcherlogin", "-uid", "wow_classic_era", "-initialgamemode=cera",
    )


def test_discovery_never_guesses_between_two_binaries(tmp_path: Path) -> None:
    root = _wow_install(tmp_path, "Other.exe")
    assert direct._discover(_row(root), _wow_entry()) is None


# --------------------------------------------------------------------------
# catalog: per-version launch arguments
# --------------------------------------------------------------------------


def _catalog() -> MergedCatalog:
    product = {
        "id": "WoW",
        "base": {
            "program_id": "WoW",
            "types": {
                "retail": {"uid": "wow"},
                "wow_classic_era": {
                    "uid": "wow_classic_era",
                    "override_product_id": "WoWC",
                    "game_packages": [
                        {"id": "hc", "rank": 2, "launchable": True,
                         "launch_arguments": ["-initialgamemode=hc"]},
                        {"id": "base", "rank": 1, "launchable": True,
                         "launch_arguments": ["-initialgamemode=cera"]},
                    ],
                },
            },
        },
    }
    return merge_fragments(iter([{"fragment_id": "x", "products": [product]}]))


def test_launch_args_come_from_the_lowest_ranked_launchable_package() -> None:
    entry = _catalog().entry_for("WoW")
    assert entry is not None
    assert entry.launch_args_for("wow_classic_era") == ("-initialgamemode=cera",)
    assert entry.launch_args_for("wow") == ()
    assert entry.launch_args_for(None) == ()


# --------------------------------------------------------------------------
# install time: the id map learns the installed version's family
# --------------------------------------------------------------------------


def test_install_records_the_installed_versions_family(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Classic installed under the wow tile kept family WoW until a sync."""
    monkeypatch.setattr(library, "read_catalog", lambda _drive_c: _catalog())
    row = _row(tmp_path)
    assert library.installed_version_fields(tmp_path, "wow", row) == {
        "family": "WoWC", "client_selects": True,
    }
    retail = _row(tmp_path, "wow")
    assert library.installed_version_fields(tmp_path, "wow", retail) == {
        "family": "WoW", "client_selects": False,
    }
    assert library.installed_version_fields(None, "wow", row) == {}


# --------------------------------------------------------------------------
# routing in battlenet_launch
# --------------------------------------------------------------------------


@pytest.fixture
def plan(tmp_path: Path) -> _Plan:
    _install_client(tmp_path)
    return _Plan(tmp_path)


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    seen: dict[str, Any] = {"exec": [], "direct": [], "toasts": []}

    async def fake_spawn(plan_: Any, exe: Path) -> None:
        return None

    def fake_start(plan_: Any, target: direct.DirectTarget) -> asyncio.Task[int]:
        seen["direct"].append(target)

        async def run() -> int:
            return 0

        return asyncio.ensure_future(run())

    monkeypatch.setattr(handler, "_start_client_detached", fake_spawn)
    monkeypatch.setattr(handler, "launcher_toast", lambda key, **kw: seen["toasts"].append(key))
    monkeypatch.setattr(handler.direct, "start", fake_start)
    monkeypatch.setattr(bnet_exec, "issue_exec", _record_exec(seen))
    # The id map is stale on purpose: what a Classic install left behind.
    monkeypatch.setattr(handler, "resolve_family", lambda uid: "WoW")
    monkeypatch.setattr(handler, "client_selects_version", lambda uid: False)
    return seen


def _resolved(monkeypatch: pytest.MonkeyPatch, found: direct.Resolution) -> None:
    monkeypatch.setattr(handler.direct, "resolve", lambda prefix, uid: found)


_TARGET = direct.DirectTarget(
    "wow_classic_era", Path("/x/_classic_era_/WowClassic.exe"),
    ("-launcherlogin", "-uid", "wow_classic_era"), "test",
)


def test_one_installed_version_launches_its_binary_directly(
    plan: _Plan, calls: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _resolved(monkeypatch, direct.Resolution(
        versions=[_row(Path("/x"))], family="WoWC", selects=True, target=_TARGET,
    ))
    _arm(monkeypatch, ready=True, game="4242")
    assert asyncio.run(handler.battlenet_launch(plan)) == 0
    assert calls["direct"] == [_TARGET]
    assert calls["exec"] == []


def test_a_direct_launch_that_starts_nothing_fails_loudly(
    plan: _Plan, calls: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _resolved(monkeypatch, direct.Resolution(
        versions=[_row(Path("/x"))], family="WoWC", target=_TARGET,
    ))
    _arm(monkeypatch, ready=True, game=None)
    with pytest.raises(GameFailedError) as excinfo:
        asyncio.run(handler.battlenet_launch(plan))
    assert "WowClassic.exe" in str(excinfo.value)


def test_several_versions_hand_over_to_the_client_picker(
    plan: _Plan, calls: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The user's choice: with 2+ versions installed, the client decides."""
    _resolved(monkeypatch, direct.Resolution(
        versions=[_row(Path("/x")), _row(Path("/y"), "wow")],
        family="WoWC", selects=True,
    ))
    _arm(monkeypatch, ready=True, game=None)
    assert asyncio.run(handler.battlenet_launch(plan)) == 0
    assert calls["direct"] == []
    assert calls["exec"][0][0] == "launch WoWC"


def test_the_launch_time_family_beats_a_stale_id_map(
    plan: _Plan, calls: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No binary found: still select Classic, never reset to Retail."""
    _resolved(monkeypatch, direct.Resolution(
        versions=[_row(Path("/x"))], family="WoWC", selects=True,
    ))
    _arm(monkeypatch, ready=True, game=None)
    assert asyncio.run(handler.battlenet_launch(plan)) == 0
    assert calls["exec"][0][0] == "launch WoWC"


def test_an_unreadable_prefix_falls_back_to_the_id_map(
    plan: _Plan, calls: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _resolved(monkeypatch, direct.Resolution())
    _arm(monkeypatch, ready=True, game="1")
    assert asyncio.run(handler.battlenet_launch(plan)) == 0
    assert calls["exec"][0][0] == "launch WoW"
