"""Battle.net ownership: licence ledger, PUB catalog, rule evaluation, install state.

The playable catalog is not a list Blizzard hands out — it is the result of
evaluating the PUB catalog's rules against account facts. Two fact sources
are needed and neither is sufficient alone: licences miss every
free-to-play and subscription title (those match on ``game_account``),
while ``games-and-subs`` misses everything purchased.

Measured on-device 2026-08-09 against a real prefix: licences alone gave 17
programs, licences + game accounts gave **22**, and every one resolved to a
real display name and install uid.

``CachedData.db`` is synthesised here rather than shipped as a fixture: the
real one carries a battletag, account id and email.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from unifideck.stores.battlenet.ownership import (
    AccountFacts,
    GrantedProduct,
    InstalledGame,
    evaluate_catalog,
    evaluate_program,
    matches,
    merge_fragments,
    merge_install_state,
    parse_aggregate,
    parse_licences,
    read_aggregate,
    read_licences,
)
from unifideck.stores.battlenet.ownership.licenses import CACHED_DATA_RELATIVE
from unifideck.stores.battlenet.product_db import ProductInstall, parse_product_db

FIXTURES = Path(__file__).parent.parent / "fixtures" / "battlenet"
REAL_LICENCES = [
    168, 236, 260, 263, 274, 16332, 16515, 17019, 34998, 43338, 53736,
    107572, 107743, 601446, 615331, 931050, 959845, 1042650, 1042653,
    1042668, 1042675, 1043667, 1043668, 1081728, 1081786, 1091697, 1105059,
]


def _make_cached_data(path: Path, licences: list[int], *, battle_tag: str = "tester#1234") -> None:
    """Build a minimal CachedData.db with the two tables we read."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE key_value_store (key TEXT, value TEXT)")
    con.execute("CREATE TABLE login_cache (name TEXT, environment TEXT, battle_tag TEXT)")
    con.execute(
        "INSERT INTO key_value_store VALUES (?, ?)",
        (
            "features_cached_data_points",
            json.dumps(
                {
                    "account_country": "IND",
                    "account_id": 1234,
                    "account_region": "US",
                    "licenses": licences,
                }
            ),
        ),
    )
    con.execute("INSERT INTO login_cache VALUES (?, ?, ?)", ("x", "us.actual.battle.net", battle_tag))
    con.commit()
    con.close()


@pytest.fixture
def real_fragment() -> dict:
    return json.loads((FIXTURES / "pub_catalog_fragment.json").read_bytes())


# --------------------------------------------------------------------------
# licence ledger
# --------------------------------------------------------------------------


def test_reads_licence_ids_and_identity(tmp_path: Path) -> None:
    _make_cached_data(tmp_path / CACHED_DATA_RELATIVE, REAL_LICENCES)
    acc = read_licences(tmp_path)
    assert len(acc.licence_ids) == 27
    assert acc.account_id == 1234
    assert acc.battle_tag == "tester#1234"
    assert acc.is_usable is True


def test_missing_database_is_not_an_error(tmp_path: Path) -> None:
    assert read_licences(tmp_path).is_usable is False


def test_non_sqlite_file_degrades(tmp_path: Path) -> None:
    target = tmp_path / CACHED_DATA_RELATIVE
    target.parent.mkdir(parents=True)
    target.write_bytes(b"definitely not sqlite")
    assert read_licences(tmp_path).is_usable is False


def test_non_integer_licence_entries_are_dropped(tmp_path: Path) -> None:
    path = tmp_path / CACHED_DATA_RELATIVE
    _make_cached_data(path, [1, 2])
    con = sqlite3.connect(path)
    con.execute(
        "UPDATE key_value_store SET value = ? WHERE key = 'features_cached_data_points'",
        (json.dumps({"licenses": [1, "two", None, 3]}),),
    )
    con.commit()
    con.close()
    assert parse_licences(path).licence_ids == frozenset({1, 3})


# --------------------------------------------------------------------------
# rule evaluation — the part that decides the library
# --------------------------------------------------------------------------


def test_licence_match(real_fragment: dict) -> None:
    cat = merge_fragments(iter([real_fragment]))
    facts = AccountFacts(licence_ids=frozenset({1105059}))
    granted = evaluate_catalog(cat.program_configurations, facts)
    assert "ARK" in granted
    assert any(p.product_type == "retail" for p in granted["ARK"])


def test_unowned_licence_grants_nothing(real_fragment: dict) -> None:
    cat = merge_fragments(iter([real_fragment]))
    assert evaluate_catalog(cat.program_configurations, AccountFacts()) == {}


