"""Ubisoft's bootstrap marker is written in the shared ``PrefixMarker`` shape.

Two mechanisms answered "did Unifideck create this prefix?" — Ubisoft's
plaintext lines and Battle.net's JSON — so §3.3 asked for one. The change is
content-only, and the two constraints below are what make it safe. Both can
lose user-visible state if broken, so they are pinned rather than commented.

**The filename never changes.** ``compatdata_scan.MARKER_PREFIXES`` proves
prefix ownership, and it matches ``unifideck_ubisoft_bootstrap.marker``
through its ``unifideck_`` arm only. Rename it and every prefix already on a
user's disk stops reading as ours.

**Legacy plaintext markers must keep working.** Every reader of this marker
tests ``is_file()`` and none parses it, which is what makes the format switch
transparent for prefixes written by an older build. The converse does *not*
hold — ``is_owned_by`` reports False for a legacy marker — so nothing may be
moved onto it until those markers are upgraded in place. That asymmetry is
the trap here, so it is asserted in both directions.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from unifideck.services.shortcut import compatdata_scan
from unifideck.stores.shared import prefix_clone
from unifideck.stores.ubisoft.prefix import helpers as ubi_helpers

MARKER = "unifideck_ubisoft_bootstrap.marker"


def _helpers() -> ubi_helpers._PrefixHelpers:
    parent = MagicMock()
    parent._config.bootstrap_marker = MARKER
    return ubi_helpers._PrefixHelpers(parent)


def test_the_marker_is_json_in_the_shared_shape(tmp_path: Path) -> None:
    _helpers().write_bootstrap_marker(str(tmp_path), "cloned_from_template", "83")

    data = json.loads((tmp_path / MARKER).read_text(encoding="utf-8"))
    assert data["store"] == "ubisoft"
    assert data["source"] == "cloned_from_template"
    assert data["game_id"] == "83"
    assert isinstance(data["created_at"], (int, float))


def test_the_filename_did_not_change(tmp_path: Path) -> None:
    """Ownership is proved by the name, and every existing prefix has it."""
    _helpers().write_bootstrap_marker(str(tmp_path), "template", None)

    assert (tmp_path / MARKER).is_file()
    assert any(
        MARKER.startswith(prefix) for prefix in compatdata_scan.MARKER_PREFIXES
    )


def test_a_template_marker_carries_no_game(tmp_path: Path) -> None:
    """A template or auth prefix belongs to no game; don't invent one."""
    _helpers().write_bootstrap_marker(str(tmp_path), "template_from_auth", None)

    data = json.loads((tmp_path / MARKER).read_text(encoding="utf-8"))
    assert data["game_id"] is None


def test_the_marker_round_trips_through_the_shared_reader(
    tmp_path: Path,
) -> None:
    _helpers().write_bootstrap_marker(str(tmp_path), "fresh_install", "83")

    marker = prefix_clone.read_marker(tmp_path, MARKER)

    assert marker is not None
    assert marker.store == "ubisoft"
    assert marker.game_id == "83"
    assert prefix_clone.is_owned_by(tmp_path, MARKER, "ubisoft")


def test_a_legacy_plaintext_marker_still_satisfies_every_reader(
    tmp_path: Path,
) -> None:
    """The upgrade path: a prefix written by an older build.

    All five Ubisoft readers ask ``is_file()``. If that ever stops being
    true, installed games silently drop back to Not Installed — the
    ``library/detection.py`` scan skips a prefix with no marker.
    """
    legacy = tmp_path / MARKER
    legacy.write_text(
        "cloned_from_template\ngame=83\ncreated=2026-08-01T10:00:00+00:00\n",
        encoding="utf-8",
    )

    assert legacy.is_file()


def test_a_legacy_marker_is_not_owned_by_the_strict_test(
    tmp_path: Path,
) -> None:
    """Why no Ubisoft path may move onto ``is_owned_by`` yet.

    An unparseable marker reads back as ``store=""``, so the strict test
    says "not ours" for every prefix predating the JSON switch. That is the
    safe direction for deletion and the wrong one for detection — which is
    exactly why the readers stay on ``is_file()``.
    """
    legacy = tmp_path / MARKER
    legacy.write_text("cloned_from_template\ngame=83\n", encoding="utf-8")

    marker = prefix_clone.read_marker(tmp_path, MARKER)

    assert marker is not None          # still recognised as a marker we wrote
    assert marker.store == ""          # but not attributable to a store
    assert not prefix_clone.is_owned_by(tmp_path, MARKER, "ubisoft")


def test_battlenets_markers_are_unaffected_by_the_new_field(
    tmp_path: Path,
) -> None:
    """``game_id`` defaults to None, so the other consumer writes as before."""
    prefix_clone.write_marker(
        tmp_path,
        ".unifideck_battlenet",
        prefix_clone.PrefixMarker(
            store="battlenet", created_at=1.0, source="/tmp/template",
        ),
    )

    data = json.loads(
        (tmp_path / ".unifideck_battlenet").read_text(encoding="utf-8"),
    )
    assert data["store"] == "battlenet"
    assert data["game_id"] is None
    assert prefix_clone.is_owned_by(tmp_path, ".unifideck_battlenet", "battlenet")
