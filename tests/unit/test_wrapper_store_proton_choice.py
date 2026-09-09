"""A wrapper store's prefix is built and driven by the *same* Proton.

Two resolvers decide which Proton touches a wrapper-store prefix:

  * the **launcher's** selector, for every launch of that prefix, and
  * ``UbisoftBinaryResolver``, for the backend-side umu spawns that
    *create* it — the vendor installer, the prefix warmup, the sign-in.

They disagreed. The backend resolver walked a hardcoded display-name
table (``"Proton - Experimental"``, ``"Proton 10.0"``, ``"Proton 9.0
(Beta)"``) that stopped at Proton 10, so on a ROG Ally X carrying Proton
11.0, GE-Proton11-5, GE-Proton10-34, Proton-CachyOS and EM-10.0-34 the
only name it could still match was Experimental. The Battle.net prefix
was therefore created by one Wine build and every client start after it
ran under another, with nobody having chosen either.

The fix has no table in it, which is the point: the plugin-managed
GE-Proton tag is read from the marker the background installer writes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from unifideck.stores.ubisoft.binaries import UbisoftBinaryResolver
from unifideck.stores.ubisoft.config import UbisoftConfig


@pytest.fixture
def resolver() -> UbisoftBinaryResolver:
    return UbisoftBinaryResolver(UbisoftConfig(), None)


def _install_ge(root: Path, tag: str, *, executable: bool = True) -> Path:
    """A GE-Proton on disk, as ``ge_installer`` leaves one."""
    tool_dir = root / tag
    tool_dir.mkdir(parents=True, exist_ok=True)
    script = tool_dir / "proton"
    script.write_text("#!/usr/bin/env python3\n")
    script.chmod(0o755 if executable else 0o644)
    return tool_dir


def _managed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tag: str | None) -> None:
    """Point ge_installer's marker and scan roots at ``tmp_path``."""
    from unifideck.launcher.proton.infrastructure import ge_installer, ge_marker

    marker = tmp_path / "proton_ge_latest.json"
    if tag is not None:
        marker.write_text(json.dumps({"tag": tag, "installed_at": 0.0}))
    monkeypatch.setattr(ge_marker, "_MARKER", marker)
    monkeypatch.setattr(ge_installer, "_SCAN_ROOTS", (str(tmp_path / "tools"),))


def test_the_managed_ge_proton_wins(
    resolver: UbisoftBinaryResolver, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole fix: the backend builds the prefix with the launcher's Proton."""
    _managed(monkeypatch, tmp_path, "GE-Proton11-5")
    tool = _install_ge(tmp_path / "tools", "GE-Proton11-5")

    assert resolver.find_proton_path() == str(tool)


def test_the_tool_directory_is_returned_not_the_proton_script(
    resolver: UbisoftBinaryResolver, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PROTONPATH wants the directory; the lookup answers with the script.

    Handing umu the script path instead of its parent is a launch that
    fails for a reason no log line explains.
    """
    _managed(monkeypatch, tmp_path, "GE-Proton11-5")
    _install_ge(tmp_path / "tools", "GE-Proton11-5")

    resolved = Path(resolver.find_proton_path())
    assert resolved.is_dir()
    assert (resolved / "proton").is_file()


def test_a_tag_with_no_install_falls_back(
    resolver: UbisoftBinaryResolver, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recorded tag whose directory is gone must not shadow the scan."""
    _managed(monkeypatch, tmp_path, "GE-Proton11-5")
    monkeypatch.setattr(
        UbisoftBinaryResolver, "_find_official_proton",
        staticmethod(lambda: "/steam/common/Proton - Experimental"),
    )
    assert resolver.find_proton_path() == "/steam/common/Proton - Experimental"


def test_a_half_extracted_ge_is_not_used(
    resolver: UbisoftBinaryResolver, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-executable ``proton`` dies with "Permission denied" on exec."""
    _managed(monkeypatch, tmp_path, "GE-Proton11-5")
    _install_ge(tmp_path / "tools", "GE-Proton11-5", executable=False)
    monkeypatch.setattr(
        UbisoftBinaryResolver, "_find_official_proton", staticmethod(lambda: None),
    )
    monkeypatch.setattr(
        UbisoftBinaryResolver, "_find_custom_proton", staticmethod(lambda: None),
    )
    assert resolver.find_proton_path() is None


def test_no_marker_leaves_the_old_behaviour_intact(
    resolver: UbisoftBinaryResolver, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Before the background installer has run, the scan still answers."""
    _managed(monkeypatch, tmp_path, None)
    monkeypatch.setattr(
        UbisoftBinaryResolver, "_find_official_proton",
        staticmethod(lambda: "/steam/common/Proton - Experimental"),
    )
    assert resolver.find_proton_path() == "/steam/common/Proton - Experimental"


def test_the_lookup_never_downloads(
    resolver: UbisoftBinaryResolver, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This runs in the backend, where a Proton download stalls a sign-in.

    ``ensure_latest_ge`` fetches and installs; only the cached-tag read is
    allowed here.
    """
    from unifideck.launcher.proton.infrastructure import ge_installer, ge_marker

    _managed(monkeypatch, tmp_path, None)

    def _boom(*_a: object, **_k: object) -> None:
        raise AssertionError("the backend resolver must never download a Proton")

    monkeypatch.setattr(ge_installer, "ensure_latest_ge", _boom)
    monkeypatch.setattr(
        UbisoftBinaryResolver, "_find_official_proton", staticmethod(lambda: None),
    )
    monkeypatch.setattr(
        UbisoftBinaryResolver, "_find_custom_proton", staticmethod(lambda: None),
    )
    assert resolver.find_proton_path() is None


def test_a_broken_lookup_does_not_break_resolution(
    resolver: UbisoftBinaryResolver, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The managed lookup is an improvement, never a new way to fail."""
    from unifideck.launcher.proton.infrastructure import ge_installer, ge_marker

    def _raise() -> str:
        raise OSError("marker unreadable")

    monkeypatch.setattr(ge_marker, "read_cached_latest_tag", _raise)
    monkeypatch.setattr(
        UbisoftBinaryResolver, "_find_official_proton",
        staticmethod(lambda: "/steam/common/Proton - Experimental"),
    )
    assert resolver.find_proton_path() == "/steam/common/Proton - Experimental"
