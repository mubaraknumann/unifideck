"""GOGLibrary install scanning across storage locations.

Regression guard: a GOG game installed OUTSIDE the default
``download_dir`` (e.g. on the SD card or a custom path) must still be
found by ``get_installed_game_info`` / ``get_installed``. Otherwise
uninstall can't locate it and silently no-ops — leaving the install
(and its ``goggame-*.info``) on disk, so a later reinstall sees a
"valid existing install" → ``repair`` and never re-fetches the chosen
language.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import Mock

import unifideck.stores.gog.library as gog_library
from unifideck.core.types import Game
from unifideck.stores.gog.config import GOGConfig
from unifideck.stores.gog.library import GOGLibrary
from unifideck.stores.shared.install_status import merge_install_status

if TYPE_CHECKING:
    import pytest

#: The arguments ``GOGStore.get_library`` passes to the shared merge. Both
#: are GOG-only and load-bearing (see ``shared/install_status``), so these
#: tests must use them or they would be asserting Epic's semantics.
_GOG_ARGS = {"exe_key": "executable", "verify_dir": False}


def _make_install(root: Path, name: str, game_id: str) -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / ".unifideck-id").write_text(
        json.dumps({"game_id": game_id}), encoding="utf-8",
    )
    return d


def test_finds_game_outside_download_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    download_dir = tmp_path / "GOG Games"  # default location, empty
    download_dir.mkdir()
    sd_like = tmp_path / "sdcard" / "Games"  # a different location
    sd_like.mkdir(parents=True)
    install = _make_install(sd_like, "BioShock", "2022341186")

    # The scan set yields the SD-like dir, NOT download_dir.
    monkeypatch.setattr(
        gog_library, "get_all_game_directories", lambda _cfg: [str(sd_like)],
    )
    lib = GOGLibrary(
        config=GOGConfig(download_dir=str(download_dir)),
        tokens=Mock(),
        exe_finder=lambda _p: None,
        config_manager=None,
    )

    info = lib.get_installed_game_info("2022341186")
    assert info is not None
    assert Path(info["install_path"]) == install


def test_returns_none_when_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        gog_library, "get_all_game_directories", lambda _cfg: [str(tmp_path)],
    )
    lib = GOGLibrary(
        config=GOGConfig(download_dir=str(tmp_path)),
        tokens=Mock(),
        exe_finder=lambda _p: None,
        config_manager=None,
    )
    assert lib.get_installed_game_info("nope") is None


def test_get_installed_scans_all_locations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    loc1 = tmp_path / "a"
    loc2 = tmp_path / "b"
    loc1.mkdir()
    loc2.mkdir()
    _make_install(loc1, "GameA", "111")
    _make_install(loc2, "GameB", "222")
    monkeypatch.setattr(
        gog_library,
        "get_all_game_directories",
        lambda _cfg: [str(loc1), str(loc2)],
    )
    lib = GOGLibrary(
        config=GOGConfig(download_dir=str(tmp_path / "empty")),
        tokens=Mock(),
        exe_finder=lambda _p: None,
        config_manager=None,
    )
    assert set(lib.get_installed()) == {"111", "222"}


def test_get_installed_map_returns_path_and_exe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    loc = tmp_path / "Games"
    loc.mkdir()
    install = _make_install(loc, "BioShock", "2022341186")
    monkeypatch.setattr(
        gog_library, "get_all_game_directories", lambda _cfg: [str(loc)],
    )
    lib = GOGLibrary(
        config=GOGConfig(download_dir=str(tmp_path / "empty")),
        tokens=Mock(),
        exe_finder=lambda p: f"{p}/start.sh",
        config_manager=None,
    )

    found = lib.get_installed_map()
    assert found == {
        "2022341186": {
            "install_path": str(install),
            "executable": f"{install}/start.sh",
        },
    }


def test_get_installed_map_dedupes_across_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    loc1 = tmp_path / "a"
    loc2 = tmp_path / "b"
    loc1.mkdir()
    loc2.mkdir()
    first = _make_install(loc1, "GameX", "333")
    _make_install(loc2, "GameY", "333")  # same id, second location
    monkeypatch.setattr(
        gog_library,
        "get_all_game_directories",
        lambda _cfg: [str(loc1), str(loc2)],
    )
    lib = GOGLibrary(
        config=GOGConfig(download_dir=str(tmp_path / "empty")),
        tokens=Mock(),
        exe_finder=lambda _p: None,
        config_manager=None,
    )

    found = lib.get_installed_map()
    # One entry, first scanned location wins.
    assert list(found.keys()) == ["333"]
    assert found["333"]["install_path"] == str(first)


def test_merge_install_status_marks_owned_installed() -> None:
    owned = [
        Game(app_id=0, store="gog", store_game_id="111", title="A"),
        Game(app_id=0, store="gog", store_game_id="222", title="B"),
    ]
    installed = {
        "111": {"install_path": "/x/A", "executable": "/x/A/start.sh"},
    }

    merged = merge_install_status(owned, installed, **_GOG_ARGS)

    by_id = {g.store_game_id: g for g in merged}
    assert by_id["111"].installed is True
    assert by_id["111"].install_path == "/x/A"
    assert by_id["111"].exe_path == "/x/A/start.sh"
    # Owned game with no install dir stays not-installed.
    assert by_id["222"].installed is False
    assert by_id["222"].install_path is None
    assert by_id["222"].exe_path is None


def test_merge_install_status_preserves_fields() -> None:
    owned = [
        Game(
            app_id=42,
            store="gog",
            store_game_id="111",
            title="A",
            tags=["rpg"],
            icon_url="icon",
            hero_url="hero",
            logo_url="logo",
            size_bytes=99,
            metadata={"k": "v"},
        ),
    ]
    installed = {"111": {"install_path": "/x/A", "executable": "/x/A/run"}}

    merged = merge_install_status(owned, installed, **_GOG_ARGS)[0]

    assert merged.app_id == 42
    assert merged.tags == ["rpg"]
    assert merged.icon_url == "icon"
    assert merged.hero_url == "hero"
    assert merged.logo_url == "logo"
    assert merged.size_bytes == 99
    assert merged.metadata == {"k": "v"}
    # tags / metadata are copies, not aliases of the owned game's objects.
    merged.tags.append("fps")
    merged.metadata["k2"] = "v2"
    assert owned[0].tags == ["rpg"]
    assert owned[0].metadata == {"k": "v"}


def test_merge_install_status_no_exe_keeps_installed() -> None:
    owned = [Game(app_id=0, store="gog", store_game_id="111", title="A")]
    installed = {"111": {"install_path": "/x/A", "executable": None}}

    merged = merge_install_status(owned, installed, **_GOG_ARGS)[0]

    assert merged.installed is True
    assert merged.install_path == "/x/A"
    assert merged.exe_path is None
