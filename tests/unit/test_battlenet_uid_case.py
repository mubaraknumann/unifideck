"""Battle.net's uid join must survive Blizzard's own inconsistent casing.

The reported bug: a Diablo install finished in the client, and the plugin went
on saying "installing" until the watchdog gave up five minutes later with
"The install was never finished in Battle.net". Measured on device::

    13:02:05  DownloadWorker  starting install for battlenet:D1
    13:04     Diablo appears at drive_c/Program Files (x86)/Diablo
    (no "detected install at" line — ever)
    13:11:04  battlenet       Battle.net gone for ~300s — abandoned
    13:11:04  DownloadWorker  failed install for battlenet:D1

Cause: Blizzard's PUB catalog spells Diablo's retail uid ``D1``, but everything
the *client* writes — ``product.db``, ``aggregate.json``, the Agent logs — says
``d1``. ``_index_by_uid`` keyed on the client's spelling while every caller
asked with the catalog's, so the dict lookup missed and ``row()`` returned
``None`` forever. Three titles carry an uppercase uid today: ``D1``, ``W1``
and ``W2`` (plus ``D1H``); everything else is lowercase, which is why this
went unnoticed.

The same miss has two more consequences, covered below: the granted tile
reports ``installed=False``, and ``_orphan_installed`` then re-adds the game a
second time under the client's lowercase uid — with a family taken from the
product code, which launches nothing.

Only the *join* is normalized. ``store_game_id`` keeps its case because it keys
every existing user's Steam shortcut, and the id map keeps its case because it
is written and read with the same catalog uid.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from unifideck.stores.battlenet import library as lib
from unifideck.stores.battlenet.install_watch import BattlenetInstallProbe
from unifideck.stores.battlenet.ownership import InstalledGame

CLIENT_UID = "d1"
CATALOG_UID = "D1"


def _installed(**over: Any) -> InstalledGame:
    """A finished Diablo row, spelled the way the client spells it."""
    fields: dict[str, Any] = {
        "code": "drtl",
        "uid": CLIENT_UID,
        "name": "Diablo",
        "install_path": "C:/Program Files (x86)/Diablo",
        "host_install_path": "/pfx/drive_c/Program Files (x86)/Diablo",
        "host_exe_path": "/pfx/drive_c/Program Files (x86)/Diablo/Diablo.exe",
        "total_bytes": 830770758,
        "is_ready": True,
    }
    fields.update(over)
    return InstalledGame(**fields)


def test_the_index_is_keyed_case_insensitively() -> None:
    """``product.db`` says ``d1``; the index must be askable as ``D1``."""
    index = lib._index_by_uid({"drtl": _installed()})

    assert lib.install_row_for(index, CATALOG_UID) is not None
    assert lib.install_row_for(index, CLIENT_UID) is not None


def test_lookup_normalizes_both_directions() -> None:
    """A catalog that went the other way must work too."""
    index = lib._index_by_uid({"drtl": _installed(uid="HS_BETA")})

    assert lib.install_row_for(index, "hs_beta") is not None


def test_a_missing_uid_is_still_missing() -> None:
    """Normalizing must not turn an absent row into a present one."""
    index = lib._index_by_uid({"drtl": _installed()})

    assert lib.install_row_for(index, "w2") is None


def test_the_probe_detects_an_uppercase_uid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact failure: probe asks ``D1``, client wrote ``d1``.

    Before the fix ``row()`` returned ``None``, so ``detect`` never fired and
    the install was failed as abandoned despite being complete on disk.
    """
    probe = BattlenetInstallProbe(CATALOG_UID, tmp_path)
    monkeypatch.setattr(
        lib, "install_state_by_uid", lambda *_: {CLIENT_UID: _installed()},
    )
    monkeypatch.setattr(
        "unifideck.stores.battlenet.paths.drive_c", lambda p: tmp_path,
    )

    assert probe.row() is not None
    assert probe.detect(probe.snapshot()) is not None
    assert probe.is_complete("/anywhere") is True


def test_an_uppercase_uid_does_not_produce_a_duplicate_tile() -> None:
    """The granted tile and the orphan sweep must agree this is one game.

    ``_orphan_installed`` re-added Diablo under ``d1`` because ``"d1"`` is not
    in ``{"D1"}``, giving two Steam shortcuts for one game — the second with
    ``family="drtl"``, which the client silently refuses to launch.
    """
    seen = {lib.normalize_uid(CATALOG_UID)}

    orphans = lib._orphan_installed(
        {"drtl": _installed()}, _EmptyCatalog(), seen, "/launcher",
    )

    assert orphans == []


def test_an_unrelated_installed_orphan_is_still_kept() -> None:
    """The dedup must not swallow a genuinely ungranted install."""
    seen = {lib.normalize_uid(CATALOG_UID)}

    orphans = lib._orphan_installed(
        {"w2": _installed(code="w2", uid="w2", name="Warcraft II")},
        _EmptyCatalog(), seen, "/launcher",
    )

    assert len(orphans) == 1


class _EmptyCatalog:
    """A catalog that knows nothing — forces the uid fallback path."""

    entries: dict[str, Any] = {}

    def entry_for(self, _code: str) -> None:
        return None

    def display_name(self, _program: str) -> None:
        return None
