"""Tests for ``stores.gamevault.variants`` — the facts that tell same-title
builds apart and the rank that prefers the Linux one."""
from __future__ import annotations

import pytest

from unifideck.stores.gamevault.filename import extract_tags, parse_archive_name
from unifideck.stores.gamevault.variants import variant_metadata


def _meta(path):
    return variant_metadata(parse_archive_name(path), extract_tags(path))


def test_linux_build_with_tag_and_version():
    assert _meta("Super Mario 64 [Ghostship] (v1.5.0) (L_P).zip") == {
        "disambiguators": ["Linux", "Ghostship", "v1.5.0"],
        "default_rank": 0,
    }


def test_windows_build_ranks_below_linux():
    assert _meta("Game (W_P).zip") == {
        "disambiguators": ["Windows"],
        "default_rank": 1,
    }


def test_linux_software_token_also_ranks_first():
    assert _meta("Game (L_SW).zip")["default_rank"] == 0


def test_setup_windows_token_is_windows():
    assert _meta("Game (W_S).zip")["disambiguators"] == ["Windows"]


def test_no_facts_gives_rank_only():
    assert _meta("Game.zip") == {"default_rank": 1}


def test_tags_join_into_one_entry():
    assert _meta("Game [A] [B] (L_P).zip")["disambiguators"] == ["Linux", "A, B"]


@pytest.mark.parametrize(
    ("path", "entries"),
    [("Game (v2).zip", ["v2"]), ("Game [X].zip", ["X"])],
)
def test_no_platform_entry_without_token(path, entries):
    assert _meta(path) == {"disambiguators": entries, "default_rank": 1}
