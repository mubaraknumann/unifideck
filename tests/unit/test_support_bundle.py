"""Tests for the Capture Logs collector.

Covers the archive itself: that it opens, that its metadata matches
reality, that truncation keeps the tail, and above all that a
credential file can never end up inside it.

Every test builds a fake home with ``monkeypatch.setenv("HOME", ...)``
so nothing reads the developer's real device.
"""
from __future__ import annotations

import json
import tempfile
import zipfile
from pathlib import Path
from typing import Any

import pytest

from unifideck.services.support_bundle import collect
from unifideck.services.support_bundle.spec import (
    MAX_LAUNCH_LOGS,
    BundleContext,
    SourceSpec,
)

SENTINEL = "SUPERSECRET-DO-NOT-SHIP"


class _FakeConfig:
    """Minimal ConfigManager stand-in."""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self._values = values or {}

    def get_str(self, key: str, default: str = "") -> str:
        return self._values.get(key, default)

    def get_int(self, key: str, default: int = 0) -> int:
        return default

    def get(self, key: str, default: Any = None) -> Any:
        return self._values.get(key, default)


class _FakePaths:
    """ServicePaths stand-in exposing only what the bundle reads."""

    def __init__(self, home: Path) -> None:
        self.data_dir = str(home / ".local" / "share" / "unifideck")
        self.plugin_dir = str(home / "homebrew" / "plugins" / "Unifideck")
        self.steam_root = str(home / ".local" / "share" / "Steam")
        self.launcher_path = f"{self.plugin_dir}/bin/unifideck-launcher"
        self.shortcuts_path = str(
            home / ".local/share/Steam/userdata/1/config/shortcuts.vdf",
        )
        self.games_map_path = f"{self.data_dir}/games.map"
        self.playtime_db = f"{self.data_dir}/playtime.db"
        self.grid_dir = str(home / ".local/share/Steam/userdata/1/config/grid")
        self.config_vdf_path = str(
            home / ".local/share/Steam/userdata/1/config/localconfig.vdf",
        )
        self.loginusers_path = str(home / ".local/share/Steam/config/loginusers.vdf")
        self.activity_log = f"{self.data_dir}/sync_activity.log"
        self.queue_file = f"{self.data_dir}/download_queue.json"


def _write(path: Path, text: str) -> Path:
    """Create a file and every parent directory it needs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _fake_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point HOME at a scratch directory and clear Decky's env."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in (
        "DECKY_PLUGIN_LOG_DIR", "DECKY_HOME", "DECKY_PLUGIN_DIR",
        "DECKY_PLUGIN_NAME", "DECKY_PLUGIN_RUNTIME_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    return home


def _populate(home: Path) -> None:
    """Build a realistic-enough fake device tree."""
    data = home / ".local" / "share" / "unifideck"
    _write(home / "homebrew/logs/Unifideck/2026-07-23 16.21.34.log", "boot ok\n")
    _write(data / "launches/abc123.log", "launch start\n")
    _write(data / "launches/abc123.game.log", "wine stdout\n")
    _write(data / "settings.json", '{"locale": "en-US"}')
    _write(data / "launcher_events.jsonl", '{"event": "x", "ts": 1}\n')
    _write(data / "sync_activity.log", '{"event": "sync"}\n')
    _write(data / "shortcuts_registry.json", '{"12345": {"name": "Game"}}')
    _write(data / "games.map", "12345 epic game-a\n")


def _capture(
    home: Path, tmp_path: Path, **kwargs: Any,
) -> dict[str, Any]:
    """Run a capture into a scratch destination."""
    dest = tmp_path / "downloads"
    dest.mkdir(exist_ok=True)
    return collect.capture_bundle(
        dest_path=str(dest),
        config=_FakeConfig(),
        paths=_FakePaths(home),
        **kwargs,
    )


def _members(result: dict[str, Any]) -> dict[str, bytes]:
    """Read every archive member into memory."""
    with zipfile.ZipFile(result["archive_path"]) as archive:
        assert archive.testzip() is None
        return {name: archive.read(name) for name in archive.namelist()}


# ── the archive ───────────────────────────────────────────────────
def test_capture_writes_openable_zip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    home = _fake_home(monkeypatch, tmp_path)
    _populate(home)
    result = _capture(home, tmp_path)
    members = _members(result)
    for generated in (
        "manifest.json", "diagnostics.txt", "environment.json", "inventory.txt",
    ):
        assert generated in members
    assert any(name.startswith("decky/") for name in members)
    assert any(name.startswith("launches/") for name in members)


def test_returned_metadata_matches_the_archive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    home = _fake_home(monkeypatch, tmp_path)
    _populate(home)
    result = _capture(home, tmp_path)
    archive = Path(result["archive_path"])
    assert result["bytes"] == archive.stat().st_size
    assert result["file_count"] == len(_members(result))
    assert result["archive_name"] == archive.name
    assert result["in_progress"] is False


def test_empty_home_still_produces_a_bundle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A device with nothing on it must still yield a readable bundle.

    This is the case where a bundle matters most: the user is reporting
    that nothing works, so refusing to produce one would be perverse.
    """
    home = _fake_home(monkeypatch, tmp_path)
    result = _capture(home, tmp_path)
    members = _members(result)
    assert "manifest.json" in members
    assert result["file_count"] >= 4


