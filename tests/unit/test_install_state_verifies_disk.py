"""``merge_install_status`` must verify install files exist on disk.

A store CLI's ``installed.json`` can outlive the install directory — e.g.
"Delete all data" (or a manual delete) removes the files but the record
survives. Before this guard, the next library sync re-marked the game
``installed`` and Steam showed PLAY for a game with no files (the reported
Vampire Survivors case). Both Epic (legendary) and Amazon (nile) record an
``install_path``; only a path that still exists counts as installed.
"""
from __future__ import annotations

from functools import partial

from unifideck.core.types import Game
from unifideck.stores.shared.install_status import merge_install_status

#: The arguments each store's ``get_library`` passes to the shared merge.
#: Epic takes the defaults; Amazon only renames the path key. Both keep
#: ``verify_dir=True``, which is what this module is about.
epic_merge = merge_install_status
amazon_merge = partial(merge_install_status, path_key="path")


def _owned(store: str, gid: str) -> Game:
    return Game(
        app_id=0, store=store, store_game_id=gid, title=gid, installed=False,
    )


def test_epic_marks_not_installed_when_dir_missing(tmp_path) -> None:
    owned = [_owned("epic", "VS")]
    installed = {"VS": {"app_name": "VS", "install_path": str(tmp_path / "gone")}}

    result = epic_merge(owned, installed)

    assert result[0].installed is False


def test_epic_marks_installed_when_dir_present(tmp_path) -> None:
    game_dir = tmp_path / "VampireSurvivors"
    game_dir.mkdir()
    owned = [_owned("epic", "VS")]
    installed = {"VS": {"app_name": "VS", "install_path": str(game_dir)}}

    result = epic_merge(owned, installed)

    assert result[0].installed is True
    assert result[0].install_path == str(game_dir)


def test_amazon_marks_not_installed_when_dir_missing(tmp_path) -> None:
    owned = [_owned("amazon", "g1")]
    installed = {"g1": {"path": str(tmp_path / "gone")}}

    result = amazon_merge(owned, installed)

    assert result[0].installed is False


def test_amazon_marks_installed_when_dir_present(tmp_path) -> None:
    game_dir = tmp_path / "Clouds"
    game_dir.mkdir()
    owned = [_owned("amazon", "g1")]
    installed = {"g1": {"path": str(game_dir)}}

    result = amazon_merge(owned, installed)

    assert result[0].installed is True
