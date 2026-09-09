"""Unit tests for latest-GE-Proton install + selector default tier.

Covers the three pieces added for "always run the latest GE-Proton,
fall back to Proton Experimental":

* ``ge_installer`` — latest-tag lookup, broken-extract detection,
  marker cache, and the "already installed → no download" short-circuit.
* ``selector`` — official-tool dir aliasing (so ``proton_experimental``
  resolves to ``Proton - Experimental``) and the new default tier
  (cached latest GE → on-demand download → Experimental → raise).
* ``ProtonService`` — no longer forces a per-store compat tool by
  default, but still honours an explicit ctor override.
"""
from __future__ import annotations

import json
import stat
import urllib.error
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from unifideck.launcher.proton.infrastructure import (
    ge_install_lock,
    ge_installer,
    ge_marker,
    selector,
)
from unifideck.launcher.types.errors import ProtonUnavailableError
from unifideck.utils import vdf_compat


def _make_proton(dir_path: Path, *, executable: bool) -> Path:
    """Create ``<dir_path>/proton`` with/without the +x bit."""
    dir_path.mkdir(parents=True, exist_ok=True)
    proton = dir_path / "proton"
    proton.write_text("#!/bin/sh\n")
    mode = proton.stat().st_mode
    exec_bits = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    proton.chmod(mode | exec_bits if executable else mode & ~exec_bits)
    return proton


# ── ge_installer.is_valid_ge_install ──────────────────────────────

def test_is_valid_ge_install_true_when_executable(tmp_path, monkeypatch):
    root = tmp_path / "compatibilitytools.d"
    proton = _make_proton(root / "GE-Proton10-34", executable=True)
    monkeypatch.setattr(ge_installer, "_SCAN_ROOTS", (str(root),))

    assert ge_installer.is_valid_ge_install("GE-Proton10-34") is True
    assert ge_installer.installed_ge_proton_path("GE-Proton10-34") == proton


def test_is_valid_ge_install_false_when_not_executable(tmp_path, monkeypatch):
    # Mirrors the real broken GE-Proton10-34 on disk (proton is 0644):
    # present but non-executable → must be treated as NOT installed.
    root = tmp_path / "compatibilitytools.d"
    _make_proton(root / "GE-Proton10-34", executable=False)
    monkeypatch.setattr(ge_installer, "_SCAN_ROOTS", (str(root),))

    assert ge_installer.is_valid_ge_install("GE-Proton10-34") is False
    assert ge_installer.installed_ge_proton_path("GE-Proton10-34") is None


def test_is_valid_ge_install_false_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(ge_installer, "_SCAN_ROOTS", (str(tmp_path),))
    assert ge_installer.is_valid_ge_install("GE-Proton99-99") is False


# ── ge_installer.get_latest_ge_tag ────────────────────────────────

def test_get_latest_ge_tag_success():
    payload = json.dumps({"tag_name": "GE-Proton10-34"}).encode()
    with patch("urllib.request.urlopen") as urlopen:
        urlopen.return_value.__enter__.return_value.read.return_value = payload
        assert ge_installer.get_latest_ge_tag() == "GE-Proton10-34"


def test_get_latest_ge_tag_network_failure_returns_none():
    with patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.URLError("offline"),
    ):
        assert ge_installer.get_latest_ge_tag() is None


# ── ge_installer._select_tarball ───────────────────────────────────

def test_select_tarball_ignores_aarch64_and_prefers_exact_tag():
    assets = [
        {"name": "GE-Proton11-1-aarch64.sha512sum", "browser_download_url": "http://example.com/arm.sha"},
        {"name": "GE-Proton11-1-aarch64.tar.gz", "browser_download_url": "http://example.com/arm.tar.gz"},
        {"name": "GE-Proton11-1.sha512sum", "browser_download_url": "http://example.com/x86.sha"},
        {"name": "GE-Proton11-1.tar.gz", "browser_download_url": "http://example.com/x86.tar.gz"},
    ]
    url = ge_installer._select_tarball(assets, "GE-Proton11-1")
    assert url == "http://example.com/x86.tar.gz"


def test_select_tarball_fallback_without_tag():
    assets = [
        {"name": "GE-Proton11-1-aarch64.tar.gz", "browser_download_url": "http://example.com/arm.tar.gz"},
        {"name": "GE-Proton11-1.tar.gz", "browser_download_url": "http://example.com/x86.tar.gz"},
    ]
    url = ge_installer._select_tarball(assets)
    assert url == "http://example.com/x86.tar.gz"


# ── ge_installer marker + ensure_latest_ge ────────────────────────


def test_marker_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(ge_marker, "_MARKER", tmp_path / "latest.json")
    assert ge_marker.read_cached_latest_tag() is None
    ge_marker.write_latest_tag("GE-Proton10-34")
    assert ge_marker.read_cached_latest_tag() == "GE-Proton10-34"


def test_ensure_latest_ge_uses_existing_without_download(tmp_path, monkeypatch):
    monkeypatch.setattr(ge_marker, "_MARKER", tmp_path / "latest.json")
    existing = tmp_path / "GE-Proton10-34" / "proton"
    monkeypatch.setattr(
        ge_installer, "_fetch_latest_release",
        lambda timeout: {"tag_name": "GE-Proton10-34"},
    )
    monkeypatch.setattr(
        ge_installer, "installed_ge_proton_path", lambda tag: existing,
    )
    download = MagicMock()
    monkeypatch.setattr(ge_installer, "_download_and_install", download)

    assert ge_installer.ensure_latest_ge() == (existing, "GE-Proton10-34")
    download.assert_not_called()
    # Marker refreshed so the launcher can skip the network next time.
    assert ge_marker.read_cached_latest_tag() == "GE-Proton10-34"


def test_ensure_latest_ge_offline_returns_none(monkeypatch):
    monkeypatch.setattr(ge_installer, "_fetch_latest_release", lambda timeout: None)
    assert ge_installer.ensure_latest_ge() is None


# ── selector.resolve_proton_path (official-tool aliasing) ─────────

