"""Cross-distro portability guards (Bazzite / CachyOS vs SteamOS).

These tests lock in the SteamOS-assumption fixes so they don't regress:

* ``find_steam_path`` must resolve under any ``$HOME`` (not just
  ``/home/deck``) — Bazzite/CachyOS users pick their own username.
* the Ubisoft SD-card default must resolve the real mounted removable
  media instead of the Deck's ``/run/media/mmcblk0p1`` device node, and
  fall back harmlessly when nothing is mounted.
* the Proton compat/library scan roots must not depend solely on the
  ``~/.steam/root`` symlink — ``~/.steam/steam`` is listed too.
* the launcher's cffi-backend probe (which decides graceful cloud-save
  degradation) must return a bool without raising.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from unifideck.steam.library import find_steam_path
from unifideck.stores.ubisoft.config import _detect_sdcard_install_base


class _FakeConfig:
    """Minimal ConfigManager stand-in: ``get(key, default)`` over a dict."""

    def __init__(self, values: dict[str, object] | None = None) -> None:
        self._v = values or {}

    def get(self, key: str, default: object = None) -> object:
        return self._v.get(key, default)


def _make_steam(home: Path, sub: str, *, user: str | None = "12345") -> Path:
    """Create a Steam root at ``home/<sub>`` (steamapps + optional userdata)."""
    steam = home / sub
    (steam / "steamapps").mkdir(parents=True)
    if user is not None:
        (steam / "userdata" / user).mkdir(parents=True)
    return steam


def test_find_steam_path_resolves_under_non_deck_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A non-'deck' username must still locate native Steam."""
    home = tmp_path / "bazzite-user"
    steam = home / ".steam" / "steam"
    (steam / "steamapps").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))

    assert find_steam_path(None) == str(steam)


def test_find_steam_path_uses_local_share_when_dot_steam_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """0.6.1 parity: probe ``~/.local/share/Steam`` when ``~/.steam`` is absent."""
    home = tmp_path / "cachyos-user"
    steam = home / ".local" / "share" / "Steam"
    (steam / "steamapps").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))

    assert find_steam_path(None) == str(steam)


def test_find_steam_path_honors_configured_candidates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The advertised ``paths.steam_candidates`` config is actually read."""
    home = tmp_path / "weird"
    monkeypatch.setenv("HOME", str(home))
    custom = tmp_path / "opt" / "steam"
    (custom / "steamapps").mkdir(parents=True)

    cfg = _FakeConfig({"paths.steam_candidates": [str(custom)]})
    assert find_steam_path(cfg) == str(custom)


def _login_vdf(account_id: int, timestamp: int) -> str:
    """A minimal, vdf-parseable ``loginusers.vdf`` with one MostRecent user."""
    steam64 = 76561197960265728 + account_id
    return (
        '"users"\n{\n'
        f'\t"{steam64}"\n\t{{\n'
        '\t\t"AccountName"\t\t"acct"\n'
        '\t\t"MostRecent"\t\t"1"\n'
        f'\t\t"Timestamp"\t\t"{timestamp}"\n'
        "\t}\n}\n"
    )


def _make_install(
    root: Path, *, account_id: int, timestamp: int, mtime: float,
) -> Path:
    """Build a Steam install: steamapps + userdata/<id> + a dated loginusers.vdf.

    ``timestamp``/``mtime`` control the liveness score so a test can express
    "this install was last used long ago" vs "this one is running now".
    """
    (root / "steamapps").mkdir(parents=True)
    (root / "userdata" / str(account_id)).mkdir(parents=True)
    login = root / "config" / "loginusers.vdf"
    login.parent.mkdir(parents=True, exist_ok=True)
    login.write_text(_login_vdf(account_id, timestamp))
    os.utime(login, (mtime, mtime))
    return root


def test_find_steam_path_prefers_running_install_over_stale(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Reporter bug: a stale native ~/.steam/steam must not shadow live Flatpak.

    Both roots have steamapps/, so the pre-fix 'first candidate wins' picked the
    stale native root and 228 shortcuts landed where the running client never
    reads them — 'synced but nothing shows in Steam', restart-immune.
    """
    home = tmp_path / "flatpak-user"
    monkeypatch.setenv("HOME", str(home))
    stale = _make_install(
        home / ".steam" / "steam",
        account_id=40677867, timestamp=1_000_000_000, mtime=1_000_000_000,
    )
    live = _make_install(
        home / ".var/app/com.valvesoftware.Steam/.steam/steam",
        account_id=225630054, timestamp=2_000_000_000, mtime=2_000_000_000,
    )

    assert find_steam_path(None) == str(live)
    assert find_steam_path(None) != str(stale)


