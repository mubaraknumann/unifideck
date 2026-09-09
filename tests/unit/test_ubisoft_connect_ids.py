"""Tests for leveldb ``ubisoftConnectGameId`` extraction and its wiring.

Ported from staging's ``_extract_cache_game_ids`` (staging ``ubisoft.py``
~L1713-1796). Three surfaces are covered:

1. ``extract_cache_game_ids`` / ``_extract_ids_from_binary`` parse both
   key orderings out of a leveldb blob.
2. ``_GameBuilder`` records the connect id on the id_map entry so
   ``resolve_launch_id`` can prefer it.
3. The launcher handler resolves ``UPLAY_ID`` from the persisted id_map
   when the env var is absent (the realistic case — Steam can't pass it).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from unifideck.stores.ubisoft.id_map import UbisoftIdMap
from unifideck.stores.ubisoft.leveldb_ids import (
    _extract_ids_from_binary,
    drop_conflicting_ids,
    extract_cache_game_ids,
)
from unifideck.stores.ubisoft.library.game_builder import _GameBuilder
from unifideck.stores.ubisoft.parser import GameConfig

_SPACE = "abcd1234-5678-90ab-cdef-1234567890ab"
# Issue #436: Avatar was handed Star Wars Outlaws' deeplink id (17903)
# and launched Outlaws. Avatar's own id, from its registry, is 4740.
_AVATAR = "fe3cdbe6-9a54-47db-9e2f-6395fd922640"
_OUTLAWS = "69488c50-c7b7-4460-b502-66a973e02150"


def _norm(name: str) -> str:
    return UbisoftIdMap._normalize_for_matching(name)


class _IdMap:
    def __init__(self) -> None:
        self.bulk: dict[str, dict[str, Any]] = {}

    def normalize_for_matching(self, name: str) -> str:
        return _norm(name)

    def update_bulk(self, mapping: dict[str, dict[str, Any]]) -> None:
        self.bulk.update(mapping)

    def reconcile_connect_ids(
        self, fresh: dict[str, str], space_ids: list[str],
    ) -> None:
        for space_id in space_ids:
            entry = self.bulk.setdefault(space_id, {})
            connect_id = fresh.get(space_id)
            if connect_id:
                entry["ubisoftconnect_game_id"] = connect_id
                entry["ubisoftconnect_game_id_source"] = "leveldb"
            else:
                entry.pop("ubisoftconnect_game_id", None)


def _cfg(install_id: int, space_id: str, name: str) -> GameConfig:
    c = GameConfig()
    c.install_id = install_id
    c.launch_id = install_id
    c.space_id = space_id
    c.name = name
    return c


# ── binary parsing ────────────────────────────────────────────────


def test_extract_ids_space_then_connect():
    blob = (
        b'junk{"spaceId":"' + _SPACE.encode() + b'","name":"X",'
        b'"ubisoftConnectGameId":4242}tail'
    )
    out: dict[str, str] = {}
    _extract_ids_from_binary(blob, out)
    assert out == {_SPACE: "4242"}


def test_extract_ids_connect_then_space():
    blob = (
        b'{"ubisoftConnectGameId":777,"spaceId":"' + _SPACE.encode() + b'"}'
    )
    out: dict[str, str] = {}
    _extract_ids_from_binary(blob, out)
    assert out == {_SPACE: "777"}


def test_extract_ids_first_wins():
    blob = (
        b'"spaceId":"' + _SPACE.encode() + b'""ubisoftConnectGameId":111'
        b'"spaceId":"' + _SPACE.encode() + b'""ubisoftConnectGameId":222'
    )
    out: dict[str, str] = {}
    _extract_ids_from_binary(blob, out)
    assert out == {_SPACE: "111"}


def test_extract_cache_game_ids_reads_leveldb(tmp_path: Path):
    leveldb = tmp_path / "ls" / "leveldb"
    leveldb.mkdir(parents=True)
    (leveldb / "000005.ldb").write_bytes(
        b'"spaceId":"' + _SPACE.encode() + b'""ubisoftConnectGameId":9001',
    )
    result = extract_cache_game_ids(str(tmp_path), "ls")
    assert result == {_SPACE: "9001"}


def test_extract_cache_game_ids_missing_dir(tmp_path: Path):
    assert extract_cache_game_ids(str(tmp_path), "nope") == {}


# ── #436: ids must never pair across record boundaries ────────────


def _records_blob(records: list[dict[str, Any]]) -> bytes:
    """A UPC-shaped localStorage value: one JSON array of game records."""
    return ('\x01{"games":' + json.dumps(records) + "}").encode()


def test_ids_do_not_shift_when_connect_id_precedes_space_id():
    """The reported failure: every record borrowed its neighbour's id.

    With ``ubisoftConnectGameId`` serialised before ``spaceId``, the old
    proximity scan paired Avatar's space id with Outlaws' id and both
    games ended up on 17903.
    """
    blob = _records_blob(
        [
            {"ubisoftConnectGameId": 4740, "name": "Avatar", "spaceId": _AVATAR},
            {"ubisoftConnectGameId": 17903, "name": "Outlaws", "spaceId": _OUTLAWS},
        ],
    )
    out: dict[str, str] = {}
    _extract_ids_from_binary(blob, out)
    assert out == {_AVATAR: "4740", _OUTLAWS: "17903"}


def test_record_without_an_id_does_not_borrow_the_next_one():
    blob = _records_blob(
        [
            {"spaceId": _AVATAR, "name": "Avatar"},
            {"spaceId": _OUTLAWS, "name": "Outlaws", "ubisoftConnectGameId": 17903},
        ],
    )
    out: dict[str, str] = {}
    _extract_ids_from_binary(blob, out)
    assert out == {_OUTLAWS: "17903"}


def test_nested_object_donates_its_id_to_the_enclosing_record():
    blob = (
        b'{"spaceId":"' + _AVATAR.encode() + b'","meta":{"ubisoftConnectGameId":4740}}'
    )
    out: dict[str, str] = {}
    _extract_ids_from_binary(blob, out)
    assert out == {_AVATAR: "4740"}


def test_nul_record_separator_blocks_pairing():
    blob = (
        b'{"spaceId":"' + _AVATAR.encode() + b'"\x00'
        b'{"ubisoftConnectGameId":17903,"spaceId":"' + _OUTLAWS.encode() + b'"}'
    )
    out: dict[str, str] = {}
    _extract_ids_from_binary(blob, out)
    assert out == {_OUTLAWS: "17903"}


def test_truncated_leading_brace_still_pairs():
    """leveldb blobs are framed, so a record's opening brace can be lost."""
    blob = (
        b'"spaceId":"' + _AVATAR.encode() + b'","ubisoftConnectGameId":4740}'
        b'{"spaceId":"' + _OUTLAWS.encode() + b'","ubisoftConnectGameId":17903}'
    )
    out: dict[str, str] = {}
    _extract_ids_from_binary(blob, out)
    assert out == {_AVATAR: "4740", _OUTLAWS: "17903"}


