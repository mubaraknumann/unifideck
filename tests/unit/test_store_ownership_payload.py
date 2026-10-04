"""What the frontend may send, and what the store page is told to draw.

The ribbon runs inside the logged-in Steam Store origin, so the payload is
built from validated pieces only. The wording rule matters as much as the
validation: a subscription row (Game Pass, mixed with Play Anywhere
purchases) is "playable", never "owned".
"""
from __future__ import annotations

from typing import Any

import pytest

from unifideck.core.cross_store_ownership import OwnedCopy
from unifideck.rpc.mixins._store_ownership_payload import (
    RibbonStrings,
    build_ribbon_payload,
    edition_detail,
    parse_steam_app_id,
    purchase_notes,
    read_steam_name,
    sanitize_ribbon_strings,
)

STRINGS: dict[str, Any] = {
    "tag_owned": "Owned",
    "tag_cloud": "Cloud",
    "message_owned": "You already own this game on:",
    "message_cloud": "Playable via Xbox Cloud Gaming",
    "installed": "Installed",
    "via": "via Unifideck",
    "dir": "ltr",
    "store_labels": {"gog": "GOG", "amazon": "Amazon Games", "microsoft": "Xbox"},
}


def _strings(**overrides: Any) -> RibbonStrings:
    parsed = sanitize_ribbon_strings({**STRINGS, **overrides})
    assert parsed is not None
    return parsed


# ── appid parsing ───────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (257350, 257350),
        ("257350", 257350),
        (True, None),
        ("abc", None),
        ("-5", None),
        (0, None),
        (-1, None),
        (2_000_000_001, None),  # shortcut AppID range, never a store page
        (None, None),
        (257350.0, None),
    ],
)
def test_parse_steam_app_id(raw: Any, expected: int | None) -> None:
    assert parse_steam_app_id(raw) == expected


# ── string sanitising ───────────────────────────────────────────────
@pytest.mark.parametrize("missing", ["tag_owned", "message_cloud", "via"])
def test_a_missing_required_string_draws_nothing(missing: str) -> None:
    raw = {k: v for k, v in STRINGS.items() if k != missing}

    assert sanitize_ribbon_strings(raw) is None


def test_non_string_or_blank_values_are_rejected() -> None:
    assert sanitize_ribbon_strings({**STRINGS, "installed": 3}) is None
    assert sanitize_ribbon_strings({**STRINGS, "installed": "   "}) is None
    assert sanitize_ribbon_strings("not a dict") is None
    assert sanitize_ribbon_strings(None) is None


def test_long_values_are_capped_and_direction_is_coerced() -> None:
    parsed = _strings(message_owned="x" * 500, dir="sideways")

    assert len(parsed.message_owned) == 160
    assert parsed.direction == "ltr"
    assert _strings(dir="rtl").direction == "rtl"


def test_store_labels_keep_only_string_pairs() -> None:
    parsed = _strings(store_labels={"gog": "GOG", "epic": 7, 3: "x", "itch": " "})

    assert parsed.store_labels == {"gog": "GOG"}


# ── edition detail ──────────────────────────────────────────────────
def test_detail_is_empty_when_the_titles_only_differ_in_punctuation() -> None:
    assert edition_detail(("baldurs gate ii™",), "Baldur's Gate II") == ""


def test_detail_names_a_different_edition() -> None:
    assert (
        edition_detail(("Baldur's Gate II: Enhanced Edition",), "Baldur's Gate II")
        == "Baldur's Gate II: Enhanced Edition"
    )


def test_detail_is_empty_without_a_steam_name() -> None:
    assert edition_detail(("Anything",), "") == ""


def test_read_steam_name_from_the_metadata_cache() -> None:
    class _Store:
        _data = {"257350": {"name": " Baldur's Gate II: Enhanced Edition "}}

    class _Cache:
        _stores = {"steam_metadata": _Store()}

    assert read_steam_name(_Cache(), 257350) == "Baldur's Gate II: Enhanced Edition"
    assert read_steam_name(_Cache(), 1) == ""
    assert read_steam_name(None, 257350) == ""


# ── payload ─────────────────────────────────────────────────────────
def test_purchases_only_payload() -> None:
    copies = [
        OwnedCopy("amazon", ("Baldur's Gate II: Enhanced Edition",), True, False),
        OwnedCopy("gog", ("Baldur's Gate II: Enhanced Edition",), False, False),
    ]

    payload = build_ribbon_payload(257350, copies, _strings(), "Baldur's Gate II: Enhanced Edition")

    assert payload["appid"] == 257350
    assert payload["overlay"] == {"tag": "Owned", "text": "Amazon Games · GOG"}
    assert payload["sections"] == [{
        "kind": "owned",
        "tag": "Owned",
        "message": "You already own this game on:",
        "chips": [
            {"label": "Amazon Games", "detail": "", "installed": True, "icon": None, "notes": []},
            {"label": "GOG", "detail": "", "installed": False, "icon": None, "notes": []},
        ],
    }]
    assert payload["installed"] == "Installed"
    assert payload["via"] == "via Unifideck"