def test_missing_decky_log_dir_is_a_skip_not_a_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    home = _fake_home(monkeypatch, tmp_path)
    _write(home / ".local/share/unifideck/settings.json", "{}")
    result = _capture(home, tmp_path)
    reasons = {item["key"]: item["reason"] for item in result["skipped"]}
    assert reasons.get("decky_session_logs") == "missing"
    assert result["errors"] == []


# ── the security property ─────────────────────────────────────────
@pytest.mark.parametrize(
    "relative",
    [
        ".local/share/unifideck/epic_auth_url.txt",
        ".local/share/unifideck/gog_auth_url.txt",
        ".local/share/unifideck/ms_auth_url.txt",
        ".local/share/unifideck/amazon_auth_url.txt",
        ".local/share/unifideck/ubisoft_upc_session.txt",
        ".config/unifideck/gog_token.json",
        ".config/unifideck/gogdl_auth.json",
        ".config/unifideck/microsoft_token.json",
        ".config/unifideck/microsoft_tokens.json",
        ".config/unifideck/device_fingerprint.json",
        ".config/legendary/user.json",
        ".config/nile/user.json",
        ".local/share/unifideck/edge-auth/Default/Cookies",
        ".local/share/unifideck/playtime.db-wal",
        ".local/share/unifideck/prefixes/1234/pfx/user.reg",
        ".local/share/unifideck/save_backups/game/save.dat",
        ".local/share/unifideck/ubisoft_uuid_catalog.json",
        ".local/share/unifideck/ubisoft_game_db.txt",
    ],
)
def test_secret_files_never_reach_the_archive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, relative: str,
) -> None:
    """The one property this feature must never get wrong.

    Each candidate is written with a sentinel and the whole archive is
    decompressed and searched. Checking member *names* alone would miss
    a file collected under some other row's name.
    """
    home = _fake_home(monkeypatch, tmp_path)
    _populate(home)
    _write(home / relative, f"{SENTINEL} {relative}")
    result = _capture(home, tmp_path)
    members = _members(result)
    leaked = [name for name, blob in members.items() if SENTINEL.encode() in blob]
    assert leaked == [], f"{relative} leaked into {leaked}"
    assert Path(relative).name not in members


