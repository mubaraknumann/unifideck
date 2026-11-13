"""Ubisoft catalog naming: ``configurations`` vs ``configurations.vdat``.

Ubisoft Connect builds since 2026-11 write the catalog to
``cache/configuration/configurations.vdat`` while older builds use the
extension-less ``configurations``. ``UbisoftPrefixPaths.find_configurations``
only ever probed the legacy name, so discovery failed outright and the
loader logged "no configurations binary found" with ``ubisoft: 0 games``
(issue #524).

The resolution now tries the legacy name first and falls back to the
``.vdat`` variant, and the loader emits a self-explanatory warning when a
``.vdat`` catalog parses to nothing (its payload is an opaque/encrypted
cache, which is a separate, parser-side problem).
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from unifideck.stores.ubisoft.config import UbisoftConfig
from unifideck.stores.ubisoft.library import data_loader as data_loader_module
from unifideck.stores.ubisoft.library.data_loader import _DataLoader
from unifideck.stores.ubisoft.paths import UbisoftPrefixPaths

_CATALOG_DIR = Path(
    "drive_c/users/steamuser/AppData/Local/Ubisoft Game Launcher/cache/configuration",
)


def _paths() -> UbisoftPrefixPaths:
    return UbisoftPrefixPaths(UbisoftConfig())


def _write(prefix: Path, name: str, *, below_pfx: bool = False) -> Path:
    root = prefix / "pfx" if below_pfx else prefix
    target = root / _CATALOG_DIR / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\x0a\x04test")
    return target


# ── path discovery ────────────────────────────────────────────────


def test_finds_vdat_catalog(tmp_path: Path) -> None:
    target = _write(tmp_path, "configurations.vdat")
    assert _paths().find_configurations(str(tmp_path)) == str(target)


def test_prefers_legacy_extensionless_name(tmp_path: Path) -> None:
    legacy = _write(tmp_path, "configurations")
    _write(tmp_path, "configurations.vdat")
    assert _paths().find_configurations(str(tmp_path)) == str(legacy)


def test_finds_vdat_catalog_below_pfx(tmp_path: Path) -> None:
    target = _write(tmp_path, "configurations.vdat", below_pfx=True)
    assert _paths().find_configurations(str(tmp_path)) == str(target)


def test_returns_none_when_no_catalog_exists(tmp_path: Path) -> None:
    assert _paths().find_configurations(str(tmp_path)) is None


# ── loader diagnostic ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_loader_warns_about_opaque_vdat_catalog(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = _write(tmp_path, "configurations.vdat")
    loader = _DataLoader(config=UbisoftConfig(), paths=_paths())
    monkeypatch.setattr(
        loader,
        "_find_library_configurations_path",
        lambda: str(catalog),
    )

    def _parse_empty(_path: str) -> list[object]:
        return []

    with caplog.at_level(
        logging.WARNING,
        logger=data_loader_module.__name__,
    ):
        result = await loader.load_configurations(_parse_empty)

    assert result is None
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "configurations.vdat" in message and "524" in message for message in messages
    ), messages


@pytest.mark.asyncio
async def test_loader_keeps_legacy_empty_message(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = _write(tmp_path, "configurations")
    loader = _DataLoader(config=UbisoftConfig(), paths=_paths())
    monkeypatch.setattr(
        loader,
        "_find_library_configurations_path",
        lambda: str(catalog),
    )

    def _parse_empty(_path: str) -> list[object]:
        return []

    with caplog.at_level(
        logging.WARNING,
        logger=data_loader_module.__name__,
    ):
        await loader.load_configurations(_parse_empty)

    messages = [record.getMessage() for record in caplog.records]
    assert "configurations binary parsed but empty" in " ".join(messages), messages