def test_subscription_only_payload_never_says_owned() -> None:
    copies = [OwnedCopy("microsoft", ("Cyberpunk 2077",), False, True)]

    payload = build_ribbon_payload(1091500, copies, _strings(), "Cyberpunk 2077")

    # The capsule strip is narrow in Gaming Mode: tag + the service name,
    # never a sentence (one was cut to "Included with Game P…"), and never
    # the bare store name, which read as owned.
    assert payload["overlay"] == {"tag": "Cloud", "text": "Playable via Xbox Cloud Gaming"}
    assert [s["kind"] for s in payload["sections"]] == ["cloud"]
    assert payload["sections"][0]["chips"] == []  # same title → nothing to add


def test_mixed_payload_keeps_the_cloud_line_separate() -> None:
    copies = [
        OwnedCopy("ubisoft", ("Far Cry 3 Blood Dragon",), False, False),
        OwnedCopy("microsoft", ("Far Cry 3 Blood Dragon Classic Edition",), False, True),
    ]

    payload = build_ribbon_payload(233270, copies, _strings(), "Far Cry 3 Blood Dragon")

    assert payload["overlay"]["text"] == "ubisoft"  # no label sent → store id
    owned, cloud = payload["sections"]
    assert owned["kind"] == "owned"
    assert cloud == {
        "kind": "cloud",
        "tag": "Cloud",
        "message": "Playable via Xbox Cloud Gaming",
        "chips": [{
            "label": "",
            "detail": "Far Cry 3 Blood Dragon Classic Edition",
            "installed": False,
            "icon": None,
        }],
    }


def test_rtl_direction_reaches_the_page() -> None:
    copies = [OwnedCopy("gog", ("X",), False, False)]

    assert build_ribbon_payload(1, copies, _strings(dir="rtl"), "X")["dir"] == "rtl"


def test_store_logos_reach_their_chips_after_sanitising() -> None:
    epic = {"viewBox": "0 0 24 24", "nodes": [{"tag": "path", "attrs": {"d": "M0 0Z"}}]}
    hostile = {"viewBox": "0 0 24 24", "nodes": [{"tag": "script", "attrs": {}}]}
    strings = _strings(store_icons={"epic": epic, "gog": hostile})
    copies = [
        OwnedCopy("epic", ("Hogwarts Legacy",), False, False),
        OwnedCopy("gog", ("Hogwarts Legacy",), False, False),
        OwnedCopy("microsoft", ("Hogwarts Legacy Xbox One Version",), False, True),
    ]

    payload = build_ribbon_payload(990080, copies, strings, "Hogwarts Legacy")

    owned, cloud = payload["sections"]
    assert [c["icon"] for c in owned["chips"]] == [epic, None]  # gog's logo did not survive
    assert cloud["chips"][0]["icon"] is None  # no Xbox logo was sent
    assert strings.store_icons == {"epic": epic}



# ── notes on an indexed (Xbox) purchase ─────────────────────────────
_NOTES = {
    "pc": "PC", "console": "Console", "pc_console": "PC + Console",
    "play_anywhere": "Play Anywhere", "gold": "Gold", "cloud": "Cloud",
}


@pytest.mark.parametrize(("copy", "notes"), [
    # Play Anywhere already covers the cloud: no second note.
    (OwnedCopy("microsoft", ("X",), False, False, platform="play_anywhere", streams=True),
     ["Play Anywhere"]),
    # A console-only purchase that also streams: "Cloud" is new information.
    (OwnedCopy("microsoft", ("X",), False, False, platform="console", streams=True),
     ["Console", "Cloud"]),
    (OwnedCopy("microsoft", ("X",), False, False, platform="console", gold=True), ["Console", "Gold"]),
    (OwnedCopy("microsoft", ("X",), False, False, platform="pc"), ["PC"]),
    # A store with no purchase index (GOG) gets no notes, even if it streams.
    (OwnedCopy("gog", ("X",), False, False, streams=True), []),
])
def test_purchase_notes(copy: OwnedCopy, notes: list[str]) -> None:
    assert purchase_notes(copy, _strings(note_labels=_NOTES)) == notes


def test_notes_need_their_translations() -> None:
    """No translation sent → no note, never an English fallback."""
    copy = OwnedCopy("microsoft", ("X",), False, False, platform="pc", gold=True)
    assert purchase_notes(copy, _strings()) == []


def test_only_known_note_keys_survive_sanitising() -> None:
    parsed = _strings(note_labels={**_NOTES, "evil": "<script>", "pc": "  PC  "})
    assert set(parsed.note_labels) == set(_NOTES)
    assert parsed.note_labels["pc"] == "PC"


def test_an_owned_xbox_chip_carries_its_notes() -> None:
    copies = [OwnedCopy("microsoft", ("Sekiro",), False, False, platform="console")]
    payload = build_ribbon_payload(814380, copies, _strings(note_labels=_NOTES), "Sekiro")
    [section] = payload["sections"]
    assert section["kind"] == "owned"
    assert section["chips"][0]["notes"] == ["Console"]