def test_free_to_play_matches_on_game_account_not_licence() -> None:
    """The bug this guards: a licence-only reading drops every F2P title."""
    config = {
        "WTCG": {
            "run_each_rule": [
                {
                    "match": {"game_account": {"program_id": "WTCG"}},
                    "actions": [
                        {"add_product": {"product_id": {"id": "WTCG", "type": "retail"}}},
                        {"add_tag": {"name": "play_for_free"}},
                    ],
                }
            ]
        }
    }
    assert evaluate_catalog(config, AccountFacts(licence_ids=frozenset({1, 2}))) == {}
    granted = evaluate_catalog(
        config, AccountFacts(game_account_programs=frozenset({"WTCG"}))
    )
    assert set(granted) == {"WTCG"}
    assert next(iter(granted["WTCG"])).is_free_to_play is True


def test_scalar_licence_id_is_tolerated() -> None:
    config = {
        "X": {"run_each_rule": [{"match": {"license_id": 42},
                                 "actions": [{"add_product": {"product_id": {"id": "X", "type": "retail"}}}]}]}
    }
    assert set(evaluate_catalog(config, AccountFacts(licence_ids=frozenset({42})))) == {"X"}


@pytest.mark.parametrize(
    ("criteria", "expected"),
    [
        pytest.param({"license_id": [1]}, True, id="licence-hit"),
        pytest.param({"license_id": [9]}, False, id="licence-miss"),
        pytest.param({"all_of": [{"license_id": [1]}, {"license_id": [2]}]}, True, id="all_of-true"),
        pytest.param({"all_of": [{"license_id": [1]}, {"license_id": [9]}]}, False, id="all_of-false"),
        pytest.param({"any_of": [{"license_id": [9]}, {"license_id": [2]}]}, True, id="any_of-true"),
        pytest.param({"any_of": [{"license_id": [8]}, {"license_id": [9]}]}, False, id="any_of-false"),
        pytest.param({"not": {"license_id": [9]}}, True, id="not-true"),
        pytest.param({"not": {"license_id": [1]}}, False, id="not-false"),
        pytest.param({"flag": "beta_access"}, True, id="flag-hit"),
        pytest.param({"license_id": [1], "flag": "nope"}, False, id="sibling-keys-are-conjunctive"),
        pytest.param({}, False, id="empty-never-matches"),
        pytest.param({"future_criterion": "x"}, False, id="unknown-key-is-false-not-true"),
    ],
)
def test_match_grammar(criteria: object, expected: bool) -> None:
    facts = AccountFacts(licence_ids=frozenset({1, 2}), flags=frozenset({"beta_access"}))
    assert matches(criteria, facts) is expected


def test_run_first_rule_stops_at_the_first_match() -> None:
    """Mutually exclusive branches must not both grant."""
    config = {
        "X": {
            "run_first_rule": [
                {"match": {"license_id": [1]},
                 "actions": [{"add_product": {"product_id": {"id": "X", "type": "gamepass"}}}]},
                {"match": {"license_id": [1]},
                 "actions": [{"add_product": {"product_id": {"id": "X", "type": "retail"}}}]},
            ]
        }
    }
    granted = evaluate_program("X", config["X"], AccountFacts(licence_ids=frozenset({1})))
    assert {p.product_type for p in granted} == {"gamepass"}


def test_granted_product_records_the_program_that_granted_it() -> None:
    """Variants like WoWPTR must fold into their program, not stand alone."""
    config = {
        "WoW": {"run_each_rule": [{"match": {"license_id": [7]},
                                   "actions": [{"add_product": {"product_id": {"id": "WoWPTR", "type": "ptr"}}}]}]}
    }
    granted = evaluate_catalog(config, AccountFacts(licence_ids=frozenset({7})))
    assert set(granted) == {"WoW"}
    product = next(iter(granted["WoW"]))
    assert (product.program, product.product_id) == ("WoW", "WoWPTR")


def test_deeply_nested_rules_terminate() -> None:
    rule: dict = {"match": {"license_id": [1]}, "actions": []}
    node = rule
    for _ in range(30):
        nested = {"match": {"license_id": [1]}, "actions": []}
        node["actions"] = [{"run_first_rule": [nested]}]
        node = nested
    config = {"X": {"run_each_rule": [rule]}}
    assert evaluate_catalog(config, AccountFacts(licence_ids=frozenset({1}))) == {}


# --------------------------------------------------------------------------
# catalog metadata
# --------------------------------------------------------------------------


def test_catalog_exposes_names_uids_and_title_id(real_fragment: dict) -> None:
    cat = merge_fragments(iter([real_fragment]))
    entry = cat.entry_for("ARK")
    assert entry is not None
    assert entry.program_id == "ARK"
    assert entry.uid_for() == "ark"
    assert cat.display_name("ARK") == "The Outer Worlds 2"


def test_english_lives_under_the_default_locale(real_fragment: dict) -> None:
    """There is no 'enUS' key in the catalog at all."""
    cat = merge_fragments(iter([real_fragment]))
    assert "enUS" not in cat.strings
    assert cat.text("arkansas#ARK_NAME") == "The Outer Worlds 2"