def _point_selector_roots(tmp_path, monkeypatch):
    compat = tmp_path / "compat"
    lib = tmp_path / "common"
    empty = tmp_path / "empty"
    for d in (compat, lib, empty):
        d.mkdir()
    # Compat roots live in vdf_compat now (one owner), so patch them there —
    # selector deliberately keeps no mirror to patch.
    monkeypatch.setattr(vdf_compat, "UNIFIDECK_COMPAT_DIR", str(empty))
    monkeypatch.setattr(vdf_compat, "STEAM_COMPAT_ROOTS", (str(compat),))
    monkeypatch.setattr(vdf_compat, "SYSTEM_COMPAT_DIRS", ())
    monkeypatch.setattr(selector, "STEAM_LIBRARY_ROOTS", [str(lib)])
    # Proton is also searched across every library in libraryfolders.vdf;
    # stub that out so the host's real Steam install can't satisfy a lookup
    # these tests expect to resolve inside tmp_path (or not at all).
    monkeypatch.setattr(selector, "_discovered_library_commons", lambda: [])
    return compat, lib


def test_resolve_proton_path_aliases_experimental(tmp_path, monkeypatch):
    _compat, lib = _point_selector_roots(tmp_path, monkeypatch)
    proton = _make_proton(lib / "Proton - Experimental", executable=True)
    # The tool id is ``proton_experimental`` but the dir is the display
    # name — without the alias map this returned None (the original bug).
    assert selector.resolve_proton_path("proton_experimental") == proton


def test_resolve_proton_path_ge_in_compat_root(tmp_path, monkeypatch):
    compat, _lib = _point_selector_roots(tmp_path, monkeypatch)
    proton = _make_proton(compat / "GE-Proton10-34", executable=True)
    assert selector.resolve_proton_path("GE-Proton10-34") == proton


def test_resolve_proton_path_unknown_returns_none(tmp_path, monkeypatch):
    _point_selector_roots(tmp_path, monkeypatch)
    assert selector.resolve_proton_path("does-not-exist") is None


# ── selector.get_saved_proton_tool (pre-0.7.0 schema compat) ──────

def _write_proton_settings(tmp_path, monkeypatch, games: dict) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / ".local" / "share" / "unifideck" / "proton_settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"games": games}))


def test_get_saved_proton_tool_reads_current_string_schema(tmp_path, monkeypatch):
    _write_proton_settings(tmp_path, monkeypatch, {"gog:1": "GE-Proton10-34"})
    assert selector.get_saved_proton_tool("gog:1") == "GE-Proton10-34"


def test_get_saved_proton_tool_ignores_legacy_dict_schema(tmp_path, monkeypatch):
    """Regression: a pre-0.7.0 dict-shaped entry ({"proton_tool": "<id>"},
    see the retired bash launcher's get_unifideck_proton_tool) must not be
    extracted and honored as the current choice. useLaunchPrep only
    refreshes this file when the game-details page is opened, not on
    every launch — so a legacy entry is stale data from before the
    rewrite, possibly a long-forgotten pin the user can't even see is in
    effect anymore (Steam's own Force-Compat UI reflects whatever was
    last restored there, independent of this file). Real field case: a
    game silently kept launching on a much older GE-Proton than the
    reporter's actual current default, with no visible error. Treat any
    dict shape as "no saved override" so the normal priority chain
    (Steam override -> Unifideck default -> latest GE-Proton) applies.
    """
    _write_proton_settings(
        tmp_path, monkeypatch, {"gog:1": {"proton_tool": "GE-Proton10-34"}},
    )
    assert selector.get_saved_proton_tool("gog:1") is None


def test_get_saved_proton_tool_legacy_dict_missing_key_is_none(tmp_path, monkeypatch):
    _write_proton_settings(tmp_path, monkeypatch, {"gog:1": {}})
    assert selector.get_saved_proton_tool("gog:1") is None


def test_select_proton_version_falls_through_legacy_dict_entry(
    tmp_path, monkeypatch,
):
    """Regression: a pre-0.7.0 dict-shaped entry must not raise TypeError
    (the original field crash: ``resolve_proton_path`` received a dict
    instead of a str tool id and blew up on ``Path(...) / tool_id``) AND
    must not resurrect the stale pinned version — it should fall through
    to the next tier exactly as if no saved override existed.
    """
    _write_proton_settings(
        tmp_path, monkeypatch, {"gog:1": {"proton_tool": "GE-Proton10-34"}},
    )
    monkeypatch.setattr(selector, "get_steam_compat_tool_override", lambda aid: None)
    monkeypatch.setattr(selector, "get_unifideck_proton_tool", lambda: None)
    proton = tmp_path / "GE-Proton11-1" / "proton"
    monkeypatch.setattr(
        selector.ge_marker, "read_cached_latest_tag", lambda: "GE-Proton11-1",
    )
    monkeypatch.setattr(
        selector.ge_installer, "installed_ge_proton_path",
        lambda tag: proton if tag == "GE-Proton11-1" else None,
    )

    assert selector.select_proton_version(store_game_id="gog:1") == (
        proton, "GE-Proton11-1",
    )


# ── selector.select_proton_version (default tier) ─────────────────

def _silence_higher_tiers(monkeypatch):
    monkeypatch.setattr(selector, "get_saved_proton_tool", lambda gid: None)
    monkeypatch.setattr(selector, "get_steam_compat_tool_override", lambda aid: None)
    monkeypatch.setattr(selector, "get_unifideck_proton_tool", lambda: None)
    monkeypatch.setattr(selector, "get_global_default_tool", lambda: None)


def test_select_prefers_cached_latest_ge(tmp_path, monkeypatch):
    _silence_higher_tiers(monkeypatch)
    proton = tmp_path / "GE-Proton10-34" / "proton"
    monkeypatch.setattr(
        selector.ge_marker, "read_cached_latest_tag", lambda: "GE-Proton10-34",
    )
    monkeypatch.setattr(
        selector.ge_installer, "installed_ge_proton_path",
        lambda tag: proton if tag == "GE-Proton10-34" else None,
    )
    ensure = MagicMock()
    monkeypatch.setattr(selector.ge_installer, "ensure_latest_ge", ensure)

    assert selector.select_proton_version() == (proton, "GE-Proton10-34")
    ensure.assert_not_called()  # cached → no network/download


def test_select_downloads_latest_when_not_cached(tmp_path, monkeypatch):
    _silence_higher_tiers(monkeypatch)
    proton = tmp_path / "GE-Proton10-34" / "proton"
    monkeypatch.setattr(selector.ge_marker, "read_cached_latest_tag", lambda: None)
    monkeypatch.setattr(
        selector.ge_installer, "ensure_latest_ge",
        lambda progress_cb=None: (proton, "GE-Proton10-34"),
    )
    assert selector.select_proton_version() == (proton, "GE-Proton10-34")


