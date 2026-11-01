"""Regression: an enriched save row must never resolve to the game itself.

Real unifiDB rows for GOG DOSBox titles (``<base>/SAVE*.SAV``,
``<base>/CHARSAVE.GFF``) resolved to the install folder, and Shogun
Showdown's ``<home>/AppData/LocalLow/...`` resolved to a made-up folder
under ``~`` for its native build. A forced cloud download empties the save
dir first and an upload sends all of it, so the install folder as a save
dir meant deleting the game or uploading it whole.
"""
from __future__ import annotations

from unifideck.services.cloud_save.path_resolver import WinePrefixResolver
from unifideck.services.cloud_save.save_location_resolver import resolve_save_dir


class _Cache:
    def __init__(self, rows):
        self._rows = rows

    def get(self, namespace, _key):
        return {"save_locations": self._rows} if namespace == "metadata" else None


def _resolve(rows, install, *, native=True):
    return resolve_save_dir(
        "gog", "1", prefix_path="", install_path=str(install),
        cache=_Cache(rows), native_linux=native,
    )


def test_wildcard_after_base_never_returns_the_install_folder(tmp_path):
    assert _resolve([{"path": "<base>/SAVE*.SAV", "tags": ["save"]}], tmp_path) is None
    assert _resolve(
        [{"path": "<base>/SAVE*.SAV", "tags": ["save"]}], tmp_path, native=False,
    ) is None


def test_a_file_row_means_its_folder_not_a_folder_named_after_it(tmp_path):
    (tmp_path / "data").mkdir()
    assert _resolve([{"path": "<base>/EOBDATA.SAV", "tags": ["save"]}], tmp_path) is None
    assert _resolve(
        [{"path": "<base>/data/EOBDATA.SAV", "tags": ["save"]}], tmp_path,
    ) == str(tmp_path / "data")


def test_native_build_ignores_windows_appdata_under_home():
    assert WinePrefixResolver.resolve_ludusavi_path(
        "<home>/AppData/LocalLow/Roboatino/ShogunShowdown", "", "/x", native_linux=True,
    ) is None


def test_dotted_folder_names_stay_folders(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    resolved = WinePrefixResolver.resolve_ludusavi_path(
        "<xdgConfig>/com.Roboatino.ShogunShowdown", "", "/x", native_linux=True,
    )
    assert resolved == str(tmp_path / "cfg" / "com.Roboatino.ShogunShowdown")
