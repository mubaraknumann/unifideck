"""The wrapper-store predicates, and the invariant they exist to protect.

A *wrapper store* runs a vendor Windows client inside the prefix. The
structural consequence is that the game's files live inside the prefix, so
a prefix reset destroys user data rather than costing a rebuild.

This module exists because that question used to be asked as a bare
``store == "ubisoft"`` in five places, and on 2026-08-01 two of them
disagreed: ``prefix_setup`` borrowed managed GE-Proton for a winetricks
verb, ``prefix_init`` saw the Proton family change and wiped the prefix,
and Rayman Origins was deleted. The borrow was for a step
``apply_prefix_compat`` skips for Ubisoft anyway.

The tests below therefore assert two different things: that the predicates
behave, and that the real call sites actually route through them.

**Why an expectation table and not a loop over ``WRAPPER_STORES``.** These
tests used to parametrize over the set and assert all four predicates True
for every member, which welded the three frozensets together — the exact
opposite of the divergence the module exists to allow (audit §3.1). The
dangerous half is ``skips_generic_compat``: forced True, a wrapper store
gets no winetricks, no vcredist and no VC++ registry import, so every one of
its games launches to a missing DLL. A new store must state its own answers
in ``_EXPECTED`` rather than inherit them from a membership test.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from unifideck.launcher import wrapper_stores as ws

REPO = Path(__file__).parent.parent.parent
LAUNCHER = REPO / "py_modules/unifideck/launcher"
PROTON = LAUNCHER / "proton"


# One row per wrapper store, each answer decided deliberately rather than
# derived from WRAPPER_STORES membership. See the module docstring.
_EXPECTED: dict[str, dict[str, bool]] = {
    "ubisoft": {
        # Installs to drive_c/Program Files (x86)/Ubisoft/Ubisoft Game
        # Launcher/games/ — inside the prefix.
        "owns_install": True,
        # UPC ships its own redistributables.
        "skips_compat": True,
        # No byte-level telemetry from UPC.
        "manual_phase": True,
    },
    "battlenet": {
        # Confirmed on-device: a real Hearthstone install landed at
        # C:/Program Files (x86)/Hearthstone.
        "owns_install": True,
        # The Battle.net client ships its own redistributables.
        "skips_compat": True,
        # Measured: product.db carries no progress during a download.
        "manual_phase": True,
    },
}


def test_every_wrapper_store_states_its_own_answers() -> None:
    """A new wrapper store must add a row before these tests can pass.

    This is the whole point of the table. Adding EA App to WRAPPER_STORES
    fails here, which forces a per-predicate decision, instead of the store
    silently inheriting ``skips_generic_compat`` and shipping with no
    redistributables installed in any of its prefixes.
    """
    assert set(_EXPECTED) == ws.WRAPPER_STORES


@pytest.mark.parametrize("store", sorted(_EXPECTED))
def test_each_wrapper_store_matches_its_declared_row(store: str) -> None:
    row = _EXPECTED[store]
    assert ws.is_wrapper_store(store)
    assert ws.prefix_owns_game_install(store) is row["owns_install"]
    assert ws.skips_generic_compat(store) is row["skips_compat"]
    assert ws.uses_manual_download_phase(store) is row["manual_phase"]


def test_the_narrow_predicates_never_exceed_the_wrapper_set() -> None:
    """The safe direction, still welded on purpose.

    A store may be a wrapper without owning its installs or bundling its own
    redistributables. The reverse is incoherent: only a wrapper store runs a
    vendor client in the prefix at all, so a non-wrapper store in either set
    would skip a reset or a redistributable install it genuinely needs.
    """
    assert ws._PREFIX_OWNS_INSTALL <= ws.WRAPPER_STORES
    assert ws._SKIPS_GENERIC_COMPAT <= ws.WRAPPER_STORES


@pytest.mark.parametrize("store", ["epic", "gog", "amazon", "microsoft", "steam"])
def test_non_wrapper_stores_are_excluded(store: str) -> None:
    """A false positive here means we skip a reset a store genuinely needs."""
    assert not ws.is_wrapper_store(store)
    assert not ws.prefix_owns_game_install(store)
    assert not ws.skips_generic_compat(store)
    assert not ws.uses_manual_download_phase(store)


@pytest.mark.parametrize("store", [None, "", "  ", "UBISOFT", "Battlenet"])
def test_unknown_or_miscased_values_are_false(store: str | None) -> None:
    """Store ids are lowercase everywhere; never match loosely."""
    assert not ws.is_wrapper_store(store)
    assert not ws.prefix_owns_game_install(store)


def test_battlenet_and_ubisoft_are_both_wrapper_stores() -> None:
    assert {"ubisoft", "battlenet"} <= ws.WRAPPER_STORES


def test_predicates_are_separate_functions_not_one_alias() -> None:
    """They answer different questions and are expected to diverge.

    EA App installs some titles to Program Files *outside* the prefix, so it
    would be a wrapper store that does not own its installs.
    """
    names = {"is_wrapper_store", "prefix_owns_game_install", "skips_generic_compat"}
    assert names <= set(dir(ws))
    assert len({id(getattr(ws, n)) for n in names}) == len(names)


def test_module_is_stdlib_only() -> None:
    """Imported from the launcher, which runs under system Python 3.10-3.14."""
    source = (LAUNCHER / "wrapper_stores.py").read_text()
    imports = [
        line for line in source.splitlines()
        if line.startswith(("import ", "from ")) and "__future__" not in line
    ]
    assert imports == []


# --------------------------------------------------------------------------
# the call sites must actually route through the predicates
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("relative", "predicate"),
    [
        pytest.param("compat/prefix_init.py", "prefix_owns_game_install", id="prefix-reset-guard"),
        pytest.param("prefix_setup.py", "prefix_owns_game_install", id="prefix-setup-guard"),
        pytest.param("compat/__init__.py", "skips_generic_compat", id="generic-compat-skip"),
    ],
)
def test_call_sites_use_the_shared_predicate(relative: str, predicate: str) -> None:
    source = (PROTON / relative).read_text()
    assert predicate in source, f"{relative} no longer routes through {predicate}"
    assert '== "ubisoft"' not in source, (
        f"{relative} reintroduced a bare store comparison — that divergence "
        f"is what deleted a user's game on 2026-08-01"
    )


def test_prefix_init_guard_agrees_with_the_expectation_table() -> None:
    """The actual guard, exercised — not just its source text.

    Driven off ``_EXPECTED``, not off ``WRAPPER_STORES``: this guard answers
    ``prefix_owns_game_install``, which a future wrapper store is allowed to
    answer ``False``.
    """
    from unifideck.launcher.proton.compat.prefix_init import _prefix_owns_game_install

    class _Ctx:
        def __init__(self, store: str) -> None:
            self.store = store

    class _Plan:
        def __init__(self, store: str) -> None:
            self.context = _Ctx(store)

    for store, row in _EXPECTED.items():
        assert _prefix_owns_game_install(_Plan(store)) is row["owns_install"]
    for store in ("epic", "gog", "amazon"):
        assert _prefix_owns_game_install(_Plan(store)) is False


def test_guard_tolerates_a_context_without_a_store_attribute() -> None:
    from unifideck.launcher.proton.compat.prefix_init import _prefix_owns_game_install

    class _Plan:
        context = object()

    assert _prefix_owns_game_install(_Plan()) is False


def test_predicates_accept_a_plain_string_not_a_context() -> None:
    """Keeps them usable from services/, which has no LaunchContext."""
    for fn in (ws.is_wrapper_store, ws.prefix_owns_game_install,
               ws.skips_generic_compat, ws.uses_manual_download_phase):
        assert inspect.signature(fn).parameters.keys() == {"store"}
