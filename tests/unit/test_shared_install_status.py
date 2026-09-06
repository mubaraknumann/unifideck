"""The one ``merge_install_status``, and the arguments each store hands it.

Epic, GOG and Amazon each carried a near-identical copy of this merge
(audit §3.4). They diverged in three places, two of which were deliberate
and one of which was a hole:

* Epic and Amazon re-checked the recorded directory against disk; GOG did
  not, because its map comes from a live walk. **Deliberate** — kept as
  ``verify_dir``.
* GOG carried the scanned executable onto ``exe_path``; Epic and Amazon did
  not, because their CLIs record a *relative* path. **Deliberate** — kept as
  ``exe_key``.
* Epic and Amazon guarded the disk check with ``if install_path and ...``,
  so an entry with a missing or empty path skipped it and was marked
  installed anyway. **A hole** — closed here, for all three.

The second half of this module is the more important half: it pins the
kwargs each store's ``get_library`` actually passes. The matrix tests would
still pass if a store were rewired to another store's semantics.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from unifideck.core.types import Game
from unifideck.stores.shared.install_status import merge_install_status

if TYPE_CHECKING:
    from pathlib import Path

#: Mirrors the three live call sites. Update together with them.
EPIC_ARGS: dict[str, Any] = {}
GOG_ARGS: dict[str, Any] = {"exe_key": "executable", "verify_dir": False}
AMAZON_ARGS: dict[str, Any] = {"path_key": "path"}

_ALL_STORES = [
    pytest.param(EPIC_ARGS, id="epic"),
    pytest.param(GOG_ARGS, id="gog"),
    pytest.param(AMAZON_ARGS, id="amazon"),
]


def _owned(**kw: Any) -> Game:
    base: dict[str, Any] = {
        "app_id": -123, "store": "s", "store_game_id": "G1", "title": "T",
    }
    return Game(**{**base, **kw})


def _record(path: str | None, exe: str | None = None) -> dict[str, Any]:
    """One install record in every store's key shape at once."""
    return {"install_path": path, "path": path, "executable": exe}


# ── Converged behaviour: true for every store ──────────────────────────


@pytest.mark.parametrize("args", _ALL_STORES)
def test_installed_when_path_exists(tmp_path: Path, args: dict[str, Any]) -> None:
    merged = merge_install_status(
        [_owned()], {"G1": _record(str(tmp_path))}, **args,
    )[0]

    assert merged.installed is True
    assert merged.install_path == str(tmp_path)


@pytest.mark.parametrize("args", _ALL_STORES)
def test_not_installed_without_an_entry(args: dict[str, Any]) -> None:
    assert merge_install_status([_owned()], {}, **args)[0].installed is False


@pytest.mark.parametrize("args", _ALL_STORES)
def test_not_installed_when_the_path_key_is_absent(args: dict[str, Any]) -> None:
    """The hole this consolidation closed.

    Epic and Amazon used to mark this installed with no path at all,
    because their disk check was itself guarded on the path being truthy.
    """
    merged = merge_install_status([_owned()], {"G1": {"version": "1"}}, **args)[0]

    assert merged.installed is False
    assert merged.install_path is None


@pytest.mark.parametrize("args", _ALL_STORES)
def test_not_installed_when_the_path_is_empty(args: dict[str, Any]) -> None:
    """Reachable for Amazon: ``read_installed_ids`` defaults the key to ""."""
    merged = merge_install_status([_owned()], {"G1": _record("")}, **args)[0]

    assert merged.installed is False
    assert merged.install_path is None


@pytest.mark.parametrize("args", _ALL_STORES)
def test_owned_game_is_never_mutated(tmp_path: Path, args: dict[str, Any]) -> None:
    owned = _owned(tags=["rpg"], metadata={"k": "v"})

    merged = merge_install_status(
        [owned], {"G1": _record(str(tmp_path))}, **args,
    )[0]

    assert owned.installed is False
    assert owned.install_path is None
    # tags/metadata are rebuilt, not shared with the source record.
    merged.tags.append("fps")
    merged.metadata["k2"] = "v2"
    assert owned.tags == ["rpg"]
    assert owned.metadata == {"k": "v"}


