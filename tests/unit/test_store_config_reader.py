"""One forgiving config reader for every store — audit register item 47.

GOG and Microsoft each defined the same three coercions as nested closures
inside their ``from_config_manager``, byte-identical apart from the prefix
constant they captured. Check 13 caught ``_list``; ``_s`` and ``_i`` sat
under its body-size floor, which is what that floor costs.

Forgiveness is the point, not a convenience. ``~/.config/unifideck/
config.json`` is user-editable, and a typo in it must not take the plugin
down — audit §1.2 found that a malformed config was already *completely*
silent, so the failure mode to avoid is the opposite one: raising out of a
store constructor at boot.
"""
from __future__ import annotations

from typing import Any

import pytest

from unifideck.stores.shared.config_reader import StoreConfigReader


class _Cfg:
    """The slice of ConfigManager these reads touch."""

    def __init__(self, values: dict[str, Any]) -> None:
        self._values = values

    def get(self, key: str, default: Any = None) -> Any:
        return self._values.get(key, default)


def _reader(values: dict[str, Any]) -> StoreConfigReader:
    return StoreConfigReader(_Cfg(values), "stores.gog")


# ── text ────────────────────────────────────────────────────────
def test_text_reads_and_strips() -> None:
    assert _reader({"stores.gog.client_id": "  abc  "}).text("client_id") == "abc"


def test_text_falls_back_when_absent() -> None:
    assert _reader({}).text("client_id", "fallback") == "fallback"


def test_a_null_value_yields_the_default_not_the_string_none() -> None:
    """``str(None)`` is ``"None"``, which would sail through as a real value."""
    assert _reader({"stores.gog.client_id": None}).text("client_id", "d") == "d"


# ── number ──────────────────────────────────────────────────────
@pytest.mark.parametrize("raw", ["7", 7, 7.9])
def test_number_coerces_what_it_can(raw: Any) -> None:
    assert _reader({"stores.gog.timeout": raw}).number("timeout", 1) == 7


@pytest.mark.parametrize("raw", ["abc", None, [], {}])
def test_number_falls_back_on_anything_uncoercible(raw: Any) -> None:
    assert _reader({"stores.gog.timeout": raw}).number("timeout", 42) == 42


# ── text_list ───────────────────────────────────────────────────
def test_text_list_keeps_non_empty_strings() -> None:
    values = {"stores.gog.uris": ["a", "", "b"]}
    assert _reader(values).text_list("uris") == ["a", "b"]


def test_text_list_drops_non_strings_rather_than_coercing() -> None:
    """A half-numeric list is malformed config.

    Inventing ``"3"`` as a redirect URI would be worse than ignoring it —
    it would be sent to an OAuth endpoint.
    """
    assert _reader({"stores.gog.uris": ["a", 3, None]}).text_list("uris") == ["a"]


@pytest.mark.parametrize("raw", ["not-a-list", 5, None, {}])
def test_text_list_yields_empty_for_a_non_list(raw: Any) -> None:
    assert _reader({"stores.gog.uris": raw}).text_list("uris") == []


# ── no config at all ────────────────────────────────────────────
def test_every_read_survives_a_missing_config_manager() -> None:
    """Stores construct before config exists.

    ``build_service_subset`` in the out-of-process launcher builds a reduced
    graph, and a store that raised here would take the launch with it.
    """
    reader = StoreConfigReader(None, "stores.gog")
    assert reader.text("k", "d") == "d"
    assert reader.number("k", 9) == 9
    assert reader.text_list("k") == []


def test_the_prefix_scopes_the_lookup() -> None:
    """Two stores reading the same key name must not collide."""
    values = {"stores.gog.client_id": "gog", "stores.microsoft.client_id": "ms"}
    assert StoreConfigReader(_Cfg(values), "stores.gog").text("client_id") == "gog"
    assert (
        StoreConfigReader(_Cfg(values), "stores.microsoft").text("client_id") == "ms"
    )