def test_type_uids_union_across_partial_fragments() -> None:
    """Fragments are partial and repeat; first-wins produced 'wow_alpha'."""
    def frag(types: dict, installs: dict) -> dict:
        return {
            "fragment_id": "wow",
            "program_configuration": {"WoW": {}},
            "installs": installs,
            "products": [{"id": "WoW", "base": {"program_id": "WoW", "types": types}}],
        }

    cat = merge_fragments(iter([
        frag({"alpha": {"uid": "wow_alpha"}}, {"wow_alpha": {}}),
        frag({"retail": {"uid": "wow"}}, {"wow": {}}),
    ]))
    assert cat.entry_for("WoW").uid_for() == "wow"


def test_supported_platforms_is_parsed_and_unioned(real_fragment: dict) -> None:
    """Blizzard gates installs on this field; the presumption follows it."""
    cat = merge_fragments(iter([real_fragment]))
    assert cat.entry_for("ARK").supported_platforms == ("win",)

    def frag(base: dict) -> dict:
        return {
            "fragment_id": "x",
            "program_configuration": {"X": {}},
            "products": [{"id": "X", "base": {"program_id": "X", **base}}],
        }

    # A later fragment carrying the field must not be lost to an earlier
    # one that omitted it — the same partial-repeat shape as type uids.
    cat = merge_fragments(iter([frag({}), frag({"supported_platforms": ["mac"]})]))
    assert cat.entry_for("X").supported_platforms == ("mac",)
    assert cat.entry_for("X").runs_on_windows() is False


def test_a_title_with_no_platform_data_is_treated_as_windows() -> None:
    """Absent data must never cost a user a game they own."""
    cat = merge_fragments(iter([{
        "fragment_id": "x",
        "program_configuration": {"X": {}},
        "products": [{"id": "X", "base": {"program_id": "X"}}],
    }]))
    assert cat.entry_for("X").runs_on_windows() is True


def test_uid_falls_back_to_installs_when_types_lack_retail() -> None:
    """Real case: no cached WoW fragment carries a retail type."""
    cat = merge_fragments(iter([{
        "fragment_id": "wow",
        "program_configuration": {"WoW": {}},
        "installs": {"wow_alpha": {}, "wow": {}, "wow_ne_vendor11": {}},
        "products": [{"id": "WoW", "base": {"program_id": "WoW",
                                            "types": {"alpha": {"uid": "wow_alpha"}}}}],
    }]))
    assert cat.entry_for("WoW").uid_for() == "wow"


def test_malformed_fragments_are_skipped() -> None:
    cat = merge_fragments(iter([
        {"fragment_id": "a", "program_configuration": None, "products": "nope"},
        {"fragment_id": "b", "products": [{"id": "X", "base": {}}]},
    ]))
    assert cat.entries == {}


# --------------------------------------------------------------------------
# installed state
# --------------------------------------------------------------------------


def test_parses_the_real_aggregate_json() -> None:
    games = parse_aggregate((FIXTURES / "aggregate_installed.json").read_bytes())
    hs = games["hsb"]
    assert hs.name == "Hearthstone"
    assert hs.exe_windows_path.endswith("Hearthstone Beta Launcher.exe")
    assert hs.launch_uri == "battlenet://game/hsb"
    assert hs.box_art_url and hs.logo_art_url


def test_aggregate_presence_does_not_imply_installed() -> None:
    """The trap: aggregate.json is written at ~40% of a download."""
    merged = merge_install_state(
        {"hsb": InstalledGame(code="hsb", name="Hearthstone")},
        {"hsb": ProductInstall(code="hsb")},
    )
    assert merged["hsb"].is_ready is False


def test_merge_overlays_product_db_truth() -> None:
    aggregate = parse_aggregate((FIXTURES / "aggregate_installed.json").read_bytes())
    products = parse_product_db((FIXTURES / "product_db_installed.bin").read_bytes())
    hs = merge_install_state(aggregate, products)["hsb"]
    assert hs.name == "Hearthstone"
    assert hs.is_ready is True
    assert hs.total_bytes == 12_428_894_444


def test_product_db_row_without_aggregate_entry_still_counts() -> None:
    products = {"zzz": ProductInstall(code="zzz", installed=True, playable=True, update_complete=True)}
    assert merge_install_state({}, products)["zzz"].is_ready is True


def test_malformed_aggregate_degrades(tmp_path: Path) -> None:
    target = tmp_path / "ProgramData/Battle.net/Agent/aggregate.json"
    target.parent.mkdir(parents=True)
    target.write_text("{not json")
    assert read_aggregate(tmp_path) == {}


def test_granted_product_is_hashable_for_set_semantics() -> None:
    a = GrantedProduct(program="X", product_id="X", product_type="retail")
    b = GrantedProduct(program="X", product_id="X", product_type="retail")
    assert len({a, b}) == 1
