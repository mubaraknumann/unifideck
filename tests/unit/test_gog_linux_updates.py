"""Regression: GOG Linux installs are updated from the Linux installer.

The update checker compared every install against the Windows build id
and ran ``gogdl update --platform windows``. A Linux install therefore
always showed "update available", and updating it wrote the Windows build
into the Linux folder (seen on Shogun Showdown: ``ShogunShowdown.exe`` and
``UnityPlayer.dll`` next to ``start.sh`` and ``game/``).
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from unifideck.stores.gog import linux_updates
from unifideck.stores.gog.linux_updates import (
    MARKER_KEY,
    linux_update_state,
    pick_linux_installer_version,
)
from unifideck.stores.gog.updates import GOGUpdatesChecker


def _product(*installers: tuple[str, str, str]) -> dict[str, Any]:
    return {"downloads": {"installers": [
        {"os": os_, "language": lang, "version": ver} for os_, lang, ver in installers
    ]}}


def _linux_install(root: Path, gameinfo_version: str, marker: dict[str, Any]) -> Path:
    root.mkdir(parents=True)
    (root / "start.sh").write_text("#!/bin/bash\n")
    (root / "gameinfo").write_text(f"Some Game\n{gameinfo_version}\nn/a\n")
    (root / ".unifideck-id").write_text(json.dumps(marker))
    return root


# ── picking the installer version ────────────────────────────────────────


def test_pick_prefers_install_language_then_english():
    product = _product(
        ("windows", "en", "w1"), ("linux", "de", "1.0 (gog-2)"), ("linux", "en", "1.0 (gog-3)"),
    )
    assert pick_linux_installer_version(product, "de-DE") == "1.0 (gog-2)"
    assert pick_linux_installer_version(product, "fr-FR") == "1.0 (gog-3)"


def test_pick_returns_none_without_a_linux_installer():
    assert pick_linux_installer_version(_product(("windows", "en", "1")), "en-US") is None


# ── deciding whether an update exists ────────────────────────────────────


def test_recorded_version_decides(tmp_path):
    install = _linux_install(tmp_path / "g", "2.0.0.3", {MARKER_KEY: "(gog-3)"})
    assert linux_update_state(str(install), "(gog-3)") is False
    assert linux_update_state(str(install), "(gog-4)") is True


def test_gameinfo_match_counts_as_current_without_a_record(tmp_path):
    install = _linux_install(tmp_path / "g", "1.0.2.1", {})
    assert linux_update_state(str(install), "1.0.2.1") is False


def test_unrelated_gameinfo_scheme_is_unknown_not_an_update(tmp_path):
    # DOSBox titles: gameinfo says 2.0.0.3 while the API says (gog-3).
    install = _linux_install(tmp_path / "g", "2.0.0.3", {})
    assert linux_update_state(str(install), "(gog-3)") is None
    assert linux_update_state(str(install), None) is None


# ── the checker ──────────────────────────────────────────────────────────


def _checker(install: Path, gogdl: str = "/nonexistent/gogdl") -> GOGUpdatesChecker:
    tokens = SimpleNamespace(
        refresh_if_stale=AsyncMock(return_value=True),
        access_token=None,
        acquire_gogdl_creds=AsyncMock(return_value=({}, "creds.json", AsyncMock())),
    )
    config = SimpleNamespace(api_gog_url="https://api.gog.com", user_agent="test")
    return GOGUpdatesChecker(
        config=config,  # type: ignore[arg-type]
        tokens=tokens,  # type: ignore[arg-type]
        gogdl_bin=gogdl,
        get_installed_ids=lambda: ["1104084973"],
        resolve_install_info=lambda _gid: {"install_path": str(install)},
    )


async def test_linux_install_is_never_compared_with_the_windows_build(tmp_path, monkeypatch):
    install = _linux_install(tmp_path / "g", "1.0.2.1", {"language": "en-US"})
    monkeypatch.setattr(
        linux_updates, "fetch_json_get",
        AsyncMock(return_value=_product(("linux", "en", "1.0.2.1"), ("windows", "en", "9"))),
    )
    checker = _checker(install)
    checker._fetch_remote_build_id = AsyncMock(return_value="59059134842096269")  # type: ignore[method-assign]
    assert await checker.check_for_game_update("1104084973") is False
    checker._fetch_remote_build_id.assert_not_awaited()


async def test_linux_update_runs_gogdl_for_linux_and_records_the_version(
    tmp_path, monkeypatch,
):
    install = _linux_install(tmp_path / "g", "1.0.2.1", {MARKER_KEY: "1.0.2.1"})
    gogdl = tmp_path / "gogdl"
    gogdl.write_text("")
    monkeypatch.setattr(
        linux_updates, "fetch_json_get",
        AsyncMock(return_value=_product(("linux", "en", "1.0.3"))),
    )
    proc = MagicMock(returncode=0)
    proc.stdout.readline = AsyncMock(return_value=b"")
    proc.wait = AsyncMock(return_value=0)
    spawn = AsyncMock(return_value=proc)
    monkeypatch.setattr("asyncio.create_subprocess_exec", spawn)

    result = await _checker(install, str(gogdl)).update_game("1104084973")

    assert result.success
    argv = list(spawn.await_args.args)
    assert argv[argv.index("--platform") + 1] == "linux"
    marker = json.loads((install / ".unifideck-id").read_text())
    assert marker[MARKER_KEY] == "1.0.3"


async def test_install_marker_records_the_linux_installer_version(tmp_path, monkeypatch):
    from unifideck.stores.gog.install import marker as marker_mod

    install = tmp_path / "g"
    install.mkdir()
    (install / "start.sh").write_text("#!/bin/bash\n")
    monkeypatch.setattr(
        marker_mod, "fetch_linux_installer_version", AsyncMock(return_value="(gog-3)"),
    )
    parent = SimpleNamespace(
        _config=SimpleNamespace(api_gog_url="https://api.gog.com", user_agent="test"),
        _tokens=SimpleNamespace(access_token=None),
    )
    writer = marker_mod._PostInstallMarker(parent)  # type: ignore[arg-type]
    assert await writer.write_install_marker(str(install), "1432575012", "en-US")
    marker = json.loads((install / ".unifideck-id").read_text())
    assert marker[MARKER_KEY] == "(gog-3)"
    assert marker["language"] == "en-US"
