"""Tests for the Edge-session probe and its Xbox sign-in verdict.

The probe exists to answer "Game Pass asks me to log in every time" from a
bundle, and it reads a cookie database to do it. So the first thing it must
get right is never shipping a cookie value.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from unifideck.services.support_bundle import probe_edge_session as probe
from unifideck.services.support_bundle.checks_edge_session import check_edge_session

SENTINEL = "SUPERSECRET-DO-NOT-SHIP"

#: 2027-10-15 in Chromium time (microseconds since 1601-01-01).
_EXPIRES_2027 = 13_468_032_000_000_000


def _write_cookies(data_dir: Path, rows: list[tuple[str, str, int]]) -> None:
    """A minimal Chromium ``Cookies`` DB with ``(host, name, persistent)`` rows."""
    db = data_dir / "edge-auth" / "Default" / "Cookies"
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT, "
        "encrypted_value BLOB, is_persistent INTEGER, expires_utc INTEGER, "
        "last_access_utc INTEGER)",
    )
    conn.executemany(
        "INSERT INTO cookies VALUES (?,?,?,?,?,?,?)",
        [
            (host, name, SENTINEL, SENTINEL.encode(), persistent,
             _EXPIRES_2027 if persistent else 0, _EXPIRES_2027)
            for host, name, persistent in rows
        ],
    )
    conn.commit()
    conn.close()


@pytest.fixture
def no_browser() -> None:
    """The probe reads only ``<data>/browser``, which ``tmp_path`` leaves empty."""


@pytest.mark.usefixtures("no_browser")
def test_cookie_values_never_reach_the_report(tmp_path: Path) -> None:
    _write_cookies(tmp_path, [(".live.com", "MSPAuth", 1), (".xbox.com", "MUID", 1)])
    block = probe.edge_session_block(str(tmp_path))
    assert SENTINEL not in json.dumps(block)
    assert {c["name"] for c in block["cookies"]} == {"MSPAuth", "MUID"}
    assert block["microsoft_session"] == "persistent"
    assert block["cookies"][0]["expires"].startswith("2027-")


@pytest.mark.usefixtures("no_browser")
def test_non_microsoft_cookies_are_not_listed(tmp_path: Path) -> None:
    _write_cookies(tmp_path, [(".amazon.com", "session-token", 1), (".gog.com", "gog_us", 1)])
    block = probe.edge_session_block(str(tmp_path))
    assert block["cookies"] == []
    assert block["microsoft_session"] == "none"


@pytest.mark.usefixtures("no_browser")
def test_session_only_sign_in(tmp_path: Path) -> None:
    _write_cookies(tmp_path, [(".live.com", "MSPAuth", 0), (".live.com", "WLSSC", 0)])
    block = probe.edge_session_block(str(tmp_path))
    assert block["microsoft_session"] == "session_only"
    assert block["cookies"][0]["expires"] == ""


@pytest.mark.usefixtures("no_browser")
def test_missing_profile(tmp_path: Path) -> None:
    block = probe.edge_session_block(str(tmp_path))
    assert block["profile_exists"] is False
    assert block["cookies_db"] == {"exists": False}
    assert block["browser"] == {"kind": "absent", "builds": []}
    assert block["log"] == {"exists": False}


def test_unresolved_data_dir() -> None:
    assert "error" in probe.edge_session_block(None)


def test_log_counts_fatal_aborts(tmp_path: Path) -> None:
    log = tmp_path / "edge-auth.log"
    log.write_text(
        "DevTools listening on ws://127.0.0.1:9223/devtools/browser/x\n"
        "[2:42:1003/041957.066153:FATAL:dbus/bus.cc:1245] D-Bus connection was disconnected. Aborting.\n"
        "[3:3:1003/050000.000000:ERROR:ui/gl/foo.cc:1] not fatal\n",
    )
    result = probe._log_block(log)
    assert result["fatal_count"] == 1
    assert "D-Bus connection was disconnected" in result["last_fatal"]


def _build(root: Path, version: str, *, executable: bool, verified: bool = True,
           old_layout: bool = True) -> Path:
    """A downloaded build: ungoogled-chromium, or the Chrome for Testing layout."""
    if old_layout:
        build = root / "browser" / f"chrome-{version}"
        binary = build / "chrome-linux64" / "chrome"
    else:
        build = root / "browser" / f"chromium-{version}"
        binary = build / f"ungoogled-chromium-{version}-1-x86_64_linux" / "chrome"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755 if executable else 0o644)
    if verified:
        (build / ".verified").write_text(version)
    return binary


def test_the_verified_browser_is_reported(tmp_path: Path) -> None:
    binary = _build(tmp_path, "156.0.1", executable=True)
    _build(tmp_path, "155.0.1", executable=True, verified=False)
    block = probe.browser_block(tmp_path)
    assert block == {"kind": "bundled", "version": "156.0.1", "path": str(binary),
                     "executable": True, "builds": ["chrome-155.0.1", "chrome-156.0.1"]}


def test_the_ungoogled_chromium_build_is_found_over_an_older_one(tmp_path: Path) -> None:
    _build(tmp_path, "156.0.8078.80", executable=True)
    binary = _build(tmp_path, "156.0.8078.79", executable=True, old_layout=False)
    block = probe.browser_block(tmp_path)
    assert block["path"] == str(binary)
    assert block["version"] == "156.0.8078.79"
    assert block["executable"] is True


def test_a_browser_that_cannot_run_is_reported(tmp_path: Path) -> None:
    _build(tmp_path, "156.0.1", executable=False)
    assert probe.browser_block(tmp_path)["executable"] is False


def test_an_unverified_download_is_not_a_browser(tmp_path: Path) -> None:
    _build(tmp_path, "156.0.1", executable=True, verified=False)
    assert probe.browser_block(tmp_path)["kind"] == "absent"


class _View:
    """The one ``View`` method the check reads."""

    def __init__(self, block: dict[str, Any]) -> None:
        self._block = block

    def block(self, name: str) -> dict[str, Any]:
        return self._block if name == "edge_session" else {}


def _verdict(**block: Any) -> tuple[str, str]:
    base: dict[str, Any] = {"profile_exists": True, "profile_dir": "/p", "cookies": [],
                            "browser": {"kind": "bundled", "executable": True}}
    result = check_edge_session(_View({**base, **block}))  # type: ignore[arg-type]
    return result.status, result.detail


def test_check_browser_that_cannot_run_fails() -> None:
    status, detail = _verdict(
        browser={"kind": "bundled", "executable": False, "path": "/b/chrome"},
        microsoft_session="persistent",
    )
    assert status == "fail"
    assert "cannot run" in detail and "/b/chrome" in detail


def test_check_missing_browser_warns_before_cookies() -> None:
    status, detail = _verdict(browser={"kind": "absent", "builds": []}, microsoft_session="persistent")
    assert status == "warn"
    assert "not downloaded" in detail


def test_check_session_only_warns_and_names_aborts() -> None:
    status, detail = _verdict(microsoft_session="session_only", log={"fatal_count": 2})
    assert status == "warn"
    assert "falls back to xbox.com" in detail
    assert "aborted 2 time(s)" in detail


def test_check_persistent_passes() -> None:
    assert _verdict(microsoft_session="persistent")[0] == "pass"


def test_check_xbox_visited_without_sign_in_warns() -> None:
    status, _ = _verdict(microsoft_session="none", cookies=[{"host": ".xbox.com", "name": "MUID"}])
    assert status == "warn"


def test_check_no_profile_is_not_applicable() -> None:
    result = check_edge_session(_View({"profile_exists": False}))  # type: ignore[arg-type]
    assert result.status == "na"