def test_service_paths_target_the_live_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The whole write chain (root + active_user) follows the live install.

    shortcuts.vdf must resolve under the *running* Flatpak's userdata/<id>, not
    the stale native root's — this is what makes the games actually appear.
    """
    from unifideck.services.bootstrap.paths import ServicePaths

    home = tmp_path / "flatpak-user"
    monkeypatch.setenv("HOME", str(home))
    _make_install(
        home / ".steam" / "steam",
        account_id=40677867, timestamp=1_000_000_000, mtime=1_000_000_000,
    )
    live = _make_install(
        home / ".var/app/com.valvesoftware.Steam/.steam/steam",
        account_id=225630054, timestamp=2_000_000_000, mtime=2_000_000_000,
    )

    sp = ServicePaths.from_config(
        _FakeConfig(), plugin_dir=str(tmp_path / "plug"),
    )

    assert sp.steam_root == str(live)
    assert sp.shortcuts_path == str(
        live / "userdata" / "225630054" / "config" / "shortcuts.vdf",
    )


def test_service_paths_write_target_follows_probed_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Symptom A: shortcuts.vdf lands under the PROBED root, not ~/.steam/steam.

    When only ``~/.local/share/Steam`` exists (no ``~/.steam`` symlink), the
    reconcile's write path must resolve there — else shortcuts get written to
    a root Steam never reads and 'nothing shows after sync'.
    """
    from unifideck.services.bootstrap.paths import ServicePaths

    home = tmp_path / "cachyos-user"
    steam = _make_steam(home, ".local/share/Steam", user="12345")
    monkeypatch.setenv("HOME", str(home))

    sp = ServicePaths.from_config(_FakeConfig(), plugin_dir=str(tmp_path / "plug"))

    assert sp.steam_root == str(steam)
    assert sp.shortcuts_path == str(
        steam / "userdata" / "12345" / "config" / "shortcuts.vdf",
    )


