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
def no_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    """No Flatpak and no native Edge: keeps the probe off the real system."""
    monkeypatch.setattr(probe.shutil, "which", lambda _name: None)


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
    assert block["browser"] == {"kind": "absent"}
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


@pytest.mark.parametrize(
    ("filesystems", "writable"),
    [
        (["home"], True),
        (["host"], True),
        (["home:ro"], False),
        (["!home", "xdg-download"], False),
        (["xdg-download", "/run/udev:ro"], False),
        (["~/.local/share/unifideck"], True),
        (["xdg-data/unifideck/edge-auth:create"], True),
        (["~/.local/share/unifideck-other"], False),
        (["/home/deck/.local/share/unifideck/edge-auth/Default:ro"], False),
    ],
)
def test_profile_writable(filesystems: list[str], writable: bool) -> None:
    profile = Path("/home/deck/.local/share/unifideck/edge-auth")
    assert probe.profile_writable(filesystems, profile, "/home/deck") is writable


def test_filesystems_parses_show_permissions() -> None:
    perms = "[Context]\nshared=network;ipc;\nfilesystems=home;/run/udev:ro;\n"
    assert probe._filesystems(perms) == ["home", "/run/udev:ro"]


class _View:
    """The one ``View`` method the check reads."""

    def __init__(self, block: dict[str, Any]) -> None:
        self._block = block

    def block(self, name: str) -> dict[str, Any]:
        return self._block if name == "edge_session" else {}


def _verdict(**block: Any) -> tuple[str, str]:
    base: dict[str, Any] = {"profile_exists": True, "profile_dir": "/p", "cookies": []}
    result = check_edge_session(_View({**base, **block}))  # type: ignore[arg-type]
    return result.status, result.detail


def test_check_unwritable_profile_fails() -> None:
    status, detail = _verdict(
        browser={"kind": "flatpak", "scope": "system", "profile_writable": False},
        microsoft_session="persistent",
    )
    assert status == "fail"
    assert "no write access" in detail


def test_check_session_only_warns_and_names_aborts() -> None:
    status, detail = _verdict(microsoft_session="session_only", log={"fatal_count": 2})
    assert status == "warn"
    assert "every launch" in detail
    assert "aborted 2 time(s)" in detail


def test_check_persistent_passes() -> None:
    assert _verdict(microsoft_session="persistent")[0] == "pass"


def test_check_xbox_visited_without_sign_in_warns() -> None:
    status, _ = _verdict(microsoft_session="none", cookies=[{"host": ".xbox.com", "name": "MUID"}])
    assert status == "warn"


def test_check_no_profile_is_not_applicable() -> None:
    result = check_edge_session(_View({"profile_exists": False}))  # type: ignore[arg-type]
    assert result.status == "na"