def test_credentials_are_still_audited_as_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Presence-only really means present *and* unread.

    "Your GOG token is missing" is the diagnostic; its contents never
    are. So the audit must report the file, with a size, while the
    archive contains none of its bytes.
    """
    home = _fake_home(monkeypatch, tmp_path)
    _populate(home)
    _write(home / ".config/unifideck/gog_token.json", f'{{"t": "{SENTINEL}"}}')
    result = _capture(home, tmp_path)
    manifest = json.loads(_members(result)["manifest.json"])
    row = next(item for item in manifest["paths"] if item["key"] == "gog_token")
    assert row["status"] == "present"
    assert row["size"] > 0
    assert row["action"] == "presence_only"
    assert all(SENTINEL.encode() not in blob for blob in _members(result).values())


# ── truncation ────────────────────────────────────────────────────
def test_read_capped_keeps_the_tail_not_the_head() -> None:
    """The reported failure is at the end of an append-only log.

    Tested directly rather than by writing a file bigger than the real
    cap: the caps are deliberately large enough that a device never
    reaches them, so generating one would cost seconds per run to prove
    a property this function owns entirely.
    """
    body = b"FIRST_LINE_MARKER\n" + (b"x" * 4000) + b"\nLAST_LINE_MARKER\n"
    path = Path(tempfile.mkdtemp()) / "big.log"
    path.write_bytes(body)
    data, source_size, truncated = collect._read_capped(path, 1024, tail=True)
    assert truncated is True
    assert source_size == len(body)
    assert b"LAST_LINE_MARKER" in data
    assert b"FIRST_LINE_MARKER" not in data
    assert b"TRUNCATED by Capture Logs" in data


def test_read_capped_never_emits_a_partial_first_line() -> None:
    """A tail that starts mid-record breaks JSONL parsing."""
    lines = [json.dumps({"n": index, "pad": "y" * 100}) for index in range(200)]
    path = Path(tempfile.mkdtemp()) / "events.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    data, _size, truncated = collect._read_capped(path, 2048, tail=True)
    assert truncated is True
    for raw in data.decode("utf-8").splitlines():
        if raw.startswith("..."):
            continue
        json.loads(raw)


def test_read_capped_returns_whole_file_under_the_cap() -> None:
    path = Path(tempfile.mkdtemp()) / "small.log"
    path.write_bytes(b"all of it\n")
    data, size, truncated = collect._read_capped(path, 1024, tail=True)
    assert data == b"all of it\n"
    assert size == 10
    assert truncated is False


def test_read_capped_keeps_content_when_the_window_has_no_newline() -> None:
    """Newline alignment must not be able to eat the whole payload.

    The partial-first-line drop used to run unconditionally, so a tail window
    containing no line break at all — one very long line, or a single-line
    file, both of which real game logs produce — partitioned down to zero
    bytes and collected a banner reading "kept 0" instead of the content.
    """
    path = Path(tempfile.mkdtemp()) / "one-long-line.log"
    path.write_bytes(b"x" * 4096 + b"NEEDLE")
    data, size, truncated = collect._read_capped(path, 512, tail=True)
    assert truncated is True
    assert size == 4102
    assert b"NEEDLE" in data


def test_a_realistic_device_is_captured_whole(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Completeness is the point: nothing real should hit a cap.

    The caps exist for pathological files only. A capture of an
    ordinary device must truncate nothing and drop nothing.
    """
    home = _fake_home(monkeypatch, tmp_path)
    _populate(home)
    data = home / ".local/share/unifideck"
    # Sizes comparable to a real device: a few hundred KB of logs.
    _write(
        home / "homebrew/logs/Unifideck/2026-07-24 10.00.00.log",
        "session line\n" * 20000,
    )
    _write(data / "library_cache.json", json.dumps({str(i): "z" * 100 for i in range(2000)}))
    _write(data / "edge-auth.log", "chromium stderr line\n" * 12000)
    result = _capture(home, tmp_path)
    assert result["truncated"] == []
    assert [item for item in result["skipped"] if item["reason"] != "missing"] == []


def test_newest_n_exclusions_are_recorded_not_silent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A cap that drops files without saying so is a lie by omission.

    A live capture once kept 20 of 21 launch logs, reported the
    directory as holding 20, and recorded the exclusion nowhere.
    """
    home = _fake_home(monkeypatch, tmp_path)
    _populate(home)
    launches = home / ".local/share/unifideck/launches"
    for index in range(MAX_LAUNCH_LOGS + 3):
        _write(launches / f"log{index:04d}.log", f"launch {index}\n")
    on_disk = len([p for p in launches.glob("*.log") if not p.name.endswith(".game.log")])
    result = _capture(home, tmp_path)
    dropped = [
        item for item in result["skipped"]
        if item["reason"] == "older_than_newest_n"
    ]
    assert len(dropped) == on_disk - MAX_LAUNCH_LOGS
    assert all(item["key"] == "launch_python_logs" for item in dropped)


def test_audit_reports_the_true_file_count_despite_the_cap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The audit must describe the device, not our subset of it."""
    home = _fake_home(monkeypatch, tmp_path)
    _populate(home)
    launches = home / ".local/share/unifideck/launches"
    for index in range(MAX_LAUNCH_LOGS + 2):
        _write(launches / f"log{index:04d}.log", f"launch {index}\n")
    total = len(
        [p for p in launches.glob("*.log") if not p.name.endswith(".game.log")],
    )
    manifest = json.loads(_members(_capture(home, tmp_path))["manifest.json"])
    row = next(
        item for item in manifest["paths"] if item["key"] == "launch_python_logs"
    )
    assert row["status"] == f"present({total})"