def test_drop_conflicting_ids_discards_a_shared_id():
    assert drop_conflicting_ids({_AVATAR: "17903", _OUTLAWS: "17903"}) == {}


def test_drop_conflicting_ids_keeps_distinct_ids():
    mapping = {_AVATAR: "4740", _OUTLAWS: "17903"}
    assert drop_conflicting_ids(mapping) == mapping


# ── builder records the connect id ────────────────────────────────


def test_builder_records_connect_id():
    builder = _GameBuilder(config=object(), id_map=_IdMap())
    cfg = _cfg(1, _SPACE, "Some Game")
    builder.build_games_from_configs(
        [cfg], installed={}, connect_ids={_SPACE: "5555"},
    )
    assert builder._id_map.bulk[_SPACE]["ubisoftconnect_game_id"] == "5555"


def test_builder_omits_connect_id_when_absent():
    builder = _GameBuilder(config=object(), id_map=_IdMap())
    cfg = _cfg(1, _SPACE, "Some Game")
    builder.build_games_from_configs([cfg], installed={}, connect_ids={})
    assert "ubisoftconnect_game_id" not in builder._id_map.bulk[_SPACE]


# ── #436: source precedence and self-heal on the persisted map ────


def _id_map(cache: dict[str, dict[str, Any]]) -> UbisoftIdMap:
    """A real ``UbisoftIdMap`` with a pre-seeded, non-persisting cache."""
    idmap = UbisoftIdMap.__new__(UbisoftIdMap)
    idmap._cache = cache
    idmap._save = lambda: None  # type: ignore[method-assign]
    return idmap


def test_leveldb_id_cannot_displace_a_registry_id():
    idmap = _id_map(
        {
            _AVATAR: {
                "ubisoftconnect_game_id": "4740",
                "ubisoftconnect_game_id_source": "registry",
            },
        },
    )

    assert idmap.set_connect_id(_AVATAR, "17903", "leveldb") is False
    assert idmap.get_entry(_AVATAR)["ubisoftconnect_game_id"] == "4740"


