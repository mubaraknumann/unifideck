"""Tests for GOG's install-mode cleanup refusing to delete a foreign install.

``determine_install_mode`` treats data it does not recognise in its target
folder as orphaned and used to ``shutil.rmtree`` the whole directory. But
"orphaned" only means *this game* left no ``goggame-<id>.info`` there — it says
nothing about who else owns the directory. Every store derives its folder name
from the title, so ``<root>/Bastion`` is GOG's folder for Bastion *and* the
natural target for a ``Bastion.zip`` in a GameVault vault.

On a real device (2026-09-01) that cost the user their files: four GOG install
attempts each logged ``removed orphan .../Games/Bastion`` and deleted the
GameVault extraction living inside it, leaving a games.map row pointing at a
path that no longer existed.

The guard is ownership via games.map, which is the one file that records, per
shortcut, the install actually in use.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from unifideck.stores.gog.install.planner import (
    INSTALL_MODE_BLOCKED,
    GOGInstallPlanner,
)


@pytest.fixture
def games_map_at(tmp_path, monkeypatch):
    """Point the ownership check at a games.map this test controls."""
    path = tmp_path / "games.map"
    path.write_text("")
    monkeypatch.setattr(
        "unifideck.utils.paths.get_games_map_path", lambda config=None: str(path),
    )
    return path


def _planner(tmp_path) -> GOGInstallPlanner:
    """A planner whose only exercised dependency is the support-cache dir."""
    config: Any = type(
        "_Config", (), {"gogdl_config_dir": str(tmp_path / "gogdl")},
    )()
    return GOGInstallPlanner(config, tokens=None)  # type: ignore[arg-type]


def _orphan_data(folder: Path, *, size: int = 200 * 1024 * 1024) -> None:
    """Fill *folder* with enough non-GOG data to trip the orphan branch.

    Over ``_CORRUPT_INSTALL_SIZE_THRESHOLD`` and with no ``goggame-*.info``,
    which is exactly the shape a foreign install presents.
    """
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "payload.bin").write_bytes(b"\0" * size)


async def test_orphan_cleanup_refuses_a_folder_another_store_owns(
    tmp_path, games_map_at,
):
    target = tmp_path / "Games" / "Bastion"
    _orphan_data(target / "Bastion" / "game")
    games_map_at.write_text(
        f"gamevault:lv_08f29ef0afcb2cc7={target}/Bastion/game/Bastion.exe"
        f"\t{target}\t-1282809411\n",
    )

    mode = await _planner(tmp_path).determine_install_mode(
        "1423058311", str(target),
    )

    assert mode == INSTALL_MODE_BLOCKED
    # The whole point: the other store's files are still there.
    assert (target / "Bastion" / "game" / "payload.bin").exists()


async def test_orphan_cleanup_still_removes_genuinely_orphaned_data(
    tmp_path, games_map_at,
):
    """No foreign row → the original behaviour is unchanged."""
    target = tmp_path / "Games" / "Bastion"
    _orphan_data(target)

    mode = await _planner(tmp_path).determine_install_mode(
        "1423058311", str(target),
    )

    assert mode == "download"
    assert not target.exists()


async def test_orphan_cleanup_ignores_gogs_own_row(tmp_path, games_map_at):
    """A row for the game being installed is not foreign."""
    target = tmp_path / "Games" / "Bastion"
    _orphan_data(target)
    games_map_at.write_text(
        f"gog:1423058311={target}/game/Bastion.exe\t{target}\t-61032104\n",
    )

    mode = await _planner(tmp_path).determine_install_mode(
        "1423058311", str(target),
    )

    assert mode == "download"
    assert not target.exists()


async def test_corrupt_cleanup_refuses_a_folder_another_store_owns(
    tmp_path, games_map_at,
):
    """The corrupt-install branch carries the same risk and the same guard."""
    target = tmp_path / "Games" / "Bastion"
    target.mkdir(parents=True)
    # Small (under the corrupt threshold) but carrying this game's info file.
    (target / "goggame-1423058311.info").write_text("{}")
    (target / "payload.bin").write_bytes(b"\0" * 1024)
    games_map_at.write_text(
        f"gamevault:lv_1={target}/Other/Game.exe\t{target}/Other\t-1\n",
    )

    mode = await _planner(tmp_path).determine_install_mode(
        "1423058311", str(target),
    )

    assert mode == INSTALL_MODE_BLOCKED
    assert (target / "payload.bin").exists()