def test_select_falls_back_to_experimental_when_offline(tmp_path, monkeypatch):
    _silence_higher_tiers(monkeypatch)
    _compat, lib = _point_selector_roots(tmp_path, monkeypatch)
    exp = _make_proton(lib / "Proton - Experimental", executable=True)
    monkeypatch.setattr(selector.ge_marker, "read_cached_latest_tag", lambda: None)
    monkeypatch.setattr(
        selector.ge_installer, "ensure_latest_ge", lambda progress_cb=None: None,
    )

    assert selector.select_proton_version() == (exp, "proton_experimental")


def test_select_raises_when_nothing_available(tmp_path, monkeypatch):
    _silence_higher_tiers(monkeypatch)
    _point_selector_roots(tmp_path, monkeypatch)  # no Experimental on disk
    monkeypatch.setattr(selector.ge_marker, "read_cached_latest_tag", lambda: None)
    monkeypatch.setattr(
        selector.ge_installer, "ensure_latest_ge", lambda progress_cb=None: None,
    )

    with pytest.raises(ProtonUnavailableError):
        selector.select_proton_version()


# ── launch-time GE-download toasts ────────────────────────────────

def test_download_announcer_toasts_once(monkeypatch):
    """The progress callback toasts on the first chunk, then stays quiet."""
    import unifideck.launcher.frontend_bridge as fb
    spy = MagicMock()
    monkeypatch.setattr(fb, "launcher_toast", spy)

    cb = selector._GeDownloadAnnouncer()
    cb(1024, 9999)
    cb(2048, 9999)
    cb(4096, 9999)

    assert cb.fired is True
    spy.assert_called_once()
    assert spy.call_args.args[0] == "toasts.launcher.downloadingProton"


def test_select_toasts_when_download_happens(tmp_path, monkeypatch):
    """A real launch-time download fires download + ready toasts."""
    _silence_higher_tiers(monkeypatch)
    import unifideck.launcher.frontend_bridge as fb
    spy = MagicMock()
    monkeypatch.setattr(fb, "launcher_toast", spy)
    proton = tmp_path / "GE-Proton10-34" / "proton"
    monkeypatch.setattr(selector.ge_marker, "read_cached_latest_tag", lambda: None)

    def _ensure(progress_cb=None):
        # Simulate streaming bytes so the announcer fires.
        if progress_cb:
            progress_cb(1024, 2048)
        return proton, "GE-Proton10-34"

    monkeypatch.setattr(selector.ge_installer, "ensure_latest_ge", _ensure)

    assert selector.select_proton_version() == (proton, "GE-Proton10-34")
    keys = [c.args[0] for c in spy.call_args_list]
    assert "toasts.launcher.downloadingProton" in keys
    assert "toasts.launcher.protonReadyBody" in keys


def test_select_silent_when_no_download(tmp_path, monkeypatch):
    """No download (cb never fires) → no GE toasts."""
    _silence_higher_tiers(monkeypatch)
    import unifideck.launcher.frontend_bridge as fb
    spy = MagicMock()
    monkeypatch.setattr(fb, "launcher_toast", spy)
    proton = tmp_path / "GE-Proton10-34" / "proton"
    monkeypatch.setattr(selector.ge_marker, "read_cached_latest_tag", lambda: None)
    # Already-installed path: ensure_latest_ge returns without streaming.
    monkeypatch.setattr(
        selector.ge_installer, "ensure_latest_ge",
        lambda progress_cb=None: (proton, "GE-Proton10-34"),
    )

    selector.select_proton_version()
    keys = [c.args[0] for c in spy.call_args_list]
    assert "toasts.launcher.downloadingProton" not in keys
    assert "toasts.launcher.protonReadyBody" not in keys


# ── umu runtime first-setup toast ─────────────────────────────────

def test_umu_runtime_toasts_when_steamrt3_missing(tmp_path, monkeypatch):
    from unifideck.launcher.proton.infrastructure import umu_runtime
    spy = MagicMock()
    monkeypatch.setattr(umu_runtime, "launcher_toast", spy)
    monkeypatch.setattr(umu_runtime, "UMU_CACHE_DIR", tmp_path / "umu")
    monkeypatch.setenv("HOME", str(tmp_path))  # contain ~/.config/umu

    umu_runtime.ensure_umu_runtime_ready()

    spy.assert_called_once()
    assert spy.call_args.args[0] == "toasts.launcher.downloadingRuntime"


def test_umu_runtime_silent_when_steamrt3_present(tmp_path, monkeypatch):
    from unifideck.launcher.proton.infrastructure import umu_runtime
    spy = MagicMock()
    monkeypatch.setattr(umu_runtime, "launcher_toast", spy)
    cache = tmp_path / "umu"
    (cache / "steamrt3").mkdir(parents=True)
    monkeypatch.setattr(umu_runtime, "UMU_CACHE_DIR", cache)
    monkeypatch.setenv("HOME", str(tmp_path))

    umu_runtime.ensure_umu_runtime_ready()

    spy.assert_not_called()


def test_umu_runtime_silent_when_steamrt4_present(tmp_path, monkeypatch):
    """Regression: a newer Proton build can resolve umu to steamrt4, not
    the default steamrt3 — the readiness check must recognize either."""
    from unifideck.launcher.proton.infrastructure import umu_runtime
    spy = MagicMock()
    monkeypatch.setattr(umu_runtime, "launcher_toast", spy)
    cache = tmp_path / "umu"
    (cache / "steamrt4").mkdir(parents=True)
    monkeypatch.setattr(umu_runtime, "UMU_CACHE_DIR", cache)
    monkeypatch.setenv("HOME", str(tmp_path))

    umu_runtime.ensure_umu_runtime_ready()

    spy.assert_not_called()


