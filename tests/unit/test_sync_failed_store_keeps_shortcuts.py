"""A store that could not answer must not lose its shortcuts.

Audit §3.5, finding B. The post-sync reconcile decides which stores' stale
shortcuts it may delete. It used to be handed *every registered store*,
which meant a store contributing zero games had every shortcut it owned
deleted — and a store contributes zero games without owning zero games in
four different ways: it raised, it timed out, it returned ``None`` ("I
could not read"), or it was unavailable and never fetched at all. The last
one records no error, so it was invisible in the logs too.

The live example this closes: GOG's ``is_available`` refuses when
``bin/gogdl`` is missing or non-executable (audit §3.2), so a half-applied
update that lost the exec bit made the next sync delete every GOG shortcut
in the library.

Two halves are pinned here, and the second is the one that fails if the fix
is written too broadly:

1. a store that did not answer is **not** sweepable;
2. a store that answered with an **empty** library still **is** — that is
   the phantom-row cleanup the original widening existed for.
"""
from __future__ import annotations

from unifideck.services.shortcut.events import _sweepable_stores
from unifideck.services.shortcut.games_map import UNIFIDECK_TAG
from unifideck.services.shortcut.protected import (
    LEGACY_SWEEP_IDS,
    PROTECTED_IDS,
)
from unifideck.services.shortcut.stale_predicate import (
    is_stale_managed_shortcut,
)

_is_stale = is_stale_managed_shortcut

_LAUNCHER = "/home/deck/homebrew/plugins/Unifideck/bin/unifideck-launcher"


def _managed(launch: str, appid: int) -> dict:
    """A genuine Unifideck shortcut: launcher ``Exe`` + store:id token."""
    return {
        "appid": appid,
        "Exe": f'"{_LAUNCHER}"',
        "LaunchOptions": launch,
        "tags": {"0": UNIFIDECK_TAG, "1": launch.split(":", 1)[0]},
    }


# ── which stores this sync may sweep ─────────────────────────────────────

def test_store_that_answered_cleanly_is_sweepable():
    assert _sweepable_stores(
        {"stores_synced": ["epic", "gog"], "errors": {}},
    ) == {"epic", "gog"}


def test_store_that_answered_empty_is_still_sweepable():
    """An authoritative "you own nothing" still sweeps.

    This is the phantom-Ubisoft / legacy-row cleanup the widening was
    added for, and narrowing the rule must not take it away. ``errors``
    is empty, so ubisoft answered — it just answered with no games.
    """
    assert "ubisoft" in _sweepable_stores(
        {"stores_synced": ["ubisoft", "epic"], "errors": {}},
    )


def test_store_that_raised_is_not_sweepable():
    assert _sweepable_stores(
        {
            "stores_synced": ["epic", "gog"],
            "errors": {"epic": "HTTPError: 503"},
        },
    ) == {"gog"}


def test_store_that_timed_out_is_not_sweepable():
    assert "gog" not in _sweepable_stores(
        {"stores_synced": ["epic", "gog"], "errors": {"gog": "timeout"}},
    )


def test_store_that_could_not_read_is_not_sweepable():
    """``get_library() -> None`` arrives as ``library_unreadable``."""
    assert "battlenet" not in _sweepable_stores(
        {
            "stores_synced": ["battlenet", "epic"],
            "errors": {"battlenet": "library_unreadable"},
        },
    )


def test_unavailable_store_never_fetched_is_not_sweepable():
    """The case that recorded no error at all.

    An unavailable store is not in ``available_stores``, so it never
    reaches ``libraries`` and never lands in ``errors`` either. Keying on
    ``stores_synced`` is what covers it; keying on ``errors`` alone would
    not. This is the gogdl-exec-bit regression path.
    """
    assert _sweepable_stores(
        {"stores_synced": ["epic"], "errors": {}},
    ) == {"epic"}


