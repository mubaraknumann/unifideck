"""User overrides for the probe mappings — audit register item 47.

``FeatureFlagService`` and ``ProbeReactionService`` each held a
byte-identical copy of this merge, and one cited the other in a comment
rather than sharing it. These tests pin the forgiveness contract, which is
the whole reason the function is shaped the way it is: ``~/.config/
unifideck/config.json`` is hand-editable, and a typo in it must degrade to
the built-in defaults rather than take a service constructor down at boot.
"""
from __future__ import annotations

from typing import Any

import pytest

from unifideck.utils.config_helpers import merge_str_list_mapping

DEFAULTS: dict[str, list[str]] = {
    "gamescope": ["wsi_workaround"],
    "vulkan32": ["battlenet_client"],
}
KEY = "probes.probe_to_handlers"


class _Cfg:
    def __init__(self, value: Any, *, raises: bool = False) -> None:
        self._value = value
        self._raises = raises

    def get(self, key: str, default: Any = None) -> Any:
        if self._raises:
            raise RuntimeError("schema drift")
        return self._value if key == KEY else default


# ── the merge ───────────────────────────────────────────────────────
def test_an_override_replaces_one_entry_and_leaves_the_rest() -> None:
    cfg = _Cfg({"gamescope": ["custom"]})

    assert merge_str_list_mapping(cfg, KEY, DEFAULTS) == {
        "gamescope": ["custom"],
        "vulkan32": ["battlenet_client"],
    }


def test_an_override_may_add_a_key_the_defaults_lack() -> None:
    cfg = _Cfg({"new_probe": ["handler"]})

    assert merge_str_list_mapping(cfg, KEY, DEFAULTS)["new_probe"] == ["handler"]


def test_the_defaults_are_never_mutated() -> None:
    """Both callers hold their defaults as a module-level constant."""
    merge_str_list_mapping(_Cfg({"gamescope": ["custom"]}), KEY, DEFAULTS)

    assert DEFAULTS == {
        "gamescope": ["wsi_workaround"],
        "vulkan32": ["battlenet_client"],
    }


def test_an_empty_list_is_a_valid_override() -> None:
    """"Run no handlers for this probe" is a legitimate thing to configure,
    and must not be mistaken for a missing value."""
    cfg = _Cfg({"gamescope": []})

    assert merge_str_list_mapping(cfg, KEY, DEFAULTS)["gamescope"] == []


# ── malformed input falls back, at three granularities ──────────────
def test_one_bad_entry_does_not_discard_its_well_formed_siblings() -> None:
    """The reason validation is per key rather than per document.

    A single typo in ``config.json`` must not silently drop every other
    override the user wrote.
    """
    cfg = _Cfg({"gamescope": ["ok"], "vulkan32": "not-a-list"})
    merged = merge_str_list_mapping(cfg, KEY, DEFAULTS)

    assert merged["gamescope"] == ["ok"]
    assert merged["vulkan32"] == ["battlenet_client"], "fell back to the default"


@pytest.mark.parametrize("bad", ["str", 5, None, ["a"], object()])
def test_a_non_dict_override_yields_the_defaults(bad: Any) -> None:
    assert merge_str_list_mapping(_Cfg(bad), KEY, DEFAULTS) == DEFAULTS


@pytest.mark.parametrize("bad", [["a", 3], [None], "abc", {}, 7])
def test_a_value_that_is_not_a_list_of_strings_is_skipped(bad: Any) -> None:
    """A half-numeric list is malformed, not something to coerce."""
    cfg = _Cfg({"gamescope": bad})

    assert merge_str_list_mapping(cfg, KEY, DEFAULTS) == DEFAULTS


# ── never raise out of a constructor ────────────────────────────────
def test_no_config_yields_the_defaults() -> None:
    assert merge_str_list_mapping(None, KEY, DEFAULTS) == DEFAULTS


def test_a_config_without_a_get_yields_the_defaults() -> None:
    assert merge_str_list_mapping(object(), KEY, DEFAULTS) == DEFAULTS


def test_a_raising_config_yields_the_defaults() -> None:
    """Both services build this in ``__init__``; an exception here would
    fail service construction at boot."""
    assert merge_str_list_mapping(_Cfg(None, raises=True), KEY, DEFAULTS) == DEFAULTS