def test_over_cap_json_is_skipped_rather_than_corrupted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A truncated JSON document is worse than an absent one.

    Half a document makes every tool downstream fail to parse it, which
    costs a support engineer a cycle to discover. Exercised through
    ``_collect_one`` with a small per-row cap, since the shipped caps
    are far larger than any real state file.
    """
    home = _fake_home(monkeypatch, tmp_path)
    target = _write(
        home / ".local/share/unifideck/library_cache.json",
        json.dumps({str(index): "z" * 100 for index in range(200)}),
    )
    spec = SourceSpec(
        key="library_cache", what="test", root="data",
        pattern="library_cache.json", arch_dir="data",
        max_bytes=512, scrub="json",
    )
    with zipfile.ZipFile(tmp_path / "t.zip", "w") as archive:
        run = collect._Run(archive, BundleContext())
        collect._collect_one(run, spec, target)
    assert run.entries == []
    assert run.skipped[0].reason == "over_cap"


# ── naming and atomicity ──────────────────────────────────────────
def test_filenames_with_spaces_are_sanitised(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Decky's log names contain spaces; shell pipelines hate them."""
    home = _fake_home(monkeypatch, tmp_path)
    _populate(home)
    result = _capture(home, tmp_path)
    members = _members(result)
    assert "decky/2026-07-23_16.21.34.log" in members
    manifest = json.loads(members["manifest.json"])
    names = {item["source_name"] for item in manifest["entries"]}
    assert "2026-07-23 16.21.34.log" in names


def test_no_duplicate_archive_members(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Two stores ship an ``installed.json``; both must survive.

    Flattening on filename alone silently produced a duplicate zip
    member, which most extractors resolve by overwriting.
    """
    home = _fake_home(monkeypatch, tmp_path)
    _populate(home)
    _write(home / ".config/legendary/installed.json", '{"epic": 1}')
    _write(home / ".config/nile/installed.json", '{"amazon": 1}')
    with zipfile.ZipFile(_capture(home, tmp_path)["archive_path"]) as archive:
        names = archive.namelist()
    assert len(names) == len(set(names))
    assert "config/legendary-installed.json" in names
    assert "config/nile-installed.json" in names


def test_no_part_file_is_left_behind(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    home = _fake_home(monkeypatch, tmp_path)
    _populate(home)
    result = _capture(home, tmp_path)
    dest = Path(result["archive_path"]).parent
    assert list(dest.glob(".*.part")) == []


def test_repeated_capture_does_not_overwrite(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Two captures in the same second must both survive."""
    home = _fake_home(monkeypatch, tmp_path)
    _populate(home)
    monkeypatch.setattr(
        "unifideck.services.support_bundle.resolve.time.strftime",
        lambda *_args: "20260101-000000",
    )
    first = _capture(home, tmp_path)
    second = _capture(home, tmp_path)
    assert first["archive_path"] != second["archive_path"]
    assert Path(first["archive_path"]).is_file()
    assert Path(second["archive_path"]).is_file()


def test_unreadable_source_records_an_error_and_continues(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """One bad file must never cost us the whole bundle."""
    home = _fake_home(monkeypatch, tmp_path)
    _populate(home)
    target = home / ".local/share/unifideck/settings.json"
    real_open = Path.open

    def _boom(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self == target:
            raise PermissionError(13, "Permission denied")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _boom)
    result = _capture(home, tmp_path)
    assert Path(result["archive_path"]).is_file()
    assert any(item["key"] == "settings" for item in result["errors"])
    assert any(name.startswith("decky/") for name in _members(result))


def test_manifest_describes_policy_and_every_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    home = _fake_home(monkeypatch, tmp_path)
    _populate(home)
    manifest = json.loads(_members(_capture(home, tmp_path))["manifest.json"])
    policy = manifest["policy"]
    assert policy["credentials_are_presence_only"] is True
    assert policy["json_over_cap_is_skipped_not_truncated"] is True
    assert policy["home_paths_preserved"] is True
    assert policy["tail_rationale"]
    assert any("_auth_url" in pattern for pattern in policy["denied_patterns"])
    assert manifest["checks"]
    assert manifest["paths"]