def test_service_paths_falls_back_when_no_steam(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """No Steam anywhere → hermetic fallback to ~/.steam/steam, no crash."""
    from unifideck.services.bootstrap import paths as bootstrap_paths

    home = tmp_path / "empty-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    fallback = str(home / ".steam" / "steam")
    monkeypatch.setattr(bootstrap_paths, "_DEFAULT_STEAM_ROOT", fallback)

    sp = bootstrap_paths.ServicePaths.from_config(
        _FakeConfig(), plugin_dir=str(tmp_path / "plug"),
    )
    assert sp.steam_root == fallback


def test_find_steam_path_none_when_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """No Steam install anywhere under HOME → None (not a crash/hardcode)."""
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    assert find_steam_path(None) is None


def test_sdcard_base_falls_back_without_mounts(tmp_path: Path) -> None:
    """No removable media mounted → harmless historical Deck fallback.

    The point is that it never *requires* the Deck device to exist; the
    fallback string is inert on other distros (the path just won't exist).
    """
    empty = tmp_path / "run-media-empty"  # does not exist
    assert _detect_sdcard_install_base(empty) == (
        "/run/media/mmcblk0p1/Games/Ubisoft"
    )


def test_sdcard_base_detects_flat_mount(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """SteamOS-style flat layout: /run/media/<label> is the mountpoint."""
    media = tmp_path / "run-media"
    label = media / "MYSDCARD"
    label.mkdir(parents=True)

    monkeypatch.setattr(os.path, "ismount", lambda p: str(p) == str(label))
    monkeypatch.setattr(os, "access", lambda p, mode: True)

    assert _detect_sdcard_install_base(media) == str(
        label / "Games" / "Ubisoft",
    )


def test_sdcard_base_detects_nested_udisks_mount(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """udisks2 layout: /run/media/<user>/<label> is the mountpoint."""
    media = tmp_path / "run-media"
    label = media / "bazzite" / "MYSDCARD"
    label.mkdir(parents=True)

    # Only the deepest <label> dir is a real mount; the <user> dir is not.
    monkeypatch.setattr(os.path, "ismount", lambda p: str(p) == str(label))
    monkeypatch.setattr(os, "access", lambda p, mode: True)

    assert _detect_sdcard_install_base(media) == str(
        label / "Games" / "Ubisoft",
    )


def test_proton_roots_include_steam_steam_dir() -> None:
    """Compat/library resolution must not depend solely on ~/.steam/root."""
    from unifideck.launcher.proton.infrastructure import selector
    from unifideck.utils import vdf_compat

    assert "~/.steam/steam/compatibilitytools.d" in vdf_compat.STEAM_COMPAT_ROOTS
    assert "~/.steam/steam/steamapps/common" in selector.STEAM_LIBRARY_ROOTS


def test_ge_installer_scan_roots_include_steam_steam_dir() -> None:
    from unifideck.launcher.proton.infrastructure import ge_installer

    assert (
        "~/.steam/steam/compatibilitytools.d" in ge_installer._SCAN_ROOTS
    )


def test_cffi_backend_probe_returns_bool() -> None:
    """The graceful-degradation probe must never raise."""
    from unifideck.services.launcher.helpers import _cffi_backend_available

    assert isinstance(_cffi_backend_available(), bool)


# ── Symptom B: Proton resolution on Bazzite/CachyOS ───────────────

_CONFIG_VDF_GLOBAL = (
    '"InstallConfigStore" { "Software" { "Valve" { "Steam" {\n'
    '  "CompatToolMapping" {\n'
    '    "0"   { "name" "proton-cachyos" }\n'
    '    "480" { "name" "GE-Proton8-30" }\n'
    '  }\n'
    '} } } }\n'
)


def _system_proton(tmp_path: Path, name: str) -> Path:
    """Create ``<tmp>/system/<name>/proton`` and return the compat root dir.

    Builds a *structurally complete* install (executable ``proton``,
    non-empty ``files/bin/wine`` payload, ``version``, and a
    ``toolmanifest.vdf`` — every real Proton ships one, and umu parses it on
    every launch) so it passes the selector's install-completeness
    validation. The selection tiers skip a truncated/half-extracted Proton,
    so a stub would be (correctly) rejected and fall through to GE.
    """
    import os
    import stat

    root = tmp_path / "system"
    tool = root / name
    (tool / "files" / "bin").mkdir(parents=True)
    proton = tool / "proton"
    proton.write_text("#!/bin/sh\n")
    os.chmod(proton, proton.stat().st_mode | stat.S_IXUSR)
    (tool / "files" / "bin" / "wine").write_text("")
    (tool / "version").write_text("1.0\n")
    (tool / "toolmanifest.vdf").write_text(
        '"manifest"\n{\n  "commandline" "/proton run"\n}\n',
    )
    return root


def test_resolve_proton_path_finds_system_wide_tool(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """CachyOS's proton-cachyos lives in the system-wide compat dir."""
    from unifideck.launcher.proton.infrastructure import selector
    from unifideck.utils import vdf_compat

    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # empty user dirs
    root = _system_proton(tmp_path, "proton-cachyos")
    monkeypatch.setattr(vdf_compat, "SYSTEM_COMPAT_DIRS", (str(root),))

    assert selector.resolve_proton_path("proton-cachyos") == (
        root / "proton-cachyos" / "proton"
    )


def test_get_steam_compat_tool_override_reads_config_vdf(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Per-app override comes from the GLOBAL config.vdf, not localconfig.vdf."""
    from unifideck.launcher.proton.infrastructure import selector

    home = tmp_path / "home"
    steam = home / ".local" / "share" / "Steam"
    (steam / "steamapps").mkdir(parents=True)
    (steam / "config").mkdir()
    (steam / "config" / "config.vdf").write_text(_CONFIG_VDF_GLOBAL)
    monkeypatch.setenv("HOME", str(home))

    assert selector.get_steam_compat_tool_override("480") == "GE-Proton8-30"
    assert selector.get_global_default_tool() == "proton-cachyos"


def test_select_proton_version_honors_global_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """With no per-game pick, the launcher falls to Steam's global default.

    This is the tier that makes a distro's system Proton (Proton-CachyOS)
    'just work' instead of silently falling through to GE-latest.
    """
    from unifideck.launcher.proton.infrastructure import selector
    from unifideck.utils import vdf_compat

    home = tmp_path / "home"
    steam = home / ".local" / "share" / "Steam"
    (steam / "steamapps").mkdir(parents=True)
    (steam / "config").mkdir()
    (steam / "config" / "config.vdf").write_text(_CONFIG_VDF_GLOBAL)
    monkeypatch.setenv("HOME", str(home))
    root = _system_proton(tmp_path, "proton-cachyos")
    monkeypatch.setattr(vdf_compat, "SYSTEM_COMPAT_DIRS", (str(root),))

    path, tag = selector.select_proton_version()
    assert tag == "proton-cachyos"
    assert path == root / "proton-cachyos" / "proton"
