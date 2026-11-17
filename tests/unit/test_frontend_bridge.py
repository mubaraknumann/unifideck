"""Unit tests for the launcher→plugin toast bridge (frontend_bridge).

The launcher runs as a separate process, so its LAUNCHER_STAGE toasts
are written to a shared JSONL file and drained into the plugin's replay
buffer on each ``subscribe_replay`` poll. These tests cover the writer
(record_event / launcher_toast / cap) and the reader
(LauncherEventDrainer prime → dedup → record).
"""
from __future__ import annotations

import json

import pytest

from unifideck.launcher import frontend_bridge as fb


@pytest.fixture(autouse=True)
def _bridge_file(tmp_path, monkeypatch):
    """Point the bridge at a temp file for every test."""
    monkeypatch.setattr(fb, "EVENTS_FILE", tmp_path / "launcher_events.jsonl")


# ── writer ────────────────────────────────────────────────────────

def test_launcher_toast_writes_full_payload():
    fb.launcher_toast(
        "toasts.launcher.startingEpicGame",
        i18n_title_key="toasts.launcher.launchingGame",
        i18n_params={"version": "GE-Proton10-34"},
        severity="warning",
        game_title="epic:1",
    )
    line = fb.EVENTS_FILE.read_text().splitlines()[-1]
    rec = json.loads(line)
    assert rec["event"] == "launcher_stage"
    assert isinstance(rec["ts"], (int, float))
    kw = rec["kwargs"]
    assert kw["i18n_key"] == "toasts.launcher.startingEpicGame"
    assert kw["i18n_title_key"] == "toasts.launcher.launchingGame"
    assert kw["i18n_params"] == {"version": "GE-Proton10-34"}
    assert kw["severity"] == "warning"
    assert kw["game_title"] == "epic:1"


def test_launcher_toast_omits_unset_optionals():
    fb.launcher_toast("toasts.launcher.signingIn")
    kw = json.loads(fb.EVENTS_FILE.read_text().splitlines()[-1])["kwargs"]
    assert "i18n_title_key" not in kw
    assert "i18n_params" not in kw
    assert "severity" not in kw


def test_compat_tool_request_carries_the_unsigned_appid_and_the_tool():
    """The frontend calls SpecifyCompatTool with exactly these args."""
    assert fb.request_steam_compat_tool(-110954320, "GE-Proton11-12", "ea:1") is True

    kw = json.loads(fb.EVENTS_FILE.read_text().splitlines()[-1])["kwargs"]
    assert kw["action"] == {"verb": fb.COMPAT_TOOL_VERB, "args": ["4184012976", "GE-Proton11-12"]}
    assert kw["i18n_key"] == "toasts.launcher.protonSwitchedTo"
    assert kw["i18n_title_key"] == "toasts.header.proton"
    assert kw["i18n_params"] == {"version": "GE-Proton11-12"}


def test_compat_tool_request_is_not_muted_by_warmup_suppression():
    """The setting is the point; only toasts are noise during an install."""
    with fb.suppress_launcher_toasts():
        assert fb.request_steam_compat_tool("4184012976", "GE-Proton11-12") is True
    assert fb.EVENTS_FILE.is_file()


@pytest.mark.parametrize("appid", [None, "", "not-a-number"])
def test_compat_tool_request_without_an_appid_writes_nothing(appid):
    assert fb.request_steam_compat_tool(appid, "GE-Proton11-12") is False
    assert not fb.EVENTS_FILE.exists()


def test_record_event_caps_file_length():
    for i in range(fb._MAX_LINES + 50):
        fb.record_event("launcher_stage", {"i": i})
    lines = fb.EVENTS_FILE.read_text().splitlines()
    assert len(lines) == fb._MAX_LINES
    # The most recent events are the ones kept.
    assert json.loads(lines[-1])["kwargs"]["i"] == fb._MAX_LINES + 49


def test_record_event_swallows_unserialisable(monkeypatch):
    # default=str keeps it best-effort rather than raising into a launch.
    fb.record_event("launcher_stage", {"obj": object()})
    assert fb.EVENTS_FILE.is_file()


# ── reader (drainer) ──────────────────────────────────────────────

def test_poll_primes_then_returns_only_new():
    drainer = fb.LauncherEventDrainer()

    # Backlog written before the UI started polling.
    fb.launcher_toast("toasts.launcher.startingGogGame")
    assert drainer.poll_new() == []  # prime only — no stale toasts

    # A genuinely new event after priming is returned once.
    import time
    time.sleep(0.01)
    fb.launcher_toast("toasts.launcher.protonSwitchedTo")
    fresh = drainer.poll_new()
    assert len(fresh) == 1
    assert fresh[0]["i18n_key"] == "toasts.launcher.protonSwitchedTo"

    # Idempotent — re-polling returns nothing new.
    assert drainer.poll_new() == []


def test_poll_missing_file_is_noop():
    drainer = fb.LauncherEventDrainer()
    assert drainer.poll_new() == []  # primes to 0
    assert drainer.poll_new() == []


def test_poll_skips_malformed_lines():
    fb.EVENTS_FILE.write_text('not json\n{"event":"launcher_stage"}\n')
    drainer = fb.LauncherEventDrainer()
    # No valid ts → nothing to prime/return.
    assert drainer.poll_new() == []
