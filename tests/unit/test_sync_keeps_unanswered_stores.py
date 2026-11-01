"""A store that gives no answer keeps only its installed games (sync_finalize_mixin).

A sync used to replace the whole in-memory library with the stores it
fetched, and save that to ``library_cache.json``. A store that was briefly
unavailable (Xbox PC switched off and on while a sync started) lost its
installed games too, so they dropped out of Installed until the next good
sync. Such a store now keeps its installed games and nothing else: a
signed-out store does not keep its library on show.
"""
from __future__ import annotations

from types import SimpleNamespace

from unifideck.core import sync_finalize_mixin as m
from unifideck.core.types import Game


class _Svc(m._SyncFinalizeMixin):
    def __init__(self, stores: list[str], previous: dict[str, list[Game]]) -> None:
        self._registry = SimpleNamespace(all=lambda: [SimpleNamespace(store_name=s) for s in stores])
        self._all_games = previous


def _game(store: str, gid: str, installed: bool = False) -> Game:
    return Game(app_id=0, store=store, store_game_id=gid, title=gid, installed=installed)


def test_unfetched_store_keeps_only_its_installed_games() -> None:
    installed = _game("microsoft", "9P20JCF7BV93", installed=True)
    svc = _Svc(
        ["epic", "microsoft"],
        {"epic": [_game("epic", "old")], "microsoft": [installed, _game("microsoft", "owned")]},
    )
    fresh = {"epic": [_game("epic", "new")]}
    result = svc._with_unanswered_stores(fresh, {})
    assert result["epic"] == fresh["epic"]
    assert result["microsoft"] == [installed]


def test_unfetched_store_with_nothing_installed_is_dropped() -> None:
    svc = _Svc(["microsoft"], {"microsoft": [_game("microsoft", "owned")]})
    assert "microsoft" not in svc._with_unanswered_stores({}, {})


def test_failed_store_with_nothing_keeps_its_installed_games() -> None:
    installed = _game("gog", "a", installed=True)
    svc = _Svc(["gog"], {"gog": [installed, _game("gog", "b")]})
    assert svc._with_unanswered_stores({"gog": []}, {"gog": "timeout"})["gog"] == [installed]


def test_a_store_that_answered_is_taken_at_its_word() -> None:
    svc = _Svc(
        ["gog", "epic"],
        {"gog": [_game("gog", "a", installed=True)], "epic": [_game("epic", "b", installed=True)]},
    )
    partial = [_game("epic", "c")]
    result = svc._with_unanswered_stores({"gog": [], "epic": partial}, {"epic": "page 3 failed"})
    assert result["gog"] == [], "an empty answer without an error is authoritative"
    assert result["epic"] is partial, "a partial answer replaces the old one"


def test_stores_no_longer_registered_are_dropped() -> None:
    svc = _Svc(["epic"], {"epic": [], "removed": [_game("removed", "x", installed=True)]})
    assert "removed" not in svc._with_unanswered_stores({"epic": []}, {})
