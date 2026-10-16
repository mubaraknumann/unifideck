"""Tests for tagging a companion executable's window alongside the game.

Regression 1: a companion (trainer) window has no ``steam_app_<appid>``
WM_CLASS of its own, so the original tagger — which only matched WM_CLASS
— never pulled it into gamescope's FOCUSABLE_APPS for the game's appid.
``register_companion_pid`` + ``_pid_descends_from`` close that gap by
matching a companion's window via PROCESS ANCESTRY instead of WM_CLASS.

Regression 2: tagging the matched companion window with the MAIN GAME's
own appid (the original fix for regression 1) made the trainer's window
never get painted at all while the game's window held gamescope's single
per-appid focus slot — only appearing once the game's window unmapped on
exit. Each companion now gets tagged with its OWN dedicated fake appid
(via ``register_companion_pid``'s ``appid`` argument and
``_companion_appid_for_pid``), so gamescope treats it as a genuinely
separate focusable app reachable independently of the game.
"""
from __future__ import annotations

import os
import subprocess

import pytest

from unifideck.launcher.proton.infrastructure import gamescope_window_tagger as tagger


@pytest.fixture(autouse=True)
def _clean_companion_pids():
    """Isolate the module-level companion-PID map across tests."""
    with tagger._companion_pids_lock:
        tagger._companion_pid_appids.clear()
    yield
    with tagger._companion_pids_lock:
        tagger._companion_pid_appids.clear()


def test_register_companion_pid_is_visible_in_snapshot():
    tagger.register_companion_pid(4242, 999)
    assert tagger._companion_pid_appids_snapshot() == {4242: 999}


def test_register_companion_pid_supports_multiple_distinct_companions():
    tagger.register_companion_pid(4242, 999)
    tagger.register_companion_pid(5353, 1000)
    assert tagger._companion_pid_appids_snapshot() == {4242: 999, 5353: 1000}


def test_pid_descends_from_matches_the_root_itself():
    assert tagger._pid_descends_from(100, {100, 200}) is True


def test_pid_descends_from_empty_roots_is_false():
    assert tagger._pid_descends_from(100, set()) is False


def test_pid_descends_from_real_process_chain(tmp_path):
    """Use a REAL child process so ``/proc/<pid>/stat`` parsing is exercised
    against actual kernel-provided data, not a mock."""
    proc = subprocess.Popen(["sleep", "5"])
    try:
        # proc.pid's parent is THIS test process (os.getpid()).
        assert tagger._pid_descends_from(proc.pid, {os.getpid()}) is True
        assert tagger._pid_descends_from(proc.pid, {999999999}) is False
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_companion_appid_for_pid_matches_the_root_itself():
    assert tagger._companion_appid_for_pid(100, {100: 999}) == 999


def test_companion_appid_for_pid_matches_a_descendant(tmp_path):
    proc = subprocess.Popen(["sleep", "5"])
    try:
        assert tagger._companion_appid_for_pid(proc.pid, {os.getpid(): 777}) == 777
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_companion_appid_for_pid_returns_none_when_unmatched():
    assert tagger._companion_appid_for_pid(100, {}) is None
    assert tagger._companion_appid_for_pid(100, {999999999: 999}) is None


def test_parent_pid_of_self_is_this_process_parent():
    # Every live process has a valid, positive PPID.
    ppid = tagger._parent_pid(os.getpid())
    assert ppid is not None
    assert ppid > 0


def test_parent_pid_of_nonexistent_pid_is_none():
    assert tagger._parent_pid(999999999) is None


class _FakeWindow:
    def __init__(self, wid: int, wm_class=None):
        self.id = wid
        self._wm_class = wm_class
        self.tagged_with: list[int] = []

    def get_wm_class(self):
        return self._wm_class

    def change_property(self, atom, prop_type, size, data):
        self.tagged_with.extend(data)


class _FakeDisplay:
    def get_atom(self, name):
        return name

    def flush(self):
        pass


def test_matches_main_game_by_wm_class():
    win = _FakeWindow(1, wm_class=("steam_app_123", "steam_app_123"))
    assert tagger._matches_main_game(win, "steam_app_123") is True


def test_matches_main_game_false_for_unrelated_wm_class():
    win = _FakeWindow(1, wm_class=("trainer", "TrainerWindow"))
    assert tagger._matches_main_game(win, "steam_app_123") is False