async def test_run_umu_with_retry_recovers_from_exit_127(tmp_path, monkeypatch):
    """Regression (refined by UD-022): rc=127 is still *recoverable* — the
    retry loop tries a second time rather than giving up on attempt 1 — but
    it must NOT wipe the shared steamrt runtime cache. 127 is exec/command-
    not-found, not a runtime-corruption signal; only 2/74 justify the
    multi-hundred-MB shared-cache nuke. See ``_RUNTIME_CORRUPTION_CODES``.
    """
    from unifideck.launcher.proton.infrastructure import umu_runtime

    counter = tmp_path / "attempts"
    script = tmp_path / "flaky.sh"
    script.write_text(
        "#!/bin/sh\n"
        f'n=$(cat "{counter}" 2>/dev/null || echo 0)\n'
        f'echo $((n + 1)) > "{counter}"\n'
        'if [ "$n" -eq 0 ]; then exit 127; fi\n'
        "exit 0\n",
    )
    script.chmod(0o755)
    cleanup = MagicMock()
    monkeypatch.setattr(umu_runtime, "cleanup_umu_runtime_cache", cleanup)

    rc = await umu_runtime.run_umu_with_retry([str(script)], max_attempts=2)

    assert rc == 0
    assert counter.read_text().strip() == "2"  # retried once → recovered
    cleanup.assert_not_called()  # 127 must NOT nuke the shared runtime cache


def test_cleanup_umu_runtime_cache_wipes_steamrt4(tmp_path, monkeypatch):
    """Regression: cache cleanup used to only ever target "steamrt3",
    silently no-op'ing for anyone whose Proton build resolved umu to
    steamrt4 — the retry-and-wipe self-heal never actually fixed anything
    for them. Must wipe whichever runtime variant(s) are actually present.
    """
    from unifideck.launcher.proton.infrastructure import umu_runtime
    cache = tmp_path / "umu"
    (cache / "steamrt4" / "pressure-vessel").mkdir(parents=True)
    monkeypatch.setattr(umu_runtime, "UMU_CACHE_DIR", cache)

    umu_runtime.cleanup_umu_runtime_cache()

    assert not (cache / "steamrt4").exists()


# ── ProtonService writes no compat-tool entries ───────────────────

async def test_proton_service_holds_no_compat_tool_surface():
    """ProtonService only keeps GE-Proton installed — it writes no config.vdf.

    The old ``GAME_INSTALLED`` → ``set_compat_tool`` path was removed: the
    event had no live emitter, its payload key never matched what the handler
    read, the per-store tool table was empty for every store, and in the
    plugin it was pointed at ``localconfig.vdf`` while ``CompatToolMapping``
    lives in ``config/config.vdf``. ``ProtonToolsManager`` in
    ``compatibility/proton_helpers.py`` is the one live writer. Asserting the
    surface is gone keeps it from being reintroduced by copy-paste.
    """
    from unifideck.services.proton_service import ProtonService

    svc = ProtonService(MagicMock())

    for gone in (
        "set_compat_tool",
        "_inject_compat_tool",
        "_on_game_installed",
        "set_config_vdf_path",
    ):
        assert not hasattr(svc, gone), f"{gone} should have been removed"


# ── emit_stage payload forwarding ─────────────────────────────────

async def test_emit_stage_forwards_optional_fields():
    from unifideck.core.types import Events
    from unifideck.launcher.rpc import emit_stage

    bus = MagicMock()
    bus.emit = AsyncMock()
    await emit_stage(
        bus,
        i18n_key="toasts.launcher.protonSwitchedTo",
        i18n_title_key="toasts.launcher.protonUpgrade",
        game_title="epic:1",
        i18n_params={"version": "GE-Proton10-34"},
        severity="info",
        priority="normal",
    )
    bus.emit.assert_awaited_once()
    args, kwargs = bus.emit.call_args
    assert args[0] == Events.LAUNCHER_STAGE
    assert kwargs["i18n_title_key"] == "toasts.launcher.protonUpgrade"
    assert kwargs["i18n_key"] == "toasts.launcher.protonSwitchedTo"
    assert kwargs["i18n_params"] == {"version": "GE-Proton10-34"}
    assert kwargs["severity"] == "info"


async def test_emit_stage_omits_unset_optionals():
    from unifideck.launcher.rpc import emit_stage

    bus = MagicMock()
    bus.emit = AsyncMock()
    await emit_stage(bus, i18n_key="toasts.launcher.launchingGame", game_title="g")
    _args, kwargs = bus.emit.call_args
    assert "i18n_title_key" not in kwargs
    assert "i18n_params" not in kwargs
    assert "severity" not in kwargs


# ── ge_installer: install serialisation + atomic publish ──────────
#
# Regression guard for the shared-Proton corruption report (Legion Go S,
# 0.7.4). The old publish did ``rmtree(dest)`` then ``move(...)`` with no
# cross-process lock, and the gate deciding whether to reach it checked only
# that ``<tag>/proton`` existed and was executable — a condition that is false
# during the very window the delete creates.


def _complete_tree(root: Path, tag: str) -> Path:
    """A tag directory that passes ``is_proton_install_complete``."""
    tool = root / tag
    proton = _make_proton(tool, executable=True)
    (tool / "files" / "bin").mkdir(parents=True, exist_ok=True)
    (tool / "files" / "bin" / "wine").write_text("#!/bin/sh\n")
    (tool / "version").write_text(f"{tag}\n")
    (tool / "toolmanifest.vdf").write_text('"manifest" { "commandline" "" }\n')
    return proton


