"""Tests for the "companion executables" feature.

Covers the three layers:

* ``dispatcher._resolve_companion_executables`` — reads
  ``games.<store>:<game_id>.companion_executables`` from user config into
  a tuple of ``CompanionExecutable``, tolerating malformed entries.
* ``CompanionExecutablesRPCMixin`` — add/list/remove persistence and the
  "existing path updates its delay instead of duplicating" behaviour.
* ``launcher.proton.infrastructure.companions`` — ``start_companions``
  schedules one task per companion honouring its delay, and
  ``CompanionSupervisor.stop_all`` cancels/kills every spawned process
  without raising even when a companion already finished on its own.
  ``_companion_fake_appid`` computes the deterministic, per-companion
  fake appid used ONLY for gamescope focus tagging (see
  ``test_gamescope_companion_tagging.py`` for why it must be distinct
  from the main game's real appid).
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

from unifideck.launcher.dispatcher import _resolve_companion_executables
from unifideck.launcher.types.context import CompanionExecutable
from unifideck.rpc import RpcError
from unifideck.rpc.mixins.companion_executables import CompanionExecutablesRPCMixin


# ── dispatcher._resolve_companion_executables ──────────────────────────
def test_resolve_companion_executables_reads_config(tmp_path, monkeypatch):
    user_cfg = tmp_path / "config.json"
    user_cfg.write_text(json.dumps({
        "games": {
            "gog:123": {
                "companion_executables": [
                    {"path": "/home/deck/Downloads/Trainer.exe", "delay_seconds": 5},
                    {"path": "/home/deck/Downloads/Other.exe"},
                ],
            },
        },
    }))
    monkeypatch.setenv("UNIFIDECK_USER_CONFIG", str(user_cfg))

    result = _resolve_companion_executables("gog", "123")

    assert result == (
        CompanionExecutable(path="/home/deck/Downloads/Trainer.exe", delay_seconds=5.0),
        CompanionExecutable(path="/home/deck/Downloads/Other.exe", delay_seconds=0.0),
    )


def test_resolve_companion_executables_empty_without_config(tmp_path, monkeypatch):
    user_cfg = tmp_path / "config.json"
    user_cfg.write_text(json.dumps({"games": {}}))
    monkeypatch.setenv("UNIFIDECK_USER_CONFIG", str(user_cfg))

    assert _resolve_companion_executables("gog", "unknown") == ()


def test_resolve_companion_executables_skips_malformed_entries(tmp_path, monkeypatch):
    user_cfg = tmp_path / "config.json"
    user_cfg.write_text(json.dumps({
        "games": {
            "gog:123": {
                "companion_executables": [
                    {"path": "/ok.exe"},
                    {"no_path": "here"},
                    "not-a-dict",
                    {"path": ""},
                ],
            },
        },
    }))
    monkeypatch.setenv("UNIFIDECK_USER_CONFIG", str(user_cfg))

    result = _resolve_companion_executables("gog", "123")

    assert result == (CompanionExecutable(path="/ok.exe", delay_seconds=0.0),)


# ── CompanionExecutablesRPCMixin ───────────────────────────────────────
class _FakeConfig:
    def __init__(self):
        self.d: dict = {}

    def get(self, key, default=None):
        return self.d.get(key, default)

    def set(self, key, value):
        self.d[key] = value


def _make_host() -> CompanionExecutablesRPCMixin:
    host = CompanionExecutablesRPCMixin()
    host.config = _FakeConfig()
    return host


def test_list_empty_by_default():
    host = _make_host()
    out = asyncio.run(host.list_companion_executables("gog", "123"))
    assert out == {"companions": []}


def test_add_then_list(tmp_path):
    host = _make_host()
    exe = tmp_path / "Trainer.exe"
    exe.write_text("")

    res = asyncio.run(
        host.add_companion_executable("gog", "123", str(exe), 2.5),
    )
    assert res["success"] is True
    assert res["companions"] == [{"path": str(exe), "delay_seconds": 2.5}]

    out = asyncio.run(host.list_companion_executables("gog", "123"))
    assert out["companions"] == [{"path": str(exe), "delay_seconds": 2.5}]


def test_add_rejects_missing_file(tmp_path):
    host = _make_host()
    missing = str(tmp_path / "does-not-exist.exe")
    with pytest.raises(RpcError):
        asyncio.run(host.add_companion_executable("gog", "123", missing))


def test_add_same_path_twice_updates_delay_not_duplicate(tmp_path):
    host = _make_host()
    exe = tmp_path / "Trainer.exe"
    exe.write_text("")

    asyncio.run(host.add_companion_executable("gog", "123", str(exe), 1.0))
    res = asyncio.run(host.add_companion_executable("gog", "123", str(exe), 9.0))

    assert res["companions"] == [{"path": str(exe), "delay_seconds": 9.0}]


def test_remove_drops_entry(tmp_path):
    host = _make_host()
    exe = tmp_path / "Trainer.exe"
    exe.write_text("")
    asyncio.run(host.add_companion_executable("gog", "123", str(exe)))

    res = asyncio.run(host.remove_companion_executable("gog", "123", str(exe)))

    assert res["companions"] == []
    out = asyncio.run(host.list_companion_executables("gog", "123"))
    assert out["companions"] == []


def test_remove_nonexistent_is_a_noop(tmp_path):
    host = _make_host()
    res = asyncio.run(
        host.remove_companion_executable("gog", "123", "/never/added.exe"),
    )
    assert res["success"] is True
    assert res["companions"] == []


# ── launcher.proton.infrastructure.companions ──────────────────────────
@dataclass
class _FakeCtx:
    companion_executables: tuple = ()
    exe_path: Path = field(default_factory=lambda: Path("/tmp/game.exe"))
    game_key: str = "gog:123"


@dataclass
class _FakeState:
    proton_path: Path | None = field(default_factory=lambda: Path("/proton/proton"))


@dataclass
class _FakePlan:
    context: _FakeCtx
    state: _FakeState = field(default_factory=_FakeState)
    env: dict = field(default_factory=dict)


def test_start_companions_empty_returns_no_tasks():
    from unifideck.launcher.proton.infrastructure.companions import start_companions

    async def _run():
        plan = _FakePlan(context=_FakeCtx(companion_executables=()))
        supervisor = start_companions(plan)
        assert supervisor.tasks == []
        await supervisor.stop_all()

    asyncio.run(_run())


def test_start_companions_skips_when_proton_path_missing(tmp_path):
    """Fails soft (no tasks) rather than raising into the main launch when
    the plan's state never got a resolved proton_path — should not happen
    by the time a plan exists, but the main game must never be affected."""
    from unifideck.launcher.proton.infrastructure.companions import start_companions

    exe = tmp_path / "Trainer.exe"
    exe.write_text("")

    async def _run():
        plan = _FakePlan(
            context=_FakeCtx(
                companion_executables=(
                    CompanionExecutable(path=str(exe), delay_seconds=0.0),
                ),
            ),
            state=_FakeState(proton_path=None),
        )
        supervisor = start_companions(plan)
        assert supervisor.tasks == []
        await supervisor.stop_all()

    asyncio.run(_run())


def test_start_companions_schedules_one_task_per_entry(tmp_path):
    from unifideck.launcher.proton.infrastructure.companions import start_companions

    exe = tmp_path / "Trainer.exe"
    exe.write_text("")

    async def _run():
        plan = _FakePlan(
            context=_FakeCtx(
                companion_executables=(
                    CompanionExecutable(path=str(exe), delay_seconds=0.0),
                ),
            ),
        )
        supervisor = start_companions(plan)
        assert len(supervisor.tasks) == 1
        await supervisor.stop_all()

    asyncio.run(_run())


def test_zero_delay_companion_is_raised_to_minimum_floor(tmp_path, monkeypatch):
    """A 0s companion must not race the main game's fresh-prefix init.

    Regression: a trainer configured with 0s delay started an umu-run
    against the SAME Wine prefix the main game's own umu-run was still
    initialising, and the main game exited with rc=41 within ~9s —
    reproduced live (Tempest Rising / GameVault). ``_run_one_companion``
    must clamp any delay below the launcher's floor up to it, so a 0s
    (the picker's own default) can never trigger this by surprise.
    """
    import unifideck.launcher.proton.infrastructure.companions as companions_mod
    from unifideck.launcher.proton.infrastructure.companions import (
        _run_one_companion,
    )

    monkeypatch.setattr(companions_mod, "_MIN_COMPANION_DELAY_SECONDS", 5.0)
    slept: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    async def _run():
        await _run_one_companion(
            str(tmp_path / "missing.exe"),  # missing → returns right after sleep
            0.0,
            proton_script=Path("/proton/proton"),
            env={},
            cwd=None,
            game_title="gamevault:227",
            fake_appid=1,
        )

    asyncio.run(_run())

    assert slept == [5.0]  # clamped up from 0.0 to the (patched) floor


def test_delay_above_floor_is_left_unchanged(tmp_path, monkeypatch):
    import unifideck.launcher.proton.infrastructure.companions as companions_mod
    from unifideck.launcher.proton.infrastructure.companions import (
        _run_one_companion,
    )

    monkeypatch.setattr(companions_mod, "_MIN_COMPANION_DELAY_SECONDS", 5.0)
    slept: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    async def _run():
        await _run_one_companion(
            str(tmp_path / "missing.exe"),
            30.0,
            proton_script=Path("/proton/proton"),
            env={},
            cwd=None,
            game_title="gamevault:227",
            fake_appid=1,
        )

    asyncio.run(_run())

    assert slept == [30.0]  # user's explicit delay already exceeds the floor


def test_missing_companion_file_is_skipped_without_raising(tmp_path, caplog):
    from unifideck.launcher.proton.infrastructure.companions import (
        _run_one_companion,
    )

    async def _run():
        await _run_one_companion(
            str(tmp_path / "missing.exe"),
            0.0,
            proton_script=Path("/proton/proton"),
            env={},
            cwd=None,
            game_title="gog:123",
            fake_appid=1,
        )

    asyncio.run(_run())  # must not raise


def test_run_one_companion_invokes_proton_script_directly_with_runinprefix(
    tmp_path, monkeypatch,
):
    """Regression: spawning the companion through umu-run (python_bin +
    umu_wrapper) made umu build its OWN fresh pressure-vessel/bwrap
    sandbox — a SEPARATE container from the main game's, even with an
    identical WINEPREFIX — which left the companion's OpenProcess call
    against the main game's PID denied ("Address not found. Reason: Access
    Denied.", the FLiNG-trainer error observed on-device). Calling proton's
    own script directly with the ``runinprefix`` verb (mirroring
    ``protontricks-launch --no-bwrap``, the community-documented working
    fix for the identical problem) avoids building that second sandbox.
    """
    from unifideck.launcher.proton.infrastructure.companions import (
        _run_one_companion,
    )

    exe = tmp_path / "Trainer.exe"
    exe.write_text("")

    captured: dict = {}

    class _FakeProc:
        pid = 4242
        returncode = 0

        async def wait(self):
            return 0

    async def _fake_create_subprocess_exec(*argv, **kwargs):
        captured["argv"] = argv
        return _FakeProc()

    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", _fake_create_subprocess_exec,
    )

    async def _run():
        await _run_one_companion(
            str(exe),
            0.0,
            proton_script=Path("/proton/proton"),
            env={},
            cwd=None,
            game_title="gog:123",
            fake_appid=1,
        )

    asyncio.run(_run())

    assert captured["argv"] == ("/proton/proton", "runinprefix", str(exe))


# ── companions._companion_fake_appid ────────────────────────────────────
def test_companion_fake_appid_is_deterministic():
    from unifideck.launcher.proton.infrastructure.companions import (
        _companion_fake_appid,
    )

    first = _companion_fake_appid("gog:123", "/home/deck/Downloads/Trainer.exe")
    second = _companion_fake_appid("gog:123", "/home/deck/Downloads/Trainer.exe")

    assert first == second
    assert isinstance(first, int)
    assert first >= 0  # unsigned 32-bit, never negative


def test_companion_fake_appid_differs_per_companion_path():
    from unifideck.launcher.proton.infrastructure.companions import (
        _companion_fake_appid,
    )

    trainer = _companion_fake_appid("gog:123", "/home/deck/Downloads/Trainer.exe")
    other = _companion_fake_appid("gog:123", "/home/deck/Downloads/Other.exe")

    assert trainer != other


def test_companion_fake_appid_differs_from_the_games_real_appid_scheme():
    """The companion appid must never collide with a real Steam shortcut
    appid's format — it's a DIFFERENT hash input (includes the companion's
    own path), so it lands on a different value even for the same game_key
    that ``games_map.generate_app_id`` would hash for the game itself."""
    from unifideck.launcher.proton.infrastructure.companions import (
        _companion_fake_appid,
    )
    from unifideck.services.shortcut.games_map import generate_app_id

    game_key = "gog:123"
    companion_appid = _companion_fake_appid(game_key, "/home/deck/Downloads/Trainer.exe")
    real_game_appid = generate_app_id("gog", game_key)

    assert companion_appid != real_game_appid


def test_start_companions_sets_proton_verb_runinprefix(tmp_path, monkeypatch):
    """Regression: the main game's default PROTON_VERB=waitforexitandrun
    made a companion's own proton invocation block forever on
    ``wineserver -w`` (waiting for the ALREADY-RUNNING main game's wine
    processes to exit) — reproduced live (Tempest Rising / GameVault): the
    companion's proton/wineserver process stayed alive for minutes with no
    trainer.exe ever spawned underneath it. Companions must get
    PROTON_VERB=runinprefix (umu-launcher's documented fix for a second
    process in an already-occupied prefix) while every other env var stays
    identical to the main game's.
    """
    from unifideck.launcher.proton.infrastructure.companions import start_companions

    exe = tmp_path / "Trainer.exe"
    exe.write_text("")

    seen_envs: list[dict] = []

    async def _fake_run_one_companion(path, delay, *, env, **kwargs):
        seen_envs.append(env)

    import unifideck.launcher.proton.infrastructure.companions as companions_mod

    monkeypatch.setattr(companions_mod, "_run_one_companion", _fake_run_one_companion)

    async def _run():
        plan = _FakePlan(
            context=_FakeCtx(
                companion_executables=(
                    CompanionExecutable(path=str(exe), delay_seconds=0.0),
                ),
            ),
            env={"PROTON_VERB": "waitforexitandrun", "WINEPREFIX": "/prefix"},
        )
        supervisor = start_companions(plan)
        await asyncio.sleep(0)
        await supervisor.stop_all()

    asyncio.run(_run())

    assert len(seen_envs) == 1
    assert seen_envs[0]["PROTON_VERB"] == "runinprefix"
    assert seen_envs[0]["WINEPREFIX"] == "/prefix"  # everything else untouched


def test_start_companions_inherits_steam_appid_vars_unmodified(
    tmp_path, monkeypatch,
):
    """Final settled behaviour (after a two-part regression history):

    1. A companion inherited the MAIN GAME's own SteamAppId/SteamGameId
       (from ``dict(plan.env)``), so Wine itself set the companion
       window's WM_CLASS to the main game's own ``steam_app_<appid>``.
       Combined with an over-eager WM_CLASS-based main-game match running
       BEFORE the PID-based companion rule, this could make gamescope's
       focus picker paint only one of the two identically-tagged windows.
    2. A first fix attempt POPPED the SteamAppId family entirely so Wine
       sets no ``steam_app_*`` WM_CLASS on the companion's window at all.
       That regressed FURTHER on device: popping/overriding these vars
       made gamescope refuse to composite the companion's window at all
       (see ``start_companions``'s own comment for the on-device
       analysis) — worse than the original bug.

    The settled fix (see ``companions.start_companions``'s comment) is to
    leave SteamAppId/SteamGameId/etc. INHERITED, verbatim, from the main
    game's env — not popped, not overridden — matching CheatDeck's own
    "sidecar inherits main game's env" approach, which needs no custom
    X11 tagging at all. ``gamescope_window_tagger``'s PID-based companion
    rule (checked BEFORE the WM_CLASS rule — see its module docstring)
    is what actually disambiguates the two windows, not env stripping.
    """
    from unifideck.launcher.proton.infrastructure.companions import (
        start_companions,
    )

    exe = tmp_path / "Trainer.exe"
    exe.write_text("")

    seen_envs: list[dict] = []

    async def _fake_run_one_companion(path, delay, *, env, **kwargs):
        seen_envs.append(env)

    import unifideck.launcher.proton.infrastructure.companions as companions_mod

    monkeypatch.setattr(companions_mod, "_run_one_companion", _fake_run_one_companion)

    game_key = "gamevault:221"

    async def _run():
        plan = _FakePlan(
            context=_FakeCtx(
                game_key=game_key,
                companion_executables=(
                    CompanionExecutable(path=str(exe), delay_seconds=0.0),
                ),
            ),
            env={
                "STEAM_COMPAT_APP_ID": "2719151430",
                "SteamAppId": "2719151430",
                "SteamGameId": "2719151430",
                "SteamOverlayGameId": "2719151430",
                "WINEPREFIX": "/prefix",
            },
        )
        supervisor = start_companions(plan)
        await asyncio.sleep(0)
        await supervisor.stop_all()

    asyncio.run(_run())

    assert len(seen_envs) == 1
    env = seen_envs[0]
    for steam_appid_var in (
        "STEAM_COMPAT_APP_ID", "SteamAppId", "SteamGameId",
        "SteamOverlayGameId",
    ):
        assert env[steam_appid_var] == "2719151430"  # inherited, unmodified
    assert env["WINEPREFIX"] == "/prefix"  # untouched


def test_start_companions_does_not_mutate_the_main_games_plan_env(tmp_path, monkeypatch):
    """The main game's own launch must keep waitforexitandrun — only the
    per-companion copy of env may be overridden."""
    from unifideck.launcher.proton.infrastructure.companions import start_companions

    exe = tmp_path / "Trainer.exe"
    exe.write_text("")

    async def _fake_run_one_companion(*args, **kwargs):
        pass

    import unifideck.launcher.proton.infrastructure.companions as companions_mod

    monkeypatch.setattr(companions_mod, "_run_one_companion", _fake_run_one_companion)

    async def _run():
        plan = _FakePlan(
            context=_FakeCtx(
                companion_executables=(
                    CompanionExecutable(path=str(exe), delay_seconds=0.0),
                ),
            ),
            env={"PROTON_VERB": "waitforexitandrun"},
        )
        supervisor = start_companions(plan)
        await asyncio.sleep(0)
        await supervisor.stop_all()
        assert plan.env["PROTON_VERB"] == "waitforexitandrun"

    asyncio.run(_run())


def test_start_companions_gives_each_companion_a_distinct_fake_appid(tmp_path, monkeypatch):
    """Two companions on the same game must be tagged with DIFFERENT
    dedicated appids — not each other's, and not the main game's — so
    gamescope's per-appid focus slot never collides between them."""
    from unifideck.launcher.proton.infrastructure.companions import start_companions

    trainer = tmp_path / "Trainer.exe"
    trainer.write_text("")
    other = tmp_path / "Other.exe"
    other.write_text("")

    seen_appids: list[int] = []

    async def _fake_run_one_companion(path, delay, *, fake_appid, **kwargs):
        seen_appids.append(fake_appid)

    import unifideck.launcher.proton.infrastructure.companions as companions_mod

    monkeypatch.setattr(companions_mod, "_run_one_companion", _fake_run_one_companion)

    async def _run():
        plan = _FakePlan(
            context=_FakeCtx(
                companion_executables=(
                    CompanionExecutable(path=str(trainer), delay_seconds=0.0),
                    CompanionExecutable(path=str(other), delay_seconds=0.0),
                ),
            ),
        )
        supervisor = start_companions(plan)
        assert len(supervisor.tasks) == 2
        await asyncio.sleep(0)  # let both tasks actually run once
        await supervisor.stop_all()

    asyncio.run(_run())

    assert len(seen_appids) == 2
    assert seen_appids[0] != seen_appids[1]


def test_stop_all_cancels_pending_delay_without_error():
    from unifideck.launcher.proton.infrastructure.companions import (
        CompanionSupervisor,
        _run_one_companion,
    )

    async def _run():
        supervisor = CompanionSupervisor()
        task = asyncio.ensure_future(
            _run_one_companion(
                "/never/reached.exe",
                delay_seconds=60.0,  # long enough to still be sleeping
                proton_script=Path("/proton/proton"),
                env={},
                cwd=None,
                game_title="gog:123",
                fake_appid=1,
            ),
        )
        supervisor.tasks.append(task)
        await asyncio.sleep(0)  # let the task start sleeping
        await supervisor.stop_all()  # must not raise
        assert task.cancelled()

    asyncio.run(_run())
