"""The template follows the newest Ubisoft Connect build on disk.

A game prefix is a clone of ``.template``. When the template's UPC was older
than the build Ubisoft ships, every clone patched itself on first launch, and
UPC 175 came out of that patch signed out (measured 2026-11-14, Rayman Origins
install: 13316 → 175.1.13416, vault rewritten without its ticket). A template
clone already on 13416 started signed in with no restart. So before cloning we
copy the newest build found in ``.upc-auth`` or a game prefix into the
template: program files only, never games, saves or the session.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from unifideck.stores.ubisoft.prefix import upc_build

LAUNCHER = Path("drive_c/Program Files (x86)/Ubisoft/Ubisoft Game Launcher")


def _prefix(root: Path, build: int, *, pfx: bool = False) -> Path:
    launcher = (root / "pfx" if pfx else root) / LAUNCHER
    launcher.mkdir(parents=True)
    (launcher / "version.txt").write_text(f"{build}\n")
    (launcher / "upc.exe").write_text(f"upc {build}")
    return root


@pytest.fixture(autouse=True)
def _nothing_running(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(upc_build, "_upc_running", lambda _p: False)


def test_reads_the_build_in_either_layout(tmp_path: Path):
    assert upc_build.upc_build(_prefix(tmp_path / "a", 13316)) == 13316
    assert upc_build.upc_build(_prefix(tmp_path / "b", 13416, pfx=True)) == 13416
    assert upc_build.upc_build(tmp_path / "missing") is None


def test_template_takes_the_newest_build_program_files_only(tmp_path: Path):
    template = _prefix(tmp_path / ".template", 13316)
    auth = _prefix(tmp_path / ".upc-auth", 13333)
    game = _prefix(tmp_path / "80", 13416, pfx=True)
    game_launcher = game / "pfx" / LAUNCHER
    for private in ("games/Rayman", "savegames/42", "logs", "data"):
        (game_launcher / private).mkdir(parents=True)
        (game_launcher / private / "f").write_text("per prefix")

    assert asyncio.run(
        upc_build.refresh_template_build(template, [str(auth), str(game)]),
    )

    tl = template / LAUNCHER
    assert (tl / "version.txt").read_text().strip() == "13416"
    assert (tl / "upc.exe").read_text() == "upc 13416"
    for private in ("games", "savegames", "logs", "data"):
        assert not (tl / private).exists(), private


def test_never_downgrades_the_template(tmp_path: Path):
    template = _prefix(tmp_path / ".template", 13416)
    auth = _prefix(tmp_path / ".upc-auth", 13333)
    assert not asyncio.run(upc_build.refresh_template_build(template, [str(auth)]))
    assert upc_build.upc_build(template) == 13416


def test_never_copies_while_upc_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    template = _prefix(tmp_path / ".template", 13316)
    game = _prefix(tmp_path / "80", 13416)
    monkeypatch.setattr(upc_build, "_upc_running", lambda p: Path(p) == game)
    assert not asyncio.run(upc_build.refresh_template_build(template, [str(game)]))
    assert upc_build.upc_build(template) == 13316


def test_falls_back_to_the_next_newest_idle_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    template = _prefix(tmp_path / ".template", 13316)
    auth = _prefix(tmp_path / ".upc-auth", 13333)
    game = _prefix(tmp_path / "80", 13416)
    monkeypatch.setattr(upc_build, "_upc_running", lambda p: Path(p) == game)
    assert asyncio.run(
        upc_build.refresh_template_build(template, [str(auth), str(game)]),
    )
    assert upc_build.upc_build(template) == 13333


def test_a_failure_never_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def _boom(_p: object) -> int:
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(upc_build, "upc_build", _boom)
    assert not asyncio.run(
        upc_build.refresh_template_build(tmp_path, [str(tmp_path)]),
    )