def test_try_tag_leaves_companion_window_untagged_per_skip_mode(monkeypatch):
    """A companion window (unrelated WM_CLASS) still matches the PID-based
    companion rule when its _NET_WM_PID descends from a registered
    companion PID — but with ``_COMPANION_TAG_MODE == "skip"`` (the
    deliberate default — see the module docstring's "skip" rationale) the
    match is recorded as already-handled WITHOUT writing any STEAM_GAME
    property, so the window is marked seen but left untagged."""
    tagger.register_companion_pid(555, 999)
    monkeypatch.setattr(tagger, "_window_pid", lambda window, atom: 555)

    win = _FakeWindow(2, wm_class=("trainer.exe", "Wine"))
    d = _FakeDisplay()
    tagged: set[int] = set()

    tagger._try_tag(d, win, "STEAM_GAME", "_NET_WM_PID", "steam_app_123", 123, tagged)

    assert win.tagged_with == []
    assert 2 in tagged


def test_try_tag_skips_unrelated_window_with_no_companion_match(monkeypatch):
    tagger.register_companion_pid(555, 999)
    monkeypatch.setattr(tagger, "_window_pid", lambda window, atom: 111)  # unrelated pid

    win = _FakeWindow(3, wm_class=("some_other_app", "SomeOtherApp"))
    d = _FakeDisplay()
    tagged: set[int] = set()

    tagger._try_tag(d, win, "STEAM_GAME", "_NET_WM_PID", "steam_app_123", 123, tagged)

    assert win.tagged_with == []
    assert 3 not in tagged


def test_try_tag_never_double_tags_same_window():
    win = _FakeWindow(4, wm_class=("steam_app_123", "steam_app_123"))
    d = _FakeDisplay()
    tagged = {4}  # already tagged in a previous poll

    tagger._try_tag(d, win, "STEAM_GAME", "_NET_WM_PID", "steam_app_123", 123, tagged)

    assert win.tagged_with == []  # short-circuited, no redundant re-tag


def test_try_tag_tags_main_game_window_with_its_own_appid_not_a_companions():
    """Two DIFFERENT companions registered; the main-game window must still
    get tagged with the main game's appid, never a companion's."""
    tagger.register_companion_pid(555, 999)
    tagger.register_companion_pid(556, 1000)

    win = _FakeWindow(5, wm_class=("steam_app_123", "steam_app_123"))
    d = _FakeDisplay()
    tagged: set[int] = set()

    tagger._try_tag(d, win, "STEAM_GAME", "_NET_WM_PID", "steam_app_123", 123, tagged)

    assert win.tagged_with == [123]


def test_try_tag_prefers_companion_match_even_when_wm_class_also_matches_main_game(
    monkeypatch,
):
    """Regression: a companion process INHERITS the main game's own
    SteamAppId/SteamGameId env vars (see ``companions.start_companions``'s
    comment for why — popping or overriding them made gamescope refuse to
    composite the companion's window at all), so Wine gives the
    companion's window the SAME ``steam_app_<main game>`` WM_CLASS as the
    real game window. Checking WM_CLASS before the PID-based companion
    check (the original order) matched a companion window as the main
    game's OWN window and never even tried the PID-based rule — gamescope's
    focus picker then painted only one of the two identically-tagged
    windows, and the trainer stayed hidden until the game's own window
    closed. The PID-based companion match must win regardless of what the
    window's WM_CLASS happens to read — and with the deliberate
    ``_COMPANION_TAG_MODE == "skip"`` default (see module docstring), that
    match leaves the window untagged rather than writing the main game's
    appid onto it.
    """
    tagger.register_companion_pid(555, 999)
    monkeypatch.setattr(tagger, "_window_pid", lambda window, atom: 555)

    # WM_CLASS matches the main game's own steam_app_123 — exactly what a
    # companion process that inherited the game's SteamAppId produces.
    win = _FakeWindow(6, wm_class=("steam_app_123", "steam_app_123"))
    d = _FakeDisplay()
    tagged: set[int] = set()

    tagger._try_tag(d, win, "STEAM_GAME", "_NET_WM_PID", "steam_app_123", 123, tagged)

    # Left UNTAGGED (never gets the main game's 123) — the PID-based
    # companion rule wins over WM_CLASS and "skip" mode records it as
    # handled without writing a STEAM_GAME property.
    assert win.tagged_with == []
    assert 6 in tagged