def test_every_store_failing_sweeps_nothing():
    """Not ``None`` — an empty set means "sweep nothing".

    ``reconcile`` treats ``valid_stores=None`` as "default to the stores
    that returned games", so returning ``None`` here would re-enable the
    sweep for exactly the run where nothing can be trusted.
    """
    result = _sweepable_stores(
        {"stores_synced": ["epic"], "errors": {"epic": "boom"}},
    )
    assert result == set()
    assert result is not None


def test_malformed_payload_sweeps_nothing():
    assert _sweepable_stores({}) == set()
    assert _sweepable_stores({"stores_synced": "epic"}) == set()
    assert _sweepable_stores(
        {"stores_synced": ["epic"], "errors": "boom"},
    ) == {"epic"}


# ── the shortcuts themselves survive ─────────────────────────────────────

def test_shortcut_of_a_failed_store_survives_the_sweep():
    """End of the chain: the entry is not deleted.

    Asserted on the entry rather than on "reconcile ran", because the
    broken version also ran reconcile — it just deleted this row.
    """
    entry = _managed("gog:1207658930", appid=999)
    sweepable = _sweepable_stores(
        {
            "stores_synced": ["epic", "gog"],
            "errors": {"gog": "library_unreadable"},
        },
    )
    assert not _is_stale(
        entry,
        valid_app_ids=set(),
        valid_stores=sweepable,
        launcher_path=_LAUNCHER,
    )


def test_shortcut_of_an_empty_but_healthy_store_is_still_swept():
    entry = _managed("ubisoft:123", appid=999)
    sweepable = _sweepable_stores(
        {"stores_synced": ["ubisoft"], "errors": {}},
    )
    assert _is_stale(
        entry,
        valid_app_ids=set(),
        valid_stores=sweepable,
        launcher_path=_LAUNCHER,
    )


# ── the legacy escape hatch ──────────────────────────────────────────────

def test_legacy_row_swept_even_though_its_store_never_answered():
    """The one artifact the narrowing would otherwise strand.

    A user who upgraded from 0.6.x and never signed into Microsoft has
    this row and an unavailable Microsoft store, so the narrow rule can
    never reach it. Naming the id is the fix; widening the store rule was
    what let a signed-out store lose its whole library.
    """
    entry = _managed("microsoft:ms-auth", appid=555)
    assert _is_stale(
        entry, valid_app_ids=set(), valid_stores=set(),
        launcher_path=_LAUNCHER,
    )


def test_legacy_sweep_does_not_override_ownership():
    """A foreign shortcut carrying the legacy id is still not ours."""
    entry = {
        "appid": 555,
        "Exe": '"/home/deck/.local/share/NonSteamLaunchers/nsl.sh"',
        "LaunchOptions": "microsoft:ms-auth",
        "tags": {},
    }
    assert not _is_stale(
        entry, valid_app_ids=set(), valid_stores=set(),
        launcher_path=_LAUNCHER,
    )


def test_legacy_sweep_and_protected_sets_are_disjoint():
    """An id in both would resolve differently depending on call order."""
    assert not (LEGACY_SWEEP_IDS & PROTECTED_IDS)


# ── item 30: the invariant is now nominal, not just documented ──────
def test_sweepable_stores_is_a_distinct_type_not_a_bare_set() -> None:
    """§3.5 finding B was a widened caller, not a missing guard.

    The guard existed and its docstring explained itself ("how staging
    avoided nuking the user's Epic shortcuts after they logged out of
    Epic"); ``events.py`` passed every registered store anyway. A store
    contributes zero games without owning zero games in four ways — it
    raised, it timed out, it was never fetched, or it answered ``[]`` — and
    each deleted every shortcut it owned.

    ``valid_stores`` is a ``NewType`` so mypy rejects
    ``reconcile(games, valid_stores=set(registry.store_ids()))`` — the exact
    line that caused it. Verified against that planted call: mypy reports
    `Argument "valid_stores" ... has incompatible type "set[Any]"`.
    Audit register item 30; per §2.1, prefer making the wrong call
    impossible over testing that it is not made.
    """
    from unifideck.services.shortcut.stale_predicate import SweepableStores

    value = SweepableStores(frozenset({"gog"}))
    assert value == frozenset({"gog"})
    # A NewType is erased at runtime, so this is a static guarantee. The
    # runtime half of the invariant is the behavioural tests above.
    # __supertype__ is the parameterised ``frozenset[str]``, not bare frozenset.
    assert SweepableStores.__supertype__ == frozenset[str]  # type: ignore[attr-defined]


