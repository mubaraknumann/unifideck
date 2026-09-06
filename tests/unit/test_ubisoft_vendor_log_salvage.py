"""UPC's own logs are salvaged before Ubisoft deletes an abandoned prefix.

``prefix_forensics.VENDOR_LOG_GLOBS`` carried a complete, measured
``"ubisoft"`` row — and nothing in ``stores/ubisoft/`` ever called
``preserve_vendor_logs``. Audit §3.3 filed that as redundancy ("only
Battle.net consumes ``shared/prefix_forensics``"); it was a live
lost-diagnostics bug. For a wrapper store the prefix *is* the install, so
``_cleanup_abandoned_prefix`` deleting it took the only first-hand account
of why UPC would not install the game, on every failed and every cancelled
install.

The rules are the failure-path rules ``prefix_forensics`` already states:
never raise, and never be the reason the cleanup does not happen.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from unifideck.stores.shared import prefix_forensics as forensics
from unifideck.stores.ubisoft.installer import installer as ubi_installer


def _upc_prefix(root: Path) -> Path:
    """A Ubisoft prefix with UPC's log layout inside it."""
    prefix = root / "83"
    drive_c = prefix / "pfx" / "drive_c"
    logs = drive_c / "ProgramData" / "Ubisoft" / "Ubisoft Game Launcher" / "logs"
    logs.mkdir(parents=True)
    (logs / "upc.log").write_text("UPC refused the install", encoding="utf-8")
    user_logs = (
        drive_c / "users" / "steamuser" / "AppData" / "Local"
        / "Ubisoft Game Launcher" / "logs"
    )
    user_logs.mkdir(parents=True)
    (user_logs / "launcher.log").write_text("and here is why", encoding="utf-8")
    return prefix


def _installer() -> ubi_installer.UbisoftInstaller:
    inst = ubi_installer.UbisoftInstaller.__new__(ubi_installer.UbisoftInstaller)
    inst._id_map = MagicMock()
    inst._id_map.resolve_prefix_path.return_value = None
    inst._library = MagicMock()
    inst._uninstall_pipeline = MagicMock()
    return inst


def test_upc_logs_are_read_from_a_ubisoft_prefix(tmp_path: Path) -> None:
    """The glob row shipped for a release with no test and no caller."""
    prefix = _upc_prefix(tmp_path)
    out = tmp_path / "launches" / "ubisoft-83.vendor.txt"

    captured = asyncio.run(
        forensics.preserve_vendor_logs("ubisoft", prefix, out),
    )

    assert captured == 2
    text = out.read_text(encoding="utf-8")
    assert "UPC refused the install" in text
    assert "and here is why" in text
    # Labelled by origin, or a triager cannot tell UPC's account from the
    # launcher's.
    assert "upc.log" in text
    assert "launcher.log" in text


def test_the_abandoned_prefix_cleanup_salvages_first(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Order is the whole point: after the deletion there is nothing to read."""
    order: list[str] = []
    prefix = _upc_prefix(tmp_path)

    async def _fake_salvage(store: str, src: Path, dest: Path) -> int:
        order.append(f"salvage:{store}")
        return 1

    async def _fake_cleanup(*_args: Any, **_kwargs: Any) -> bool:
        order.append("delete")
        return True

    monkeypatch.setattr(ubi_installer, "preserve_vendor_logs", _fake_salvage)
    monkeypatch.setattr(ubi_installer, "cleanup_abandoned_prefix", _fake_cleanup)

    asyncio.run(_installer()._cleanup_abandoned_prefix("83", str(prefix)))

    assert order == ["salvage:ubisoft", "delete"]


def test_a_failed_salvage_never_blocks_the_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A prefix the user is waiting on must still be reclaimed.

    ``preserve_vendor_logs`` swallows its own errors, so this drives the
    real function against an unreadable prefix rather than a mock that
    politely returns — the mock would prove nothing about the real one.
    """
    deleted: list[str] = []

    async def _fake_cleanup(*_args: Any, **_kwargs: Any) -> bool:
        deleted.append("yes")
        return True

    monkeypatch.setattr(ubi_installer, "cleanup_abandoned_prefix", _fake_cleanup)

    asyncio.run(
        _installer()._cleanup_abandoned_prefix(
            "83", str(tmp_path / "does-not-exist"),
        ),
    )

    assert deleted == ["yes"]


def test_the_salvage_lands_where_the_support_bundle_looks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``launches/*.vendor.txt`` is the bundle's ``vendor_client_logs`` row.

    A file written anywhere else is salvaged and still never reaches a
    report, which is the whole failure being fixed.
    """
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))

    path = forensics.salvage_path("ubisoft", "83")

    assert path.parent == tmp_path / "unifideck" / "launches"
    assert path.name == "ubisoft-83.vendor.txt"
    assert path.name.endswith(forensics.SUFFIX)
