"""Launcher toasts must name the game, not the launch key.

Toasts rendered "Starting battlenet:D1 through Battle.net…" because the
launcher had no title to pass: ``games.map`` rows carry exe/work_dir/app_id
and nothing else. ``shortcuts_registry.json`` does record one, since the
reconcile pass needs it to name the Steam entry.

Never raising matters here: this runs in the out-of-process launcher, and a
missing registry must cost a nicer toast, never a launch.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from unifideck.launcher.game_title import registered_title, resolve_title


@pytest.fixture
def registry(tmp_path: Path) -> Path:
    path = tmp_path / "shortcuts_registry.json"
    path.write_text(
        json.dumps({
            "battlenet:D1": {"appid": -93121870, "title": "Diablo"},
            "battlenet:fenris": {"appid": -887644844, "title": "Diablo IV"},
            "ubisoft:abc-123": {"appid": -1, "title": "Rayman Origins"},
            "battlenet:untitled": {"appid": -2},
            "battlenet:blank": {"appid": -3, "title": ""},
        }),
        encoding="utf-8",
    )
    return path


def test_resolves_the_display_title(registry: Path) -> None:
    assert resolve_title("battlenet:D1", registry_path=registry) == "Diablo"
    assert resolve_title("ubisoft:abc-123", registry_path=registry) == "Rayman Origins"


def test_an_unknown_key_falls_back_to_the_key(registry: Path) -> None:
    """"Starting battlenet:x" is poor; "Starting" is broken."""
    assert resolve_title("battlenet:x", registry_path=registry) == "battlenet:x"


@pytest.mark.parametrize("key", ["battlenet:untitled", "battlenet:blank"])
def test_a_missing_or_empty_title_falls_back(registry: Path, key: str) -> None:
    assert resolve_title(key, registry_path=registry) == key


def test_registered_title_is_empty_when_there_is_no_title(registry: Path) -> None:
    assert registered_title("battlenet:D1", registry_path=registry) == "Diablo"
    for key in ("battlenet:x", "battlenet:untitled", "battlenet:blank"):
        assert registered_title(key, registry_path=registry) == ""


def test_a_missing_registry_is_not_an_error(tmp_path: Path) -> None:
    assert resolve_title("battlenet:D1", registry_path=tmp_path / "nope.json") == (
        "battlenet:D1"
    )


def test_a_corrupt_registry_is_not_an_error(tmp_path: Path) -> None:
    path = tmp_path / "shortcuts_registry.json"
    path.write_text("{not json", encoding="utf-8")
    assert resolve_title("battlenet:D1", registry_path=path) == "battlenet:D1"


def test_a_registry_that_is_not_a_mapping_is_not_an_error(tmp_path: Path) -> None:
    path = tmp_path / "shortcuts_registry.json"
    path.write_text("[]", encoding="utf-8")
    assert resolve_title("battlenet:D1", registry_path=path) == "battlenet:D1"
