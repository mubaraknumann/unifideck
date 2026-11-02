"""The shared achievements payload, cache, and timestamp parse."""
from __future__ import annotations

from unifideck.stores.shared import achievements_payload as ap
from unifideck.stores.shared.timestamps import parse_timestamp


def _ach(key: str, unlocked: bool, at: float | None = None) -> dict:
    return {"key": key, "name": key, "unlocked": unlocked, "unlocked_at": at}


def test_unlocked_first_newest_on_top_locked_keep_store_order() -> None:
    payload = ap.build_payload("gog", "1", [
        _ach("l1", False), _ach("u_old", True, 10.0),
        _ach("l2", False), _ach("u_new", True, 20.0),
    ])
    assert [a["key"] for a in payload["achievements"]] == ["u_new", "u_old", "l1", "l2"]
    assert payload["total"] == 4
    assert payload["unlocked"] == 2
    assert payload["percent"] == 50.0
    assert payload["store"] == "gog"
    assert payload["game_id"] == "1"


def test_empty_list_is_a_normal_payload() -> None:
    payload = ap.build_payload("microsoft", "X", [])
    assert payload["total"] == 0
    assert payload["percent"] == 0.0
    assert payload["achievements"] == []


def test_unlocked_keys() -> None:
    payload = ap.build_payload("epic", "1", [_ach("a", True, 1.0), _ach("b", False)])
    assert ap.unlocked_keys(payload) == {"a"}


def test_ttl_cache_expires_and_invalidates(monkeypatch) -> None:
    now = [100.0]
    monkeypatch.setattr(ap.time, "monotonic", lambda: now[0])
    cache = ap.TtlCache(ttl=60.0)
    cache.put("g", {"x": 1})
    assert cache.get("g") == {"x": 1}
    now[0] = 161.0
    assert cache.get("g") is None
    cache.put("g", {"x": 2})
    cache.invalidate("g")
    assert cache.get("g") is None
    cache.put("h", {"x": 3})
    cache.clear()
    assert cache.get("h") is None


def test_timestamp_with_seven_fractional_digits() -> None:
    """Xbox Live writes ``…11.9770000Z``; fromisoformat promises six."""
    seven = parse_timestamp("2026-10-07T21:05:11.9770000Z")
    six = parse_timestamp("2026-10-07T21:05:11.977000Z")
    assert seven is not None
    assert seven == six
