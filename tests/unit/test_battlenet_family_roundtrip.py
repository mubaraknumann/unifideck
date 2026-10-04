"""The family code must survive the trip from catalog to launcher.

This is the gap that made every Battle.net game unlaunchable. The family
code (``--exec="launch <FAMILY>"``) is known only inside the catalog join in
``library.build_library``, which puts it on ``Game.metadata["family"]``. The
launcher runs out-of-process under the system Python and cannot reach the
catalog, so it reads the family back out of ``battlenet_id_map.json``.

Nothing wrote it there. ``install_game`` merged only ``prefix_path`` and
``mark_launch_ok`` had no production caller, so ``resolve_family`` returned
``None`` for every uid and both launch handlers aborted with
``battlenetFamilyMissing``. On-device the id map did not exist at all.

Every test here crosses the boundary — backend writer to launcher reader —
because both halves passing in isolation is exactly what shipped.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from unifideck.core.types.domain import Game
from unifideck.launcher.proton.handlers import battlenet_client as client
from unifideck.stores.battlenet.id_map import BattlenetIdMap
from unifideck.stores.battlenet.library import family_updates


@pytest.fixture
def id_map_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the launcher's module-level reader at a temp id map."""
    path = tmp_path / "battlenet_id_map.json"
    monkeypatch.setattr(client, "id_map_path", lambda p=path: p)
    return path


def _game(uid: str, family: str | None) -> Game:
    return Game(
        app_id=1,
        store="battlenet",
        store_game_id=uid,
        title=uid,
        metadata={"family": family} if family is not None else {},
    )


# ── The backend half: what the catalog join hands over ────────────


def test_family_updates_extracts_uid_to_family() -> None:
    updates = family_updates([_game("fenris", "Fen"), _game("s1", "S1")])
    assert updates == {
        "fenris": {"family": "Fen", "client_selects": False},
        "s1": {"family": "S1", "client_selects": False},
    }


def test_a_version_the_client_only_selects_is_recorded_as_such() -> None:
    """The launcher reads the id map and nothing else.

    Without this it treats "the client is showing Classic, waiting for
    Play" as a failed launch.
    """
    game = _game("wow", "WoWC")
    game.metadata["client_selects"] = True
    assert family_updates([game]) == {
        "wow": {"family": "WoWC", "client_selects": True},
    }


def test_a_game_without_a_family_is_skipped_not_written_as_none() -> None:
    """A ``None`` family in the map reads back as "no family known".

    Writing the key with an empty value would look like a recorded fact and
    mask the real problem (a catalog that does not describe the title).
    """
    assert family_updates([_game("mystery", None)]) == {}


def test_merge_many_writes_the_file_once_and_only_when_changed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "battlenet_id_map.json"
    id_map = BattlenetIdMap(path)

    assert id_map.merge_many({"fenris": {"family": "Fen"}}) == 1
    first = path.stat().st_mtime_ns

    # Re-recording the same fact must not rewrite the file: a sync runs on
    # every library refresh and the id map holds prefix paths that cannot
    # be recomputed if a write is ever torn.
    assert id_map.merge_many({"fenris": {"family": "Fen"}}) == 0
    assert path.stat().st_mtime_ns == first


def test_merge_many_preserves_fields_it_is_not_writing(tmp_path: Path) -> None:
    """A sync refreshing families must not drop a recorded prefix path."""
    path = tmp_path / "battlenet_id_map.json"
    id_map = BattlenetIdMap(path)
    id_map.merge("fenris", prefix_path="/games/prefixes/fenris")

    id_map.merge_many({"fenris": {"family": "Fen"}})

    record = id_map.get("fenris")
    assert record is not None
    assert record.family == "Fen"
    assert record.prefix_path == "/games/prefixes/fenris"


# ── The crossing: backend writes, launcher reads ──────────────────


def test_the_launcher_resolves_a_family_the_backend_recorded(
    id_map_path: Path,
) -> None:
    BattlenetIdMap(id_map_path).merge_many(
        family_updates([_game("fenris", "Fen")]),
    )
    assert client.resolve_family("fenris") == "Fen"


def test_the_launcher_reports_no_family_for_an_unknown_uid(
    id_map_path: Path,
) -> None:
    BattlenetIdMap(id_map_path).merge_many({"fenris": {"family": "Fen"}})
    assert client.resolve_family("s1") is None


def test_a_missing_id_map_is_not_an_error(id_map_path: Path) -> None:
    """The launcher must degrade, not crash, before the first sync."""
    assert not id_map_path.exists()
    assert client.resolve_family("fenris") is None
    assert client.resolve_prefix("fenris") is None


# ── The launcher half: recording a proven launch ──────────────────


def test_record_launch_ok_makes_the_family_proven(id_map_path: Path) -> None:
    client.record_launch_ok("fenris", "Fen", 1_700_000_000.0)

    written = json.loads(id_map_path.read_text(encoding="utf-8"))
    assert written["fenris"]["family"] == "Fen"
    assert written["fenris"]["last_launch_family"] == "Fen"
    assert written["fenris"]["launch_ok_at"] == 1_700_000_000.0


def test_a_proven_family_wins_over_a_newer_catalog_one(id_map_path: Path) -> None:
    """The whole reason ``launch_ok_at`` exists.

    Blizzard renamed Diablo IV's family ``D4`` -> ``Fen`` and the client
    accepts a dead code silently. A family that has demonstrably started the
    game is never second-guessed by a catalog refresh.
    """
    client.record_launch_ok("fenris", "Fen", 1_700_000_000.0)
    BattlenetIdMap(id_map_path).merge_many({"fenris": {"family": "D4"}})

    assert client.resolve_family("fenris") == "Fen"


def test_recording_a_launch_preserves_the_recorded_prefix(id_map_path: Path) -> None:
    """The launcher rewrites the whole file, so this is the dangerous one.

    ``prefix_path`` is the one field here that cannot be recomputed — a
    Battle.net game can be installed anywhere, and the resolver never
    guesses from the uid.
    """
    BattlenetIdMap(id_map_path).merge("fenris", prefix_path="/mnt/sd/prefixes/fenris")

    client.record_launch_ok("fenris", "Fen", 1_700_000_000.0)

    assert client.resolve_prefix("fenris") == Path("/mnt/sd/prefixes/fenris")
    reloaded = BattlenetIdMap(id_map_path).get("fenris")
    assert reloaded is not None
    assert reloaded.prefix_path == "/mnt/sd/prefixes/fenris"
    assert reloaded.launch_proven is True


def test_recording_a_launch_does_not_disturb_other_games(id_map_path: Path) -> None:
    id_map = BattlenetIdMap(id_map_path)
    id_map.merge_many(
        {"fenris": {"family": "Fen"}, "s1": {"family": "S1"}},
    )

    client.record_launch_ok("fenris", "Fen", 1_700_000_000.0)

    assert client.resolve_family("s1") == "S1"