def test_registry_id_displaces_a_leveldb_id():
    idmap = _id_map(
        {
            _AVATAR: {
                "ubisoftconnect_game_id": "17903",
                "ubisoftconnect_game_id_source": "leveldb",
            },
        },
    )

    assert idmap.set_connect_id(_AVATAR, "4740", "registry") is True
    entry = idmap.get_entry(_AVATAR)
    assert entry["ubisoftconnect_game_id"] == "4740"
    assert entry["ubisoftconnect_game_id_source"] == "registry"


def test_untagged_id_is_treated_as_untrusted():
    """Entries written before #436 carry no source and must be correctable."""
    idmap = _id_map({_AVATAR: {"ubisoftconnect_game_id": "17903"}})

    assert idmap.set_connect_id(_AVATAR, "4740", "leveldb") is True
    assert idmap.get_entry(_AVATAR)["ubisoftconnect_game_id"] == "4740"


def test_sweep_keeps_the_id_on_the_game_that_corroborates_it():
    idmap = _id_map(
        {
            _AVATAR: {"install_id": "4740", "ubisoftconnect_game_id": "17903"},
            _OUTLAWS: {"install_id": "17903", "ubisoftconnect_game_id": "17903"},
        },
    )

    assert idmap.sweep_conflicting_connect_ids() == 1
    assert "ubisoftconnect_game_id" not in idmap.get_entry(_AVATAR)
    assert idmap.get_entry(_OUTLAWS)["ubisoftconnect_game_id"] == "17903"


def test_sweep_strips_an_uncorroborated_id_from_every_claimant():
    idmap = _id_map(
        {
            _AVATAR: {"ubisoftconnect_game_id": "17903"},
            _OUTLAWS: {"ubisoftconnect_game_id": "17903"},
        },
    )

    assert idmap.sweep_conflicting_connect_ids() == 2
    assert "ubisoftconnect_game_id" not in idmap.get_entry(_AVATAR)
    assert "ubisoftconnect_game_id" not in idmap.get_entry(_OUTLAWS)


def test_sweep_leaves_distinct_ids_alone():
    idmap = _id_map(
        {
            _AVATAR: {"ubisoftconnect_game_id": "4740"},
            _OUTLAWS: {"ubisoftconnect_game_id": "17903"},
        },
    )

    assert idmap.sweep_conflicting_connect_ids() == 0


def test_reconcile_drops_an_id_the_fresh_scan_no_longer_supports():
    idmap = _id_map(
        {
            _AVATAR: {
                "ubisoftconnect_game_id": "17903",
                "ubisoftconnect_game_id_source": "leveldb",
            },
        },
    )

    idmap.reconcile_connect_ids({}, [_AVATAR])

    assert "ubisoftconnect_game_id" not in idmap.get_entry(_AVATAR)


def test_reconcile_keeps_a_registry_id_through_a_force_sync():
    idmap = _id_map(
        {
            _AVATAR: {
                "ubisoftconnect_game_id": "4740",
                "ubisoftconnect_game_id_source": "registry",
            },
        },
    )

    idmap.reconcile_connect_ids({_AVATAR: "17903"}, [_AVATAR])

    assert idmap.get_entry(_AVATAR)["ubisoftconnect_game_id"] == "4740"


# ── launcher handler resolves UPLAY_ID from the id_map ────────────


def test_handler_resolves_uplay_id_from_id_map(tmp_path, monkeypatch):
    from unifideck.launcher.proton.handlers import ubisoft as handler

    id_map_file = tmp_path / "ubisoft_id_map.json"
    id_map_file.write_text(
        json.dumps(
            {_SPACE: {"launch_id": "100", "ubisoftconnect_game_id": "9001"}},
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(handler, "ID_MAP_FILE", id_map_file)
    # prefers connect id over launch_id
    assert handler._uplay_id_from_id_map(_SPACE) == "9001"


def test_handler_falls_back_to_launch_id(tmp_path, monkeypatch):
    from unifideck.launcher.proton.handlers import ubisoft as handler

    id_map_file = tmp_path / "ubisoft_id_map.json"
    id_map_file.write_text(
        json.dumps({_SPACE: {"launch_id": "100", "install_id": "50"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(handler, "ID_MAP_FILE", id_map_file)
    assert handler._uplay_id_from_id_map(_SPACE) == "100"


def test_handler_returns_none_when_unknown(tmp_path, monkeypatch):
    from unifideck.launcher.proton.handlers import ubisoft as handler

    monkeypatch.setattr(handler, "ID_MAP_FILE", tmp_path / "missing.json")
    assert handler._uplay_id_from_id_map(_SPACE) is None