@pytest.mark.parametrize("args", _ALL_STORES)
def test_every_other_field_survives(tmp_path: Path, args: dict[str, Any]) -> None:
    """``dataclasses.replace`` carries fields the copies used to hand-list.

    Each of the three deleted copies re-listed all twelve ``Game`` fields,
    so a field added to the dataclass would have been dropped by three
    call sites at once.
    """
    owned = _owned(
        app_id=42, tags=["rpg"], icon_url="i", hero_url="h", logo_url="l",
        size_bytes=99, metadata={"k": "v"},
    )

    merged = merge_install_status(
        [owned], {"G1": _record(str(tmp_path))}, **args,
    )[0]

    assert (merged.app_id, merged.store, merged.store_game_id, merged.title) == (
        42, "s", "G1", "T",
    )
    assert (merged.icon_url, merged.hero_url, merged.logo_url) == ("i", "h", "l")
    assert merged.size_bytes == 99
    assert merged.tags == ["rpg"]
    assert merged.metadata == {"k": "v"}


# ── Deliberate per-store divergence ────────────────────────────────────


def test_epic_and_amazon_reject_a_path_that_no_longer_exists(
    tmp_path: Path,
) -> None:
    """A CLI record can outlive its directory; Steam then shows PLAY."""
    gone = str(tmp_path / "gone")

    for args in (EPIC_ARGS, AMAZON_ARGS):
        merged = merge_install_status([_owned()], {"G1": _record(gone)}, **args)[0]
        assert merged.installed is False
        assert merged.install_path is None


def test_gog_trusts_its_own_walk(tmp_path: Path) -> None:
    """GOG's map is produced by a live ``iterdir``, so it is not re-statted."""
    gone = str(tmp_path / "gone")

    merged = merge_install_status([_owned()], {"G1": _record(gone)}, **GOG_ARGS)[0]

    assert merged.installed is True
    assert merged.install_path == gone


def test_only_gog_carries_the_scanned_executable(tmp_path: Path) -> None:
    """legendary's and nile's equivalent field is a *relative* path.

    Carrying it onto ``exe_path`` would put a relative path into games.map
    and break launch, so ``exe_key`` stays GOG-only.
    """
    record = {"G1": _record(str(tmp_path), exe="/abs/start.sh")}

    assert merge_install_status([_owned()], record, **GOG_ARGS)[0].exe_path == (
        "/abs/start.sh"
    )
    for args in (EPIC_ARGS, AMAZON_ARGS):
        assert merge_install_status([_owned()], record, **args)[0].exe_path is None


def test_gog_falls_back_to_the_owned_exe_path(tmp_path: Path) -> None:
    owned = _owned(exe_path="/previous/start.sh")
    record = {"G1": _record(str(tmp_path), exe=None)}

    merged = merge_install_status([owned], record, **GOG_ARGS)[0]

    assert merged.exe_path == "/previous/start.sh"


# ── The wiring: what each store actually passes ────────────────────────


@pytest.mark.parametrize(
    ("module_path", "expected"),
    [
        ("unifideck.stores.epic.store", EPIC_ARGS),
        ("unifideck.stores.gog.store", GOG_ARGS),
        ("unifideck.stores.amazon.amazon_store", AMAZON_ARGS),
    ],
)
def test_store_get_library_passes_its_own_arguments(
    module_path: str, expected: dict[str, Any],
) -> None:
    """Read the kwargs straight off each ``get_library`` call site.

    The behaviour tests above all pass if a store is rewired to another
    store's semantics — GOG losing ``exe_key`` would break launch after a
    sync and no matrix assertion would notice. This parses the actual call.
    """
    import ast
    import importlib
    import inspect

    module = importlib.import_module(module_path)
    tree = ast.parse(inspect.getsource(module))

    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "merge_install_status"
    ]
    assert len(calls) == 1, f"{module_path}: expected exactly one call site"

    passed = {
        kw.arg: ast.literal_eval(kw.value) for kw in calls[0].keywords if kw.arg
    }
    assert passed == expected