def test_only_sweepable_stores_constructs_the_type() -> None:
    """The producer must be the single entry point.

    A second producer would reintroduce the second policy this type exists
    to prevent — which is what ``rpc/mixins/account.py`` nearly was: it
    calls ``reconcile(games)`` with no ``valid_stores`` and relies on the
    narrow stores-with-games default, a *different* policy that happens to
    be safe. Anything constructing the type must be deliberate.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[2] / "py_modules" / "unifideck"
    producers = set()
    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if re.search(r"\bSweepableStores\(", text):
            producers.add(path.relative_to(root).as_posix())

    assert producers == {
        "services/shortcut/events.py",
        "services/shortcut/reconcile_phases.py",
    }, (
        f"unexpected producer(s) of SweepableStores: {sorted(producers)} — "
        f"the whole point is that only the sweep-eligibility calculation "
        f"and reconcile's own narrow default can build one"
    )


# ── The other half: the launch row ────────────────────────────────────
#
# The sweep above protects ``shortcuts.vdf``. ``_reconcile_phase_prune_map``
# prunes ``games.map``, which is where the launcher resolves a game's
# executable, and it was NOT scoped the same way — so a store that could
# not answer kept its shortcut and lost the row behind it. The shortcut then
# sits in the library looking installed and does nothing when launched.
#
# Measured on GameVault, whose server is a machine the user runs and so is
# routinely offline. The sync did the right thing and said so:
#
#   [SyncService] gamevault could not read its library — keeping its
#   existing shortcuts rather than treating this as an empty library
#
# and ``games.map`` was rewritten two lines later without the row.

class _Host:
    """Just enough of ShortcutService for the prune phase."""

    def __init__(self, games_map: dict) -> None:
        self._games_map = games_map


def _prune(games_map: dict, valid_keys: set, valid_stores) -> tuple[int, dict]:
    from unifideck.services.shortcut.reconcile_phases import (
        _ReconcilePhasesMixin,
    )

    host = _Host(dict(games_map))
    removed = _ReconcilePhasesMixin._reconcile_phase_prune_map(
        host, valid_keys, valid_stores,
    )
    return removed, host._games_map


def test_a_store_that_did_not_answer_keeps_its_launch_rows() -> None:
    games_map = {"gamevault:1": object(), "epic:abc": object()}
    # Epic answered (and still lists its game); GameVault answered not at all.
    removed, remaining = _prune(
        games_map, valid_keys={"epic:abc"}, valid_stores=frozenset({"epic"}),
    )

    assert removed == 0
    assert set(remaining) == {"gamevault:1", "epic:abc"}


def test_a_store_that_answered_still_drops_a_game_it_no_longer_lists() -> None:
    """The phase must keep doing its job — this is a real removal."""
    games_map = {"epic:gone": object(), "epic:kept": object()}

    removed, remaining = _prune(
        games_map, valid_keys={"epic:kept"}, valid_stores=frozenset({"epic"}),
    )

    assert removed == 1
    assert set(remaining) == {"epic:kept"}


def test_an_answering_store_with_an_empty_library_drops_its_rows() -> None:
    """Symmetric with the sweep half above: empty is an answer."""
    games_map = {"epic:gone": object()}

    removed, remaining = _prune(
        games_map, valid_keys=set(), valid_stores=frozenset({"epic"}),
    )

    assert removed == 1
    assert remaining == {}