def test_install_lock_serialises_across_holders(tmp_path, monkeypatch):
    """A second holder waits: the lock is exclusive, not advisory-per-process."""
    monkeypatch.setattr(ge_install_lock, "INSTALL_LOCK", tmp_path / "ge.lock")
    with ge_install_lock.install_lock() as held:
        assert held is True
        # flock is per-open-file-description, so a fresh fd genuinely blocks.
        import fcntl
        import os as _os
        fd = _os.open(tmp_path / "ge.lock", _os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            _os.close(fd)


def test_ensure_latest_ge_rechecks_under_lock_and_skips_download(
    tmp_path, monkeypatch,
):
    """The loser of a race finds a COMPLETE install and does not republish."""
    monkeypatch.setattr(ge_marker, "_MARKER", tmp_path / "latest.json")
    monkeypatch.setattr(ge_install_lock, "INSTALL_LOCK", tmp_path / "ge.lock")
    tag = "GE-Proton11-6"
    settled = _complete_tree(tmp_path, tag)
    monkeypatch.setattr(
        ge_installer, "_fetch_latest_release", lambda timeout: {
            "tag_name": tag,
            "assets": [{"name": f"{tag}.tar.gz", "browser_download_url": "u"}],
        },
    )
    # First (pre-lock) probe says "absent"; the probe under the lock finds the
    # tree the winner just published.
    calls = iter([None, settled, settled])
    monkeypatch.setattr(
        ge_installer, "installed_ge_proton_path", lambda tag: next(calls),
    )
    download = MagicMock()
    monkeypatch.setattr(ge_installer, "_download_and_install", download)

    assert ge_installer.ensure_latest_ge() == (settled, tag)
    download.assert_not_called()


def test_ensure_latest_ge_downloads_when_recheck_finds_incomplete(
    tmp_path, monkeypatch,
):
    """A half-published tree does NOT satisfy the re-check."""
    monkeypatch.setattr(ge_marker, "_MARKER", tmp_path / "latest.json")
    monkeypatch.setattr(ge_install_lock, "INSTALL_LOCK", tmp_path / "ge.lock")
    tag = "GE-Proton11-6"
    # +x proton but no files/, version or toolmanifest — passes the weak check
    # and fails the strong one.
    half = _make_proton(tmp_path / tag, executable=True)
    monkeypatch.setattr(
        ge_installer, "_fetch_latest_release", lambda timeout: {
            "tag_name": tag,
            "assets": [{"name": f"{tag}.tar.gz", "browser_download_url": "u"}],
        },
    )
    calls = iter([None, half])
    monkeypatch.setattr(
        ge_installer, "installed_ge_proton_path", lambda tag: next(calls),
    )
    installed = tmp_path / "fresh" / "proton"
    monkeypatch.setattr(
        ge_installer, "_download_and_install",
        lambda tag, url, cb: installed,
    )
    assert ge_installer.ensure_latest_ge() == (installed, tag)


def test_promote_never_leaves_the_tag_dir_missing(tmp_path, monkeypatch):
    """Publishing renames aside instead of deleting the live tree."""
    monkeypatch.setattr(ge_installer, "COMPAT_TOOLS_DIR", tmp_path)
    tag = "GE-Proton11-6"
    live = _complete_tree(tmp_path, tag).parent
    # A file another process is executing out of, by inode.
    in_use = live / "files" / "bin" / "wine"
    held = in_use.open("rb")
    staging = tmp_path / f".{tag}.dl-x"
    _complete_tree(staging, tag)
    (staging / tag / "version").write_text("new\n")

    try:
        final = ge_installer._promote_extracted(staging, tag)
        assert final == tmp_path / tag / "proton"
        assert (tmp_path / tag / "version").read_text() == "new\n"
        # The old inode survived the swap — a live umu-run keeps reading it.
        assert held.read() == b"#!/bin/sh\n"
        # No aside copies left behind.
        assert not list(tmp_path.glob(f".{tag}.old-*"))
    finally:
        held.close()


def test_promote_rolls_back_when_the_swap_fails(tmp_path, monkeypatch):
    """A failed publish restores the live tree rather than leaving nothing."""
    monkeypatch.setattr(ge_installer, "COMPAT_TOOLS_DIR", tmp_path)
    tag = "GE-Proton11-6"
    _complete_tree(tmp_path, tag)
    staging = tmp_path / f".{tag}.dl-x"
    _complete_tree(staging, tag)

    real_rename = Path.rename
    state = {"n": 0}

    def flaky(self, target):
        state["n"] += 1
        if state["n"] == 2:  # the staged -> dest half
            raise OSError("EXDEV")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", flaky)
    assert ge_installer._promote_extracted(staging, tag) is None
    monkeypatch.undo()
    assert (tmp_path / tag / "proton").is_file()


# ── external GE: one decision rule, shared by selector and service ──

def test_external_at_least_as_new_requires_proof():
    """An unknown version never beats a build we installed ourselves.

    The regression guard for register 54. ``is_ge_outdated`` returns False on
    a parse failure, so the previous guard read "not provably older" as
    "adopt it". Measured on device: a ``version`` of
    ``1724000000 CachyOS-11.0-100`` and a bare ``1724000000`` both beat a
    newer cached GE-Proton11-6.
    """
    from unifideck.launcher.proton.infrastructure import external_ge as ext

    assert ext.external_at_least_as_new("GE-Proton11-6", "GE-Proton11-6") is True
    assert ext.external_at_least_as_new("GE-Proton11-7", "GE-Proton11-6") is True
    assert ext.external_at_least_as_new("GE-Proton11-3", "GE-Proton11-6") is False
    for unknown in ("", "CachyOS-11.0-100", "1724000000", "garbage"):
        assert ext.external_at_least_as_new(unknown, "GE-Proton11-6") is False, unknown
    assert ext.external_at_least_as_new("GE-Proton11-6", "garbage") is False


@pytest.mark.parametrize(
    ("ext_version", "expect_external"),
    [
        ("GE-Proton11-6", True),    # equal -> external
        ("GE-Proton11-7", True),    # newer -> external
        ("GE-Proton11-3", False),   # older -> ours
        ("", False),                # unknown -> ours
        ("1724000000", False),      # timestamp-only version file -> ours
    ],
)
def test_choose_ge_prefers_the_provably_newer(tmp_path, ext_version, expect_external):
    from unifideck.launcher.proton.infrastructure import external_ge as ext

    ext_path, cached_path = tmp_path / "ext" / "proton", tmp_path / "ours" / "proton"
    choice = ext.choose_ge(
        (ext_path, "Proton-GE Latest", ext_version), "GE-Proton11-6", cached_path,
    )
    assert choice is not None
    assert choice.is_external is expect_external
    assert choice.path == (ext_path if expect_external else cached_path)


def test_choose_ge_edge_cases(tmp_path):
    from unifideck.launcher.proton.infrastructure import external_ge as ext

    ext_path = tmp_path / "ext" / "proton"
    # No cached build of our own: an unknown-version external still wins,
    # because the alternative is no GE at all.
    choice = ext.choose_ge((ext_path, "Proton-GE Latest", ""), None, None)
    assert choice is not None and choice.is_external
    # Nothing anywhere -> caller falls through to its download ladder.
    assert ext.choose_ge(None, None, None) is None
    # No external -> ours.
    ours = tmp_path / "ours" / "proton"
    choice = ext.choose_ge(None, "GE-Proton11-6", ours)
    assert choice is not None and not choice.is_external


def test_selector_and_service_agree_on_the_same_inputs(tmp_path, monkeypatch):
    """One rule, not one per call site (register 56).

    The selector used strict "prefer newer" while the service used a
    5-minor tolerance, and they disagreed on the unknown-version case.
    """
    from unifideck.launcher.proton.infrastructure import external_ge as ext

    cached_path = tmp_path / "ours" / "proton"
    ext_path = tmp_path / "ext" / "proton"
    for ext_version in ("GE-Proton11-7", "GE-Proton11-3", "", "1724000000"):
        external = (ext_path, "Proton-GE Latest", ext_version)
        monkeypatch.setattr(ext, "find_external_ge_proton", lambda roots=None, e=external: e)
        monkeypatch.setattr(ge_marker, "read_cached_latest_tag", lambda: "GE-Proton11-6")
        monkeypatch.setattr(ge_installer, "installed_ge_proton_path", lambda t: cached_path)
        _path, tool = selector._default_latest_ge([])
        choice = ext.choose_ge(external, "GE-Proton11-6", cached_path)
        assert choice is not None
        assert tool == choice.tool_id, f"disagreement for {ext_version!r}"


def test_external_ge_opt_out(tmp_path, monkeypatch):
    """``compat.external_ge: off`` restores pre-0.7.5 behaviour (register 59)."""
    from unifideck.launcher.proton.infrastructure import external_ge as ext

    root = tmp_path / "compatibilitytools.d"
    alias = root / "Proton-GE Latest"
    alias.mkdir(parents=True)
    _make_proton(alias, executable=True)
    (alias / "files" / "bin").mkdir(parents=True)
    (alias / "files" / "bin" / "wine").write_text("")
    (alias / "version").write_text("1724000000 GE-Proton11-6\n")
    (alias / "toolmanifest.vdf").write_text('"manifest" { "commandline" "/proton" }')

    from unifideck.launcher.proton.infrastructure import proton_config

    cfg = tmp_path / "config.json"
    monkeypatch.setattr(proton_config, "CONFIG_PATH", cfg)

    cfg.write_text(json.dumps({"compat": {"external_ge": "auto"}}))
    assert ext.find_external_ge_proton(roots=[root]) is not None
    cfg.write_text(json.dumps({"compat": {"external_ge": "off"}}))
    assert ext.find_external_ge_proton(roots=[root]) is None
    # Absent config / absent key both default to auto.
    cfg.write_text(json.dumps({}))
    assert ext.find_external_ge_proton(roots=[root]) is not None
    cfg.unlink()
    assert ext.find_external_ge_proton(roots=[root]) is not None


def test_external_roots_exclude_our_own_and_dedupe_by_realpath(tmp_path, monkeypatch):
    """Register 57: one owner, and symlinked roots enumerated once."""
    from unifideck.launcher.proton.infrastructure import external_ge as ext

    real = tmp_path / "real"
    real.mkdir()
    link_a, link_b = tmp_path / "a", tmp_path / "b"
    link_a.symlink_to(real)
    link_b.symlink_to(real)
    monkeypatch.setattr(vdf_compat, "STEAM_COMPAT_ROOTS", (str(link_a), str(link_b), str(real)))
    monkeypatch.setattr(vdf_compat, "SYSTEM_COMPAT_DIRS", ())
    monkeypatch.setattr(vdf_compat, "UNIFIDECK_COMPAT_DIR", str(tmp_path / "ours"))

    assert len(vdf_compat.compat_tool_roots(include_unifideck=False)) == 1
    # Name resolution includes our own dir; external discovery must not.
    assert len(vdf_compat.compat_tool_roots(include_unifideck=True)) == 2
    assert Path(tmp_path / "ours") not in ext.get_external_compat_roots()


def test_marker_update_preserves_other_keys(tmp_path, monkeypatch):
    """``_write_marker`` used to clobber the warn state on every install."""
    marker = tmp_path / "proton_ge_latest.json"
    monkeypatch.setattr(ge_marker, "_MARKER", marker)

    ge_marker.update_marker(external_warned_tag="GE-Proton11-7")
    ge_marker.write_latest_tag("GE-Proton11-6")
    data = ge_marker.read_marker()
    assert data["tag"] == "GE-Proton11-6"
    assert data["external_warned_tag"] == "GE-Proton11-7"
    assert ge_marker.read_cached_latest_tag() == "GE-Proton11-6"


def test_read_marker_survives_corruption(tmp_path, monkeypatch):
    marker = tmp_path / "proton_ge_latest.json"
    monkeypatch.setattr(ge_marker, "_MARKER", marker)
    assert ge_marker.read_marker() == {}
    marker.write_text("{not json")
    assert ge_marker.read_marker() == {}
    marker.write_text('["a list"]')
    assert ge_marker.read_marker() == {}


# ── external GE: coverage restored after the #448 merge dropped it ──
# The merge into 0.7.5 (7a66940) took "ours" for this file and silently
# discarded all 12 tests the PR shipped, so the feature landed with zero
# coverage and the suite stayed green because nothing was asserting.
# Restored here, adapted to the fixed behaviour. Register 62.

def _make_external_tool(root, dirname, version_line, *, display_name=None):
    """Build a tool dir that satisfies is_proton_install_complete."""
    d = root / dirname
    d.mkdir(parents=True, exist_ok=True)
    _make_proton(d, executable=True)
    (d / "files" / "bin").mkdir(parents=True, exist_ok=True)
    (d / "files" / "bin" / "wine").write_text("")
    (d / "version").write_text(version_line)
    (d / "toolmanifest.vdf").write_text('"manifest" { "commandline" "/proton" }')
    if display_name:
        (d / "compatibilitytool.vdf").write_text(
            '"compatibilitytools" {\n  "compat_tools" {\n'
            f'    "custom_ge" {{\n      "display_name" "{display_name}"\n'
            '      "install_path" "."\n    }\n  }\n}',
        )
    return d


def test_parse_ge_version():
    from unifideck.launcher.proton.infrastructure import external_ge as ext

    assert ext.parse_ge_version("GE-Proton11-5") == (11, 5)
    assert ext.parse_ge_version("GE-Proton10-34") == (10, 34)
    assert ext.parse_ge_version("GE-Proton8_25") == (8, 25)
    assert ext.parse_ge_version("Proton-10") is None
    assert ext.parse_ge_version("") is None


def test_is_ge_outdated():
    from unifideck.launcher.proton.infrastructure import external_ge as ext

    assert ext.is_ge_outdated("GE-Proton10-34", "GE-Proton11-5") is True
    assert ext.is_ge_outdated("GE-Proton11-4", "GE-Proton11-5") is True
    assert ext.is_ge_outdated("GE-Proton11-5", "GE-Proton11-5") is False
    assert ext.is_ge_outdated("GE-Proton11-6", "GE-Proton11-5") is False
    assert ext.is_ge_outdated("unknown", "GE-Proton11-5") is False


def test_is_ge_sufficiently_fresh():
    """The recovery-floor tolerance — NOT a selection rule."""
    from unifideck.launcher.proton.infrastructure import external_ge as ext

    assert ext.is_ge_sufficiently_fresh("GE-Proton11-5", "GE-Proton11-5") is True
    assert ext.is_ge_sufficiently_fresh("GE-Proton11-1", "GE-Proton11-5") is True
    assert ext.is_ge_sufficiently_fresh("GE-Proton11-1", "GE-Proton11-7") is False
    assert ext.is_ge_sufficiently_fresh("GE-Proton10-34", "GE-Proton11-1") is False
    assert ext.is_ge_sufficiently_fresh("GE-Proton11-6", "GE-Proton11-5") is True
    assert ext.is_ge_sufficiently_fresh("unknown", "GE-Proton11-5") is False
    assert ext.is_ge_sufficiently_fresh("GE-Proton11-5", "invalid") is False


def test_find_external_ge_proton_detects_via_manifest_display_name(tmp_path):
    """Manifest declares the alias while the directory is named otherwise.

    The case the detection rewrite exists for: ProtonPlus modifies
    compatibilitytool.vdf, and in the equivalent shipped case
    (Proton-CachyOS Latest) "Latest" is a display name, not a directory.
    """
    from unifideck.launcher.proton.infrastructure import external_ge as ext

    root = tmp_path / "compatibilitytools.d"
    tool_dir = _make_external_tool(
        root, "GE-Proton-custom", "1724000000 GE-Proton11-3\n",
        display_name="Proton-GE Latest",
    )
    result = ext.find_external_ge_proton(roots=[root])
    assert result is not None
    proton_script, alias_name, real_ver = result
    assert proton_script == tool_dir / "proton"
    assert alias_name == "Proton-GE Latest"
    assert real_ver == "GE-Proton11-3"


def test_find_external_ge_proton_detects_alias_dir(tmp_path):
    from unifideck.launcher.proton.infrastructure import external_ge as ext

    root = tmp_path / "compatibilitytools.d"
    alias_dir = _make_external_tool(root, "Proton-GE Latest", "GE-Proton11-4\n")
    result = ext.find_external_ge_proton(roots=[root])
    assert result is not None
    proton_script, alias_name, real_ver = result
    assert proton_script == alias_dir / "proton"
    assert alias_name == "Proton-GE Latest"
    assert real_ver == "GE-Proton11-4"


def test_find_external_ge_proton_rejects_incomplete_tool(tmp_path):
    """A half-installed external tool must not become the default."""
    from unifideck.launcher.proton.infrastructure import external_ge as ext

    root = tmp_path / "compatibilitytools.d"
    alias = _make_external_tool(root, "Proton-GE Latest", "GE-Proton11-6\n")
    (alias / "proton").chmod(0o644)
    assert ext.find_external_ge_proton(roots=[root]) is None


def test_selector_prefers_external_ge_when_up_to_date(tmp_path, monkeypatch):
    from unifideck.launcher.proton.infrastructure import external_ge as ext

    fake_ext = tmp_path / "Proton-GE Latest" / "proton"
    monkeypatch.setattr(
        ext, "find_external_ge_proton",
        lambda roots=None: (fake_ext, "Proton-GE Latest", "GE-Proton11-5"),
    )
    monkeypatch.setattr(ge_marker, "read_cached_latest_tag", lambda: "GE-Proton11-3")
    monkeypatch.setattr(
        ge_installer, "installed_ge_proton_path",
        lambda tag: tmp_path / "unifideck-ge" / "proton",
    )
    tried = []
    path, tool = selector._default_latest_ge(tried)
    assert path == fake_ext
    assert tool == "Proton-GE Latest"
    assert "external-ge:Proton-GE Latest" in tried


def test_selector_prefers_cached_unifideck_ge_when_external_is_older(tmp_path, monkeypatch):
    from unifideck.launcher.proton.infrastructure import external_ge as ext

    fake_cached = tmp_path / "unifideck-ge" / "proton"
    monkeypatch.setattr(
        ext, "find_external_ge_proton",
        lambda roots=None: (tmp_path / "ext" / "proton", "Proton-GE Latest", "GE-Proton10-34"),
    )
    monkeypatch.setattr(ge_marker, "read_cached_latest_tag", lambda: "GE-Proton11-5")
    monkeypatch.setattr(ge_installer, "installed_ge_proton_path", lambda tag: fake_cached)
    tried = []
    path, tool = selector._default_latest_ge(tried)
    assert path == fake_cached
    assert tool == "GE-Proton11-5"
    assert "latest-ge-cached:GE-Proton11-5" in tried


# ── ProtonService: the outdated toast is gated on reality and a marker ──

def _svc_with_external(monkeypatch, tmp_path, *, ext_ver, latest, cached=None):
    """Wire ProtonService against a fake external tool. Returns (svc, mocks)."""
    from unifideck.launcher.proton.infrastructure import external_ge as ext
    from unifideck.services.proton_service import ProtonService

    monkeypatch.setattr(ge_marker, "_MARKER", tmp_path / "marker.json")
    monkeypatch.setattr(
        ext, "find_external_ge_proton",
        lambda roots=None: (Path("/fake/Proton-GE Latest/proton"), "Proton-GE Latest", ext_ver),
    )
    monkeypatch.setattr(ge_installer, "get_latest_ge_tag", lambda timeout=8.0: latest)
    monkeypatch.setattr(ge_marker, "read_cached_latest_tag", lambda: cached)
    monkeypatch.setattr(
        ge_installer, "installed_ge_proton_path",
        lambda tag: (tmp_path / "ours" / "proton") if cached else None,
    )
    monkeypatch.setattr(ge_installer, "is_valid_ge_install", lambda tag: False)
    ensure = MagicMock(return_value=(tmp_path / "ours" / "proton", latest))
    monkeypatch.setattr(ge_installer, "ensure_latest_ge", ensure)

    svc = ProtonService(MagicMock())
    svc._emit_proton_toast = AsyncMock()
    return svc, ensure


def _toast_keys(svc):
    return [c.args[0] for c in svc._emit_proton_toast.await_args_list]


async def test_external_outdated_toast_fires_once_per_release(tmp_path, monkeypatch):
    """Register 55: no cooldown meant a nag on every single boot."""
    svc, _ = _svc_with_external(
        monkeypatch, tmp_path, ext_ver="GE-Proton11-3", latest="GE-Proton11-5",
    )
    await svc._ensure_latest_ge()
    await svc._ensure_latest_ge()
    await svc._ensure_latest_ge()
    assert _toast_keys(svc).count("toasts.launcher.externalProtonOutdatedTitle") == 1


async def test_external_outdated_toast_fires_again_on_a_new_release(tmp_path, monkeypatch):
    svc, _ = _svc_with_external(
        monkeypatch, tmp_path, ext_ver="GE-Proton11-3", latest="GE-Proton11-5",
    )
    await svc._ensure_latest_ge()
    monkeypatch.setattr(ge_installer, "get_latest_ge_tag", lambda timeout=8.0: "GE-Proton11-6")
    await svc._ensure_latest_ge()
    assert _toast_keys(svc).count("toasts.launcher.externalProtonOutdatedTitle") == 2


async def test_no_outdated_toast_when_we_are_not_using_the_external_tool(tmp_path, monkeypatch):
    """Register 55, the other half.

    On a major lag we download our own GE and the selector prefers it, so
    telling the user to update their manager points at a build we have
    already routed around.
    """
    svc, ensure = _svc_with_external(
        monkeypatch, tmp_path, ext_ver="GE-Proton10-34", latest="GE-Proton11-5",
        cached="GE-Proton11-5",
    )
    await svc._ensure_latest_ge()
    keys = _toast_keys(svc)
    assert "toasts.launcher.externalProtonOutdatedTitle" not in keys
    assert "toasts.launcher.installingProton" in keys
    assert "toasts.launcher.protonReadyTitle" in keys
    ensure.assert_called_once()


async def test_external_up_to_date_is_silent_and_downloads_nothing(tmp_path, monkeypatch):
    svc, ensure = _svc_with_external(
        monkeypatch, tmp_path, ext_ver="GE-Proton11-5", latest="GE-Proton11-5",
    )
    await svc._ensure_latest_ge()
    svc._emit_proton_toast.assert_not_awaited()
    ensure.assert_not_called()


async def test_severely_outdated_external_still_gets_us_a_recovery_floor(tmp_path, monkeypatch):
    """A major lag must still leave a Unifideck-managed GE on disk."""
    svc, ensure = _svc_with_external(
        monkeypatch, tmp_path, ext_ver="GE-Proton10-34", latest="GE-Proton11-5",
    )
    await svc._ensure_latest_ge()
    ensure.assert_called_once()


# ── ge_fallback: identity guard resolves symlinks (register 61) ──

def test_same_proton_resolves_alias_symlinks(tmp_path):
    from unifideck.launcher.proton.compat import ge_fallback

    real = tmp_path / "GE-Proton11-6"
    real.mkdir()
    (real / "proton").write_text("#!/bin/sh\n")
    alias = tmp_path / "Proton-GE Latest"
    alias.symlink_to(real)

    assert ge_fallback._same_proton(alias / "proton", real / "proton") is True
    assert ge_fallback._same_proton(real / "proton", real / "proton") is True
    assert ge_fallback._same_proton(tmp_path / "other" / "proton", real / "proton") is False
    assert ge_fallback._same_proton(None, real / "proton") is False


# ── umu spawn revalidates Proton (register 60) ──

async def test_run_umu_once_rejects_a_proton_broken_after_selection(tmp_path):
    """An external manager can rewrite its tool between selection and spawn."""
    from unifideck.launcher.proton.infrastructure import umu_runtime

    tool = _make_external_tool(tmp_path, "GE-Proton11-6", "GE-Proton11-6\n")
    env = {"PROTONPATH": str(tool)}
    umu_runtime.assert_proton_still_complete(env)  # complete: no raise

    (tool / "proton").chmod(0o644)  # torn mid-update
    with pytest.raises(ProtonUnavailableError):
        umu_runtime.assert_proton_still_complete(env)


def test_assert_proton_still_complete_ignores_non_proton_paths(tmp_path):
    from unifideck.launcher.proton.infrastructure import umu_runtime

    umu_runtime.assert_proton_still_complete(None)
    umu_runtime.assert_proton_still_complete({})
    # umu resolves some PROTONPATH values itself; no 'proton' script is not
    # our failure to report.
    umu_runtime.assert_proton_still_complete({"PROTONPATH": str(tmp_path)})


# ── one owner for the compat config block (register 63) ──

def test_compat_config_has_a_single_reader(tmp_path, monkeypatch):
    """``proton_config`` is the only place that reads ``compat`` off disk.

    ``selector.get_unifideck_proton_tool`` and ``external_ge`` each used to
    keep their own hardcoded path and their own ``except (OSError,
    ValueError)`` for the same block — two copies of one fact, which is how
    the path in one drifts from the path in the other.
    """
    from unifideck.launcher.proton.infrastructure import external_ge as ext
    from unifideck.launcher.proton.infrastructure import proton_config

    cfg = tmp_path / "config.json"
    monkeypatch.setattr(proton_config, "CONFIG_PATH", cfg)

    # Patching the one owner must steer BOTH readers.
    cfg.write_text(json.dumps({"compat": {
        "proton_tool": "GE-Proton11-3", "external_ge": "off",
    }}))
    assert selector.get_unifideck_proton_tool() == "GE-Proton11-3"
    assert ext.external_ge_enabled() is False

    cfg.write_text(json.dumps({"compat": {"external_ge": "auto"}}))
    assert selector.get_unifideck_proton_tool() is None
    assert ext.external_ge_enabled() is True


def test_compat_config_never_raises(tmp_path, monkeypatch):
    """A missing/corrupt config leaves the launcher on defaults, not broken."""
    from unifideck.launcher.proton.infrastructure import external_ge as ext
    from unifideck.launcher.proton.infrastructure import proton_config

    cfg = tmp_path / "config.json"
    monkeypatch.setattr(proton_config, "CONFIG_PATH", cfg)
    for content in (None, "{not json", '["a list"]', '{"compat": "not a dict"}'):
        if content is None:
            cfg.unlink(missing_ok=True)
        else:
            cfg.write_text(content)
        assert proton_config.read_compat_section() == {}
        assert selector.get_unifideck_proton_tool() is None
        assert ext.external_ge_enabled() is True, f"must default to auto for {content!r}"
