"""Tests for ``stores/microsoft/pc_library/entitlements``: the port of the
Nexus entitlement filter (``account-library.ts``).

The fixture is a real ``xodus-cli library --json --market IN`` answer cut
down to 14 rows, with the account email replaced by a fake one.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from unifideck.stores.microsoft.pc_library import entitlements as ent
from unifideck.stores.microsoft.pc_library.entitlements import (
    EnvelopeError,
    access_of,
    parse_library_output,
    parse_rfc3339,
    plan_id_of,
)

FIXTURE = Path(__file__).parent.parent / "fixtures" / "microsoft" / "xbox_pc_library_IN.json"
CHECKED = parse_rfc3339("2026-10-20T18:51:54.508009681+00:00")
assert CHECKED is not None
NOW = CHECKED + 10


def _raw() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text())


def _row(pid: str, parent: str | None = None, **over: Any) -> dict[str, Any]:
    row = {"productId": pid, "status": "Active", "isTrial": False, "special": False,
           "sharingSource": "None", "endDate": "9999-12-31T23:59:59.997Z",
           "satisfiedByProductId": parent}
    row.update(over)
    return row


def _answer(rows: list[Any], **over: Any) -> str:
    data = {"schema": 1, "account": "player@example.invalid", "market": "IN",
            "checkedAt": "2026-10-20T18:51:54.508009681+00:00",
            "skippedEntitlements": 0, "entitlements": rows}
    data.update(over)
    return json.dumps(data)


def test_fixture_has_no_real_account() -> None:
    assert _raw()["account"] == "player@example.invalid"


def test_fixture_access_kinds() -> None:
    snap = parse_library_output(FIXTURE.read_text(), market="IN", now=NOW)
    kinds = {pid: access_of(pid, snap.entries, NOW) for pid in snap.entries}
    assert kinds["9PK087LNGJC5"] == "gamepass"  # Balatro, Game Pass for Console
    assert kinds["9MSMN2PJ6HQN"] == "gamepass"  # PC Game Pass
    assert kinds["9MW4RJDRSF78"] == "gamepass"  # Essential
    assert kinds["9MTP0GJ0Q3MG"] == "ubisoft_plus"
    assert kinds["9MVN4ND41DD3"] == "ea_play"
    assert kinds["9MVQN16MCT10"] == "owned"
    assert kinds["9PB4ZWV7S2MG"] == "owned"  # a bundle purchase
    assert kinds["9MVJ1KSRJQJQ"] == "owned"  # a part of that bundle
    assert kinds["9MTVJ3HHTQGS"] is None     # its parent is not a row
    assert snap.skipped == 0


def test_account_is_only_kept_as_a_hash(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    snap = parse_library_output(FIXTURE.read_text(), market="IN", now=NOW)
    assert "@" not in snap.account_hash
    assert snap.account_hash == ent.account_hash(" Player@Example.invalid ")
    assert "player@example.invalid" not in repr(snap)
    assert "player@example.invalid" not in caplog.text


def test_nine_digit_fractions_and_z_parse() -> None:
    assert parse_rfc3339("2026-10-20T18:51:54.508009681+00:00") == pytest.approx(
        parse_rfc3339("2026-10-20T18:51:54.508009Z"))
    assert parse_rfc3339("9999-12-31T23:59:59.997Z") is not None
    assert parse_rfc3339("not a date") is None
    assert parse_rfc3339("") is None


@pytest.mark.parametrize(("over", "code"), [
    ({"schema": 2}, "bad_envelope"),
    ({"market": "US"}, "bad_envelope"),
    ({"account": None}, "bad_envelope"),
    ({"entitlements": "x"}, "bad_envelope"),
    ({"checkedAt": "2026-10-20T18:40:00Z"}, "stale"),       # older than 5 minutes
    ({"checkedAt": "2026-10-20T19:00:00Z"}, "stale"),       # more than 60 s ahead
    ({"skippedEntitlements": -1}, "bad_skipped"),
    ({"skippedEntitlements": 1.5}, "bad_skipped"),
    ({"skippedEntitlements": 200_001}, "bad_skipped"),
])
def test_bad_envelopes_fail_the_whole_read(over: dict[str, Any], code: str) -> None:
    with pytest.raises(EnvelopeError) as err:
        parse_library_output(_answer([], **over), market="IN", now=NOW)
    assert err.value.code == code


def test_not_json_and_too_large() -> None:
    with pytest.raises(EnvelopeError, match="bad_json"):
        parse_library_output("panic: oops\n", market="IN", now=NOW)
    with pytest.raises(EnvelopeError, match="too_large"):
        parse_library_output("{" + " " * ent.MAX_OUTPUT_CHARS, market="IN", now=NOW)


def test_too_many_rows() -> None:
    rows = [_row("9AAAAAAAAAAA")] * (ent.MAX_ROWS + 1)
    with pytest.raises(EnvelopeError):
        parse_library_output(_answer(rows), market="IN", now=NOW)


def test_the_last_json_line_is_used() -> None:
    text = "warning: something\n" + _answer([_row("9AAAAAAAAAAA")]) + "\n"
    snap = parse_library_output(text, market="IN", now=NOW)
    assert list(snap.entries) == ["9AAAAAAAAAAA"]


def test_invalid_id_is_skipped_but_not_blocked() -> None:
    rows = [_row("9aaaaaaaaaaa"), _row("9BBBBBBBBBBB", parent="9aaaaaaaaaaa"), _row("9CCCCCCCCCCC")]
    snap = parse_library_output(_answer(rows), market="IN", now=NOW)
    # The child names an invalid parent id, so it is itself invalid and blocked.
    assert set(snap.entries) == {"9CCCCCCCCCCC"}
    assert snap.skipped == 2


def test_duplicates_block_both_copies_and_their_descendants() -> None:
    rows = [
        _row("9AAAAAAAAAAA"), _row("9AAAAAAAAAAA"),
        _row("9BBBBBBBBBBB", parent="9AAAAAAAAAAA"),
        _row("9CCCCCCCCCCC", parent="9BBBBBBBBBBB"),
        _row("9DDDDDDDDDDD"),
    ]
    snap = parse_library_output(_answer(rows), market="IN", now=NOW)
    assert set(snap.entries) == {"9DDDDDDDDDDD"}
    assert snap.skipped == 4


@pytest.mark.parametrize("over", [
    {"status": 5}, {"status": "x" * 40}, {"isTrial": "no"}, {"special": None},
    {"sharingSource": 1}, {"endDate": "never"}, {"endDate": "x" * 50},
    {"satisfiedByProductId": "cfq7ttc0kgq8"},
])
def test_a_bad_field_blocks_the_row(over: dict[str, Any]) -> None:
    snap = parse_library_output(_answer([_row("9AAAAAAAAAAA", **over)]), market="IN", now=NOW)
    assert snap.entries == {}
    assert snap.skipped == 1


def test_skipped_adds_the_cli_count() -> None:
    snap = parse_library_output(
        _answer([_row("9AAAAAAAAAAA"), "junk"], skippedEntitlements=3), market="IN", now=NOW)
    assert snap.skipped == 4


@pytest.mark.parametrize(("over", "kind"), [
    ({}, "owned"),
    ({"status": "Revoked"}, None),
    ({"isTrial": True}, None),
    ({"special": True}, None),
    ({"sharingSource": "Family"}, None),
    ({"endDate": "2020-01-01T00:00:00Z"}, None),
])
def test_access_needs_a_usable_row(over: dict[str, Any], kind: str | None) -> None:
    snap = parse_library_output(_answer([_row("9AAAAAAAAAAA", **over)]), market="IN", now=NOW)
    assert access_of("9AAAAAAAAAAA", snap.entries, NOW) == kind


@pytest.mark.parametrize(("plan", "kind"), [
    ("CFQ7TTC0KGQ8", "gamepass"), ("CFQ7TTC0KHS0", "gamepass"), ("CFQ7TTC0P85B", "gamepass"),
    ("CFQ7TTC0K5DJ", "gamepass"), ("CFQ7TTC0K6L8", "gamepass"), ("CFQ7TTC10QFD", "gamepass"),
    ("CFQ7TTC0K5DH", "ea_play"), ("CFQ7TTBZT8LN", "ubisoft_plus"), ("CFQ7TTZZZZZZ", "unknown_plan"),
])
def test_plan_parents(plan: str, kind: str) -> None:
    snap = parse_library_output(_answer([_row("9AAAAAAAAAAA", parent=plan)]), market="IN", now=NOW)
    assert access_of("9AAAAAAAAAAA", snap.entries, NOW) == kind
    assert plan_id_of("9AAAAAAAAAAA", snap.entries) == plan


def test_a_plan_row_is_not_owned() -> None:
    snap = parse_library_output(_answer([_row("CFQ7TTC0KGQ8")]), market="IN", now=NOW)
    assert access_of("CFQ7TTC0KGQ8", snap.entries, NOW) is None


def test_bundle_parts_inherit_access_and_need_a_usable_parent() -> None:
    rows = [_row("9AAAAAAAAAAA", parent="CFQ7TTC0KGQ8"),
            _row("9BBBBBBBBBBB", parent="9AAAAAAAAAAA"),
            _row("9CCCCCCCCCCC", endDate="2020-01-01T00:00:00Z"),
            _row("9DDDDDDDDDDD", parent="9CCCCCCCCCCC")]
    snap = parse_library_output(_answer(rows), market="IN", now=NOW)
    assert access_of("9BBBBBBBBBBB", snap.entries, NOW) == "gamepass"
    assert plan_id_of("9BBBBBBBBBBB", snap.entries) == "CFQ7TTC0KGQ8"
    assert access_of("9DDDDDDDDDDD", snap.entries, NOW) is None


def test_parent_cycles_and_long_chains_give_no_access() -> None:
    rows = [_row("9AAAAAAAAAAA", parent="9BBBBBBBBBBB"), _row("9BBBBBBBBBBB", parent="9AAAAAAAAAAA")]
    snap = parse_library_output(_answer(rows), market="IN", now=NOW)
    assert access_of("9AAAAAAAAAAA", snap.entries, NOW) is None
    chain = [_row(f"9{i:011d}", parent=f"9{i + 1:011d}" if i < 20 else None) for i in range(21)]
    snap = parse_library_output(_answer(chain), market="IN", now=NOW)
    assert access_of(f"9{0:011d}", snap.entries, NOW) is None
    assert access_of(f"9{10:011d}", snap.entries, NOW) == "owned"
