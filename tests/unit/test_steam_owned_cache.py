"""Round-trip tests for the frontend-supplied owned-Steam-titles cache.

The frontend enumerates the full owned Steam library (installed or not)
and pushes it to the backend; the Ubisoft Steam-linked filter unions it
in. These tests lock the persist/read contract and the normalisation.
"""
from __future__ import annotations

from pathlib import Path

from unifideck.steam import owned_games


def test_save_then_load_round_trips_normalised(tmp_path, monkeypatch):
    monkeypatch.setattr(
        owned_games, "_FRONTEND_CACHE_PATH", tmp_path / "owned.json",
    )
    n = owned_games.save_frontend_owned_titles(
        ["Far Cry® Primal", "Assassin's Creed Odyssey", "Far Cry Primal"],
    )
    # ® stripped + dedup collapses the two Far Cry Primal spellings.
    assert n == 2
    loaded = owned_games.load_frontend_owned_titles()
    assert loaded == frozenset(
        {"far cry primal", "assassins creed odyssey"},
    )


def test_load_missing_cache_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(
        owned_games, "_FRONTEND_CACHE_PATH", tmp_path / "absent.json",
    )
    assert owned_games.load_frontend_owned_titles() == frozenset()


def test_save_ignores_non_strings(tmp_path, monkeypatch):
    monkeypatch.setattr(
        owned_games, "_FRONTEND_CACHE_PATH", tmp_path / "owned.json",
    )
    n = owned_games.save_frontend_owned_titles(
        ["Watch Dogs", None, 123, ""],  # type: ignore[list-item]
    )
    assert n == 1
    assert owned_games.load_frontend_owned_titles() == frozenset({"watch dogs"})
    assert Path(tmp_path / "owned.json").is_file()


def test_save_then_load_games_round_trips_appid(tmp_path, monkeypatch):
    monkeypatch.setattr(
        owned_games, "_FRONTEND_GAMES_CACHE_PATH", tmp_path / "games.json",
    )
    n = owned_games.save_frontend_owned_games(
        [{"title": "Disco Elysium", "appid": 632470}],
    )
    assert n == 1
    assert owned_games.load_frontend_owned_games() == {
        "disco elysium": owned_games.OwnedApp(appid=632470, title="Disco Elysium"),
    }


def test_save_games_ignores_malformed_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(
        owned_games, "_FRONTEND_GAMES_CACHE_PATH", tmp_path / "games.json",
    )
    n = owned_games.save_frontend_owned_games(
        [
            {"title": "Disco Elysium", "appid": 632470},
            {"title": "No Appid"},
            {"appid": 123},
            "not a dict",  # type: ignore[list-item]
            {"title": 42, "appid": 1},
        ],
    )
    assert n == 1
    assert owned_games.load_frontend_owned_games() == {
        "disco elysium": owned_games.OwnedApp(appid=632470, title="Disco Elysium"),
    }


def test_load_frontend_owned_games_preserves_original_title_for_edition_extraction(
    tmp_path, monkeypatch,
):
    """The whole point of storing the original title: extracting an
    edition label from it later (see game_grouping._annotate_steam_owned)."""
    monkeypatch.setattr(
        owned_games, "_FRONTEND_GAMES_CACHE_PATH", tmp_path / "games.json",
    )
    owned_games.save_frontend_owned_games(
        [{"title": "Disco Elysium - The Final Cut", "appid": 632470}],
    )
    loaded = owned_games.load_frontend_owned_games()
    assert loaded["disco elysium the final cut"].title == (
        "Disco Elysium - The Final Cut"
    )


def test_load_frontend_owned_games_back_compat_with_plain_int_cache(
    tmp_path, monkeypatch,
):
    """An older cache wrote ``{title: appid}`` (plain int values) — must
    still load, just without an original title to extract from."""
    games_path = tmp_path / "games.json"
    monkeypatch.setattr(owned_games, "_FRONTEND_GAMES_CACHE_PATH", games_path)
    games_path.write_text(
        '{"updated": 0, "games": {"disco elysium": 632470}}', encoding="utf-8",
    )
    assert owned_games.load_frontend_owned_games() == {
        "disco elysium": owned_games.OwnedApp(appid=632470, title="disco elysium"),
    }


def test_load_missing_games_cache_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(
        owned_games, "_FRONTEND_GAMES_CACHE_PATH", tmp_path / "absent.json",
    )
    assert owned_games.load_frontend_owned_games() == {}


def _write_manifest(steamapps: Path, appid: int, name: str) -> None:
    steamapps.mkdir(parents=True, exist_ok=True)
    (steamapps / f"appmanifest_{appid}.acf").write_text(
        f'"AppState"\n{{\n\t"appid"\t\t"{appid}"\n\t"name"\t\t"{name}"\n}}\n',
        encoding="utf-8",
    )


def test_games_from_library_extracts_title_and_appid(tmp_path):
    steamapps = tmp_path / "steamapps"
    _write_manifest(steamapps, 632470, "Disco Elysium")
    _write_manifest(steamapps, 4112708008, "Fallout 3")

    mapping = owned_games._games_from_library(tmp_path)

    assert mapping == {
        "disco elysium": owned_games.OwnedApp(appid=632470, title="Disco Elysium"),
        "fallout 3": owned_games.OwnedApp(appid=4112708008, title="Fallout 3"),
    }


def test_games_from_library_skips_manifest_missing_appid(tmp_path):
    steamapps = tmp_path / "steamapps"
    steamapps.mkdir(parents=True)
    (steamapps / "appmanifest_1.acf").write_text(
        '"AppState"\n{\n\t"name"\t\t"No AppID Here"\n}\n', encoding="utf-8",
    )

    assert owned_games._games_from_library(tmp_path) == {}


def test_get_all_owned_app_ids_merges_installed_and_frontend(
    tmp_path, monkeypatch,
):
    steam_path = tmp_path / "steam"
    steamapps = steam_path / "steamapps"
    _write_manifest(steamapps, 632470, "Disco Elysium")
    monkeypatch.setattr(owned_games, "find_steam_path", lambda config=None: steam_path)
    owned_games.invalidate_cache()

    monkeypatch.setattr(
        owned_games, "_FRONTEND_GAMES_CACHE_PATH", tmp_path / "games.json",
    )
    owned_games.save_frontend_owned_games(
        [{"title": "Fallout 3", "appid": 4112708008}],
    )

    combined = owned_games.get_all_owned_app_ids()

    assert combined == {
        "disco elysium": owned_games.OwnedApp(appid=632470, title="Disco Elysium"),
        "fallout 3": owned_games.OwnedApp(appid=4112708008, title="Fallout 3"),
    }
    owned_games.invalidate_cache()


def test_get_all_owned_app_ids_frontend_wins_on_overlap(tmp_path, monkeypatch):
    steam_path = tmp_path / "steam"
    steamapps = steam_path / "steamapps"
    _write_manifest(steamapps, 111, "Same Game")
    monkeypatch.setattr(owned_games, "find_steam_path", lambda config=None: steam_path)
    owned_games.invalidate_cache()

    monkeypatch.setattr(
        owned_games, "_FRONTEND_GAMES_CACHE_PATH", tmp_path / "games.json",
    )
    owned_games.save_frontend_owned_games(
        [{"title": "Same Game", "appid": 222}],
    )

    assert owned_games.get_all_owned_app_ids() == {
        "same game": owned_games.OwnedApp(appid=222, title="Same Game"),
    }
    owned_games.invalidate_cache()
