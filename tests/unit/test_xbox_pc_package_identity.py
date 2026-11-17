"""Windows package names of an Xbox PC game and the env that hands them to the runtime (I-085)."""
from __future__ import annotations

from pathlib import Path

from unifideck.launcher.xbox_pc.game_manifest import inspect_game
from unifideck.launcher.xbox_pc.package_identity import PackageNames, package_names, publisher_id
from unifideck.launcher.xbox_pc.title_env import PACKAGE_IDENTITY_TITLES, title_env

MICROSOFT = "CN=Microsoft Corporation, O=Microsoft Corporation, L=Redmond, S=Washington, C=US"

CONFIG = """<?xml version="1.0" encoding="utf-8"?>
<Game configVersion="1">
  <Identity Name="Studio.Game" Publisher="{publisher}" Version="1.53.3.0"/>
  <StoreId>9NBL0XKVCN5L</StoreId>
  <ExecutableList><Executable Name="Game\\\\Game.exe" Id="Game"/></ExecutableList>
  {extra}
</Game>
"""


def test_publisher_id_matches_windows() -> None:
    # Microsoft's publisher id, the suffix of every first-party package family name.
    assert publisher_id(MICROSOFT) == "8wekyb3d8bbwe"


def test_package_names_from_the_identity() -> None:
    names = package_names("Studio.Game", MICROSOFT, "1.53.3.0", None)
    assert names == PackageNames(
        family="Studio.Game_8wekyb3d8bbwe",
        full="Studio.Game_1.53.3.0_x64__8wekyb3d8bbwe",
    )
    assert package_names("Studio.Game", MICROSOFT, "1.0.0.0", "X86").full == "Studio.Game_1.0.0.0_x86__8wekyb3d8bbwe"
    assert package_names("Studio.Game", MICROSOFT, "1.0.0.0", "mips").full.endswith("_x64__8wekyb3d8bbwe")


def test_an_unusable_identity_gives_no_names() -> None:
    assert package_names(None, MICROSOFT, "1.0.0.0", None) is None
    assert package_names("Studio.Game", "", "1.0.0.0", None) is None
    assert package_names("Studio.Game", MICROSOFT, "1.0", None) is None
    assert package_names("bad name/with slash", MICROSOFT, "1.0.0.0", None) is None


def test_inspect_game_reads_the_package_names(tmp_path: Path) -> None:
    (tmp_path / "MicrosoftGame.config").write_text(CONFIG.format(publisher=MICROSOFT, extra=""))
    game = inspect_game(tmp_path)
    assert game.package == PackageNames(
        family="Studio.Game_8wekyb3d8bbwe", full="Studio.Game_1.53.3.0_x64__8wekyb3d8bbwe",
    )


def test_a_manifest_without_identity_has_no_package(tmp_path: Path) -> None:
    config = CONFIG.format(publisher=MICROSOFT, extra="").replace(
        '<Identity Name="Studio.Game" Publisher="' + MICROSOFT + '" Version="1.53.3.0"/>', "",
    )
    (tmp_path / "MicrosoftGame.config").write_text(config)
    assert inspect_game(tmp_path).package is None


def test_only_listed_titles_get_the_package_identity() -> None:
    names = PackageNames(family="Studio.Game_8wekyb3d8bbwe", full="Studio.Game_1.0.0.0_x64__8wekyb3d8bbwe")
    assert "9NBL0XKVCN5L" in PACKAGE_IDENTITY_TITLES
    assert title_env("9nbl0xkvcn5l", package=names) == {
        "XODUS_PACKAGE_FAMILY_NAME": names.family,
        "XODUS_PACKAGE_FULL_NAME": names.full,
    }
    assert title_env("9NBL0XKVCN5L", package=None) == {}
    assert title_env("9PK087LNGJC5", package=names) == {}
