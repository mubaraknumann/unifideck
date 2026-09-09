"""The Battle.net two-phase launch handler.

Three assertions here encode facts measured on-device, each of which cost
real debugging time and none of which is obvious from the code:

* **Phase C must use ``PROTON_VERB=run``.** ``waitforexitandrun`` runs
  ``wineserver -w`` first, which blocks until the prefix's existing
  wineserver exits — and that wineserver is the client we just started.
  With it, the second invocation never reaches the exe at all.
* **Phase D is mandatory.** Blizzard renamed Diablo IV's family ``D4`` ->
  ``Fen`` and the client accepts the dead code and does nothing: no error,
  no dialog, no exit code. Only a new process proves a launch worked.
* **There is no ``Battle.net Helper.exe``.** That string is a command-line
  argument; every CEF child is named ``Battle.net.exe``.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

from unifideck.launcher.proton.handlers import battlenet as handler
from unifideck.launcher.proton.handlers import battlenet_client as client
from unifideck.stores.battlenet import paths as store_paths
from unifideck.launcher.proton.handlers import battlenet_watch as watch
from unifideck.launcher.proton.handlers import battlenet_wsi as wsi
from unifideck.launcher.proton.handlers import wrapper_clients as wc
from unifideck.launcher.types.errors import GameFailedError


async def _noop(*_a: Any, **_k: Any) -> None:
    """Stand in for a coroutine whose effect this test does not exercise."""
    return None


class _Ctx:
    def __init__(self, game_id: str = "fenris") -> None:
        self.game_id = game_id
        self.game_key = "battlenet:fenris"
        self.store = "battlenet"


class _State:
    game_exit_code: int | None = None


class _Plan:
    def __init__(self, prefix: Path) -> None:
        self.context = _Ctx()
        self.state = _State()
        self.prefix_path = prefix
        self.env = {"PROTON_VERB": "waitforexitandrun", "WINEPREFIX": str(prefix)}
        self.python_bin = Path("/usr/bin/python3")
        self.umu_wrapper = Path("/plugin/bin/umu-run")
        self.on_process_start = None


def _install_client(prefix: Path) -> None:
    # Constants come from the store package, which owns them. The launcher
    # handler used to declare identical literals of its own; those are gone
    # (audit register item 47), and the sibling Battle.net tests already
    # reached for the store's copies.
    d = prefix / "drive_c" / store_paths.CLIENT_DIR
    d.mkdir(parents=True, exist_ok=True)
    (d / store_paths.CLIENT_EXE).write_bytes(b"MZ")
    (d / store_paths.LAUNCHER_EXE).write_bytes(b"MZ")
    # The versioned payload the shim loads. Without it the prefix is
    # the shape an interrupted install leaves and no client can start.
    build = d / "Battle.net.17651"
    build.mkdir(exist_ok=True)
    (build / client.CLIENT_DLL).write_bytes(b"MZ")


@pytest.fixture
def plan(tmp_path: Path) -> _Plan:
    _install_client(tmp_path)
    return _Plan(tmp_path)


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Record what the handler does without touching the system."""
    calls: dict[str, Any] = {"exec": [], "spawned": 0, "toasts": []}

    async def fake_exec(plan_: Any, exe: Path, command: str) -> None:
        calls["exec"].append((command, dict(plan_.env)))

    async def fake_spawn(plan_: Any, exe: Path) -> None:
        calls["spawned"] += 1

    monkeypatch.setattr(handler, "_start_client_detached", fake_spawn)
    monkeypatch.setattr(
        handler, "launcher_toast",
        lambda key, **kw: calls["toasts"].append(key),
    )
    monkeypatch.setattr(client, "resolve_family", lambda uid: "Fen")
    return calls


# --------------------------------------------------------------------------
# process observation
# --------------------------------------------------------------------------


def test_client_processes_are_never_mistaken_for_the_game() -> None:
    for image in (
        "battle.net.exe", "battle.net launcher.exe", "agent.exe",
        "blizzarderror.exe", "explorer.exe", "services.exe", "xalia.exe",
    ):
        assert image in watch.EXCLUDED_IMAGES


def test_helper_exe_is_not_in_the_exclusion_list_because_it_does_not_exist() -> None:
    """`--battle-net-helper=` is an argument, not a process name."""
    assert "battle.net helper.exe" not in watch.EXCLUDED_IMAGES


def test_a_real_game_image_is_not_excluded() -> None:
    assert "hearthstone.exe" not in watch.EXCLUDED_IMAGES


def test_prefix_comparison_normalises_the_pfx_selflink(tmp_path: Path) -> None:
    """umu rewrites WINEPREFIX to <prefix>/pfx/ via a self-symlink."""
    (tmp_path / "pfx").symlink_to(".")
    assert wc.normalise_prefix(tmp_path) == wc.normalise_prefix(tmp_path / "pfx")


@pytest.mark.parametrize(
    ("cmdline", "expected"),
    [
        ("C:\\Program Files (x86)\\Battle.net\\Battle.net.exe\x00--x", "battle.net.exe"),
        ("C:/Games/Hearthstone/Hearthstone.exe\x00-launch", "hearthstone.exe"),
        ("", ""),
    ],
)
def test_image_name_extraction(cmdline: str, expected: str) -> None:
    assert wc.image_name(cmdline) == expected


# --------------------------------------------------------------------------
# family resolution
# --------------------------------------------------------------------------


def test_family_is_read_from_the_id_map_never_derived(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """uid and family are unrelated namespaces: fenris -> Fen, hs_beta -> WTCG."""
    import json

    path = tmp_path / "map.json"
    path.write_text(json.dumps({"fenris": {"family": "Fen"}}))
    monkeypatch.setattr(client, "id_map_path", lambda p=path: p)
    assert client.resolve_family("fenris") == "Fen"
    assert client.resolve_family("unknown") is None


def test_a_proven_family_wins_over_a_stale_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A family that has actually launched is never second-guessed."""
    import json

    path = tmp_path / "map.json"
    path.write_text(json.dumps({
        "fenris": {"family": "D4", "last_launch_family": "Fen", "launch_ok_at": 1.0},
    }))
    monkeypatch.setattr(client, "id_map_path", lambda p=path: p)
    assert client.resolve_family("fenris") == "Fen"


def test_missing_family_is_a_hard_failure_not_a_bare_client_open(
    plan: _Plan, stub: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opening the client with no game would look identical to success."""
    monkeypatch.setattr(handler, "resolve_family", lambda uid: None)
    with pytest.raises(GameFailedError):
        asyncio.run(handler.battlenet_launch(plan))
    assert stub["spawned"] == 0


# --------------------------------------------------------------------------
# the two-phase sequence
# --------------------------------------------------------------------------


def _arm(monkeypatch: pytest.MonkeyPatch, *, ready: bool, game: str | None) -> None:
    monkeypatch.setattr(handler.watch, "client_ready", lambda p: ready)
    monkeypatch.setattr(handler.watch, "game_pids", lambda p: set())

    async def fake_wait_ready(
        p: Any, t: float, poll: float = 2.0, proc: Any = None,
    ) -> bool:
        return ready

    async def fake_wait_game(p: Any, before: set, t: float, poll: float = 3.0) -> str | None:
        return game

    async def fake_wait_exit(
        p: Any, pid: str, *, before: set, poll: float = 10.0,
    ) -> None:
        return None

    monkeypatch.setattr(handler.watch, "wait_for_client_ready", fake_wait_ready)
    monkeypatch.setattr(handler.watch, "wait_for_game", fake_wait_game)
    monkeypatch.setattr(handler.watch, "wait_for_exit", fake_wait_exit)


def test_phase_c_uses_proton_verb_run(
    plan: _Plan, stub: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The single most load-bearing line: waitforexitandrun deadlocks."""
    monkeypatch.setattr(handler, "resolve_family", lambda uid: "Fen")
    monkeypatch.setattr(handler, "_issue_exec", _record_exec(stub))
    _arm(monkeypatch, ready=True, game="4242")
    assert asyncio.run(handler.battlenet_launch(plan)) == 0
    command, env = stub["exec"][0]
    assert command == "launch Fen"
    assert env["PROTON_VERB"] == "run"


def _record_exec(calls: dict):
    async def fake(plan_: Any, exe: Path, command: str) -> None:
        env = dict(plan_.env)
        env["PROTON_VERB"] = "run"
        calls["exec"].append((command, env))
    return fake


def test_phase_a_keeps_waitforexitandrun(plan: _Plan) -> None:
    """Phase A owns the wineserver session, so its verb is unchanged."""
    assert plan.env["PROTON_VERB"] == "waitforexitandrun"


def test_only_one_argument_is_passed(
    plan: _Plan, stub: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NSL #957: a conflicting battlenet:// arg opens the launcher instead."""
    monkeypatch.setattr(handler, "resolve_family", lambda uid: "Fen")
    monkeypatch.setattr(handler, "_issue_exec", _record_exec(stub))
    _arm(monkeypatch, ready=True, game="1")
    asyncio.run(handler.battlenet_launch(plan))
    command, _ = stub["exec"][0]
    assert "battlenet://" not in command
    assert command.count("launch") == 1


def test_silent_failure_is_detected(
    plan: _Plan, stub: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The D4 -> Fen case: command accepted, nothing launched, rc says 0."""
    monkeypatch.setattr(handler, "resolve_family", lambda uid: "D4")
    monkeypatch.setattr(handler, "_issue_exec", _record_exec(stub))
    _arm(monkeypatch, ready=True, game=None)
    with pytest.raises(GameFailedError) as excinfo:
        asyncio.run(handler.battlenet_launch(plan))
    assert "no game process appeared" in str(excinfo.value)


def test_client_that_never_becomes_ready_fails_cleanly(
    plan: _Plan, stub: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(handler, "resolve_family", lambda uid: "Fen")
    _arm(monkeypatch, ready=False, game=None)
    with pytest.raises(GameFailedError):
        asyncio.run(handler.battlenet_launch(plan))


def test_missing_client_reports_rc_127(
    tmp_path: Path, stub: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(handler, "resolve_family", lambda uid: "Fen")
    _arm(monkeypatch, ready=False, game=None)
    empty = _Plan(tmp_path)  # no client installed
    with pytest.raises(GameFailedError) as excinfo:
        asyncio.run(handler.battlenet_launch(empty))
    assert excinfo.value.context["subprocess_rc"] == 127


def test_a_running_client_is_not_started_twice(
    plan: _Plan, stub: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(handler, "resolve_family", lambda uid: "Fen")
    monkeypatch.setattr(handler, "_issue_exec", _record_exec(stub))
    _arm(monkeypatch, ready=True, game="7")
    asyncio.run(handler.battlenet_launch(plan))
    assert stub["spawned"] == 0


def _record_startup(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the prefix-preparation steps, in the order they run."""
    order: list[str] = []

    async def fake_inject(prefix: Any) -> None:
        order.append("inject")

    async def fake_tweaks(plan_: Any) -> bool:
        order.append("tweaks")
        return True

    monkeypatch.setattr(handler.session, "inject_into", fake_inject)
    monkeypatch.setattr(handler.bootstrap, "ensure_tweaks", fake_tweaks)
    return order


def test_the_prefix_is_prepared_before_the_client_starts(
    plan: _Plan, stub: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both writes have to land while the client is down.

    The client reads its session and its settings at startup and rewrites the
    settings file wholesale from memory when it exits, so anything written to a
    live client's prefix is discarded without an error.
    """
    monkeypatch.setattr(handler, "resolve_family", lambda uid: "Fen")
    monkeypatch.setattr(handler, "_issue_exec", _record_exec(stub))
    order = _record_startup(monkeypatch)
    _arm(monkeypatch, ready=True, game="7")
    # Ready only *after* we start it: this is the cold-client path.
    monkeypatch.setattr(handler.watch, "client_ready", lambda p: False)

    asyncio.run(handler.battlenet_launch(plan))

    assert order == ["inject", "tweaks"]
    assert stub["spawned"] == 1


def test_the_tweaks_never_run_before_the_injection(
    plan: _Plan, stub: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ordering with teeth, because getting it backwards is silent.

    The injection carries the user's launcher settings in, and it takes the
    *newer* of the two settings files. Writing the tweaks first would make this
    prefix's file the newer one, the injection would decline, and the setting
    would stay behind in the prefix it was changed in - which is the bug the
    settings merge exists to fix, reintroduced by a swapped pair of lines.
    """
    monkeypatch.setattr(handler, "resolve_family", lambda uid: "Fen")
    monkeypatch.setattr(handler, "_issue_exec", _record_exec(stub))
    order = _record_startup(monkeypatch)
    _arm(monkeypatch, ready=True, game="7")
    monkeypatch.setattr(handler.watch, "client_ready", lambda p: False)

    asyncio.run(handler.battlenet_launch(plan))

    assert order.index("inject") < order.index("tweaks")


def test_a_client_already_up_is_never_written_underneath(
    plan: _Plan, stub: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No preparation at all when the client is already running.

    Neither write would survive, and the settings merge would have recorded a
    change that never reached disk.
    """
    monkeypatch.setattr(handler, "resolve_family", lambda uid: "Fen")
    monkeypatch.setattr(handler, "_issue_exec", _record_exec(stub))
    order = _record_startup(monkeypatch)
    _arm(monkeypatch, ready=True, game="7")

    asyncio.run(handler.battlenet_launch(plan))

    assert order == []


# --------------------------------------------------------------------------
# gating environment
# --------------------------------------------------------------------------


def test_gating_env_is_applied_and_overrides_are_merged() -> None:
    from unifideck.launcher.proton.infrastructure.core import _apply_battlenet_env

    env = {"WINEDLLOVERRIDES": "existing=n"}
    _apply_battlenet_env(env)
    assert env["WINE_SIMULATE_WRITECOPY"] == "1"
    # The July study's PROTON_DISABLE_XALIA does not exist in Proton at all.
    assert env["PROTON_USE_XALIA"] == "0"
    assert "locationapi=d" in env["WINEDLLOVERRIDES"]
    assert "existing=n" in env["WINEDLLOVERRIDES"]


def test_the_gating_env_does_not_disable_the_wsi_layer_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The layer stays on unless THIS host has been measured to need it off.

    Disabling it costs the XWayland-bypass path (direct scanout, HDR), and
    the game inherits the client's environment, so a blanket setting would
    charge every healthy host for a bug a minority have. Measured on two
    machines with identical SteamOS 3.8.25, kernel and gamescope: the
    client works on a Steam Deck (Van Gogh) and aborts on a ROG Ally X
    (Phoenix). See ``battlenet_wsi``.
    """
    from unifideck.launcher.proton.infrastructure.core import _apply_battlenet_env

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    env: dict[str, str] = {}
    _apply_battlenet_env(env)
    assert wsi.DISABLE_VAR not in env


def test_a_recorded_host_gets_the_layer_disabled_up_front(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After one measured abort, later launches skip the doomed attempt."""
    from unifideck.launcher.proton.infrastructure.core import _apply_battlenet_env

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    wsi.record_workaround("measured in a previous launch")

    env: dict[str, str] = {}
    _apply_battlenet_env(env)
    assert env[wsi.DISABLE_VAR] == "1"


def test_gating_env_does_not_duplicate_locationapi() -> None:
    from unifideck.launcher.proton.infrastructure.core import _apply_battlenet_env

    env = {"WINEDLLOVERRIDES": "locationapi=d;other=b"}
    _apply_battlenet_env(env)
    assert env["WINEDLLOVERRIDES"].count("locationapi") == 1


# ── the readiness probe ───────────────────────────────────────────
#
# Measured on-device: /proc yielded the ``--from-launcher`` main process
# (pid 69087) before the two live renderers (69473, 69551). The loop
# returned that first process's verdict, so ``client_ready`` answered False
# while the client was plainly up — every launch then failed after the full
# 300 s timeout, and the install shortcut's keep-alive returned instantly.


class _FakeProc:
    """A /proc stand-in that preserves iteration order."""

    def __init__(self, entries: list[tuple[str, str, str]]) -> None:
        # (pid, cmdline, wineprefix)
        self._entries = entries

    def install(self, monkeypatch, _watch_mod=None) -> None:
        """Patch the /proc readers in ``wrapper_clients``.

        That is where they live since the cross-prefix scan needed them:
        ``battlenet_watch`` calls through, so patching it would not be seen.
        """
        order = [pid for pid, _, _ in self._entries]
        by_pid = {pid: (cmd, pfx) for pid, cmd, pfx in self._entries}
        monkeypatch.setattr(wc, "pids", lambda: order)

        def _field(pid: str, field: str) -> str:
            cmd, pfx = by_pid.get(pid, ("", ""))
            return cmd if field == "cmdline" else f"WINEPREFIX={pfx}\x00"

        monkeypatch.setattr(wc, "proc_field", _field)


PREFIX = "/prefixes/battlenet/D1"
_EXE = "C:\\Program Files (x86)\\Battle.net\\Battle.net.exe"
# NUL-separated, as /proc/<pid>/cmdline really is — a space-separated
# fake makes the image name parse as "battle.net.exe --type=renderer".
_MAIN = ("69087", f"{_EXE}\x00--from-launcher\x00", PREFIX)
_R1 = ("69473", f"{_EXE}\x00--type=renderer\x00", PREFIX)
_R2 = ("69551", f"{_EXE}\x00--type=renderer\x00", PREFIX)


def test_ready_when_the_main_process_is_enumerated_first(monkeypatch) -> None:
    """The exact on-device ordering that made every launch fail."""
    from unifideck.launcher.proton.handlers import battlenet_watch as w

    _FakeProc([_MAIN, _R1, _R2]).install(monkeypatch, w)
    assert w.client_ready(PREFIX) is True


def test_ready_when_a_renderer_is_enumerated_first(monkeypatch) -> None:
    from unifideck.launcher.proton.handlers import battlenet_watch as w

    _FakeProc([_R1, _MAIN]).install(monkeypatch, w)
    assert w.client_ready(PREFIX) is True


def test_not_ready_with_only_the_main_process(monkeypatch) -> None:
    """No renderer means no window yet — it cannot accept --exec."""
    from unifideck.launcher.proton.handlers import battlenet_watch as w

    _FakeProc([_MAIN]).install(monkeypatch, w)
    assert w.client_ready(PREFIX) is False


def test_running_is_weaker_than_ready(monkeypatch) -> None:
    """Liveness must survive a moment with no renderer.

    ``wait_while_client_running`` keyed on readiness returned on its first
    poll, so Steam marked the install shortcut finished while the detached
    client kept running — the tile stopped responding and the play session
    never closed.
    """
    from unifideck.launcher.proton.handlers import battlenet_watch as w

    _FakeProc([_MAIN]).install(monkeypatch, w)
    assert w.client_ready(PREFIX) is False
    assert w.client_running(PREFIX) is True


def test_another_prefix_client_is_not_ours(monkeypatch) -> None:
    """A sibling Blizzard game's client must never count as this one's."""
    from unifideck.launcher.proton.handlers import battlenet_watch as w

    other = ("70001", _R1[1], "/prefixes/battlenet/fenris")
    _FakeProc([other]).install(monkeypatch, w)
    assert w.client_ready(PREFIX) is False
    assert w.client_running(PREFIX) is False


# --------------------------------------------------------------------------
# only Windows images are processes; the umu chain is not
# --------------------------------------------------------------------------
#
# WINEPREFIX is inherited by every Linux-side wrapper umu spawns, and
# EXCLUDED_IMAGES lists only Windows names — so srt-bwrap and friends read
# as game processes. Measured on-device: phase C's OWN srt-bwrap (pid 13227)
# was logged as "game process appeared after 0s", which both defeats the
# silent-failure detector and leaves phase E watching a pid that is not the
# game, so Steam shows the shortcut running forever.

_BWRAP = ("13227", "/home/deck/.local/share/umu/pressure-vessel/bin/srt-bwrap\x00", PREFIX)
_PVADVERB = ("13309", "/usr/lib/pressure-vessel/from-host/bin/pv-adverb\x00", PREFIX)
_UMURUN = ("12633", "/usr/bin/python3.13\x00/plugin/bin/umu/umu-run\x00", PREFIX)
_D2R = ("14001", "C:\\Program Files (x86)\\Diablo II Resurrected\\D2R.exe\x00", PREFIX)
_D2R_LAUNCHER = (
    "13990",
    "C:\\Program Files (x86)\\Diablo II Resurrected\\"
    "Diablo II Resurrected Launcher.exe\x00",
    PREFIX,
)
_AGENT = ("12838", "C:\\ProgramData\\Battle.net\\Agent\\Agent.exe\x00", PREFIX)
_SERVICES = ("12740", "C:\\windows\\system32\\services.exe\x00", PREFIX)
_GPU = ("12940", f"{_EXE}\x00--type=gpu-process\x00", PREFIX)
_UTILITY = ("12949", f"{_EXE}\x00--type=utility\x00", PREFIX)
_EXEC = ("13334", f"{_EXE}\x00--exec=launch OSI\x00", PREFIX)


def test_linux_wrappers_are_not_game_processes(monkeypatch) -> None:
    """The pid-13227 bug: phase C's own srt-bwrap read as the game."""
    from unifideck.launcher.proton.handlers import battlenet_watch as w

    _FakeProc([_BWRAP, _PVADVERB, _UMURUN]).install(monkeypatch, w)
    assert w.game_pids(PREFIX) == set()


def test_real_game_images_are_game_processes(monkeypatch) -> None:
    from unifideck.launcher.proton.handlers import battlenet_watch as w

    _FakeProc([_BWRAP, _D2R, _D2R_LAUNCHER]).install(monkeypatch, w)
    assert w.game_pids(PREFIX) == {_D2R[0], _D2R_LAUNCHER[0]}


def test_the_exec_invocation_is_not_a_game_process(monkeypatch) -> None:
    """Phase C's Windows leaf is the client itself, already excluded."""
    from unifideck.launcher.proton.handlers import battlenet_watch as w

    _FakeProc([_EXEC, _AGENT, _SERVICES]).install(monkeypatch, w)
    assert w.game_pids(PREFIX) == set()


def test_wine_pids_counts_everything_including_infrastructure(monkeypatch) -> None:
    """What client_running cannot answer: is the prefix occupied at all."""
    from unifideck.launcher.proton.handlers import battlenet_watch as w

    _FakeProc([_AGENT, _SERVICES, _BWRAP]).install(monkeypatch, w)
    assert set(w.wine_pids(PREFIX)) == {_AGENT[0], _SERVICES[0]}
    assert w.client_running(PREFIX) is False


def test_lowest_pid_is_chosen_numerically(monkeypatch) -> None:
    """sorted() on pid strings puts "10000" before "9999"."""
    from unifideck.launcher.proton.handlers import battlenet_watch as w

    low = ("9999", _D2R[1], PREFIX)
    high = ("10000", _D2R[1], PREFIX)
    _FakeProc([high, low]).install(monkeypatch, w)
    pid = asyncio.run(w.wait_for_game(PREFIX, set(), 1.0, poll=0.01))
    assert pid == "9999"


# --------------------------------------------------------------------------
# teardown reaches the whole client, not just its main process
# --------------------------------------------------------------------------


def test_cef_children_alone_still_count_as_a_running_client(monkeypatch) -> None:
    """The stacking bug: gpu/utility children matched neither predicate.

    So stop_client signalled zero, the dead session stayed in the prefix,
    and because client_ready was False the next launch started a SECOND
    client on top of it. Two stacked sessions were measured on-device.
    """
    from unifideck.launcher.proton.handlers import battlenet_watch as w

    _FakeProc([_GPU, _UTILITY]).install(monkeypatch, w)
    assert w.client_running(PREFIX) is True
    assert w.client_ready(PREFIX) is False


def _spy_kill(monkeypatch, _watch_mod=None) -> list[tuple[int, int]]:
    """Spy on the signals sent, wherever the teardown lives.

    Signalling is shared with every other wrapper store now (``kill_client``
    is table-driven off ``CLIENT_IMAGES``), so ``battlenet_watch`` no longer
    imports ``os`` at all — it calls through, like it already does for the
    ``/proc`` readers.
    """
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(wc.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    return sent


def test_stop_client_signals_cef_children_and_spares_the_agent(monkeypatch) -> None:
    """Agent.exe must survive: this also runs from the INSTALL teardown."""
    import signal as sig

    from unifideck.launcher.proton.handlers import battlenet_watch as w

    _FakeProc([_MAIN, _GPU, _UTILITY, _AGENT, _SERVICES]).install(monkeypatch, w)
    sent = _spy_kill(monkeypatch, w)

    assert w.stop_client(PREFIX, timeout=0.0) == 3
    signalled = {pid for pid, _ in sent}
    assert signalled == {int(_MAIN[0]), int(_GPU[0]), int(_UTILITY[0])}
    assert int(_AGENT[0]) not in signalled
    assert int(_SERVICES[0]) not in signalled
    assert sig.SIGTERM in {s for _, s in sent}


def test_stop_stale_session_clears_everything_and_reaps(monkeypatch) -> None:
    from unifideck.launcher.proton.handlers import battlenet_watch as w
    from unifideck.launcher.proton.infrastructure import wineserver_reap as wr

    _FakeProc([_GPU, _AGENT, _SERVICES]).install(monkeypatch, w)
    sent = _spy_kill(monkeypatch, w)
    reaped: list[object] = []
    monkeypatch.setattr(wr, "reap_prefix_wineserver", lambda p: reaped.append(p))

    assert w.stop_stale_session(PREFIX, timeout=0.0) == 3
    assert {pid for pid, _ in sent} == {
        int(_GPU[0]), int(_AGENT[0]), int(_SERVICES[0]),
    }
    assert reaped == [Path(PREFIX)]


def test_stop_stale_session_is_a_noop_on_a_cold_prefix(monkeypatch) -> None:
    from unifideck.launcher.proton.handlers import battlenet_watch as w

    _FakeProc([]).install(monkeypatch, w)
    sent = _spy_kill(monkeypatch, w)
    assert w.stop_stale_session(PREFIX, timeout=0.0) == 0
    assert sent == []


# --------------------------------------------------------------------------
# phase E follows the launcher -> game hand-off
# --------------------------------------------------------------------------


def test_wait_for_exit_survives_the_launcher_handoff(monkeypatch) -> None:
    """D2R: the launcher exits once D2R.exe is up, and is NOT the game.

    Watching only the first pid ended the wait seconds in, so Steam marked
    the shortcut stopped while the game was still running.
    """
    from unifideck.launcher.proton.handlers import battlenet_watch as w

    states = [
        [_D2R_LAUNCHER, _D2R],  # hand-off in progress
        [_D2R],                 # launcher gone, game up
        [],                     # game exited
    ]

    def _advance(prefix):
        entries = states[0] if len(states) == 1 else states.pop(0)
        return {pid for pid, _, _ in entries}

    monkeypatch.setattr(w, "game_pids", _advance)
    monkeypatch.setattr(w, "_game_still_running", lambda p, pid: False)

    asyncio.run(w.wait_for_exit(PREFIX, _D2R_LAUNCHER[0], before=set(), poll=0.01))
    assert states == [[]]


def test_wait_for_exit_ignores_processes_that_predate_the_launch(monkeypatch) -> None:
    """`before` keeps a pre-existing game from holding the wait open."""
    from unifideck.launcher.proton.handlers import battlenet_watch as w

    monkeypatch.setattr(w, "game_pids", lambda p: {"999"})
    monkeypatch.setattr(w, "_game_still_running", lambda p, pid: False)

    asyncio.run(w.wait_for_exit(PREFIX, "4242", before={"999"}, poll=0.01))


# --------------------------------------------------------------------------
# phase C must not reap the client's wineserver
# --------------------------------------------------------------------------


def test_phase_c_opts_out_of_the_wineserver_reap(
    plan: _Plan, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression pin for the whole stalled-install bug.

    The reap is prefix-scoped, and phase C shares its prefix with the
    client phase A started. With the default (True), the EXEC_TIMEOUT
    cancellation SIGKILLed that client 60s into every launch and the
    Battle.net Agent died mid-download — measured on-device, the Agent's
    log going silent at the reap's exact timestamp, download frozen at 27%.
    """
    seen: dict[str, Any] = {}

    async def fake_run(argv, **kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(handler, "run_umu_with_retry", fake_run)
    asyncio.run(handler._issue_exec(plan, Path("/c/Battle.net.exe"), "launch Fen"))

    assert seen["reap_wineserver"] is False
    assert seen["max_attempts"] == 1


def test_auth_launch_opts_out_of_the_wineserver_reap(
    plan: _Plan, stub: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stop from the UI must unwind through stop_client's SIGTERM.

    A SIGKILL loses the token the client rotated into CachedData.db.

    Patched on ``auth_wsi`` rather than on this module: the sign-in run moved
    there so a first sign-in can measure the gamescope-WSI abort after
    ``run_umu_with_retry`` returns, which it must, because that abort exits 0.
    The flag under test moved with it.
    """
    seen: dict[str, Any] = {}

    async def fake_run(argv, **kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(handler.auth_wsi, "run_umu_with_retry", fake_run)
    monkeypatch.setattr(handler.watch, "stop_client", lambda p: 0)
    assert asyncio.run(handler.battlenet_auth_launch(plan)) == 0
    assert seen["reap_wineserver"] is False


# --------------------------------------------------------------------------
# a stale session is cleared before phase A
# --------------------------------------------------------------------------


def _arm_stale(
    monkeypatch: pytest.MonkeyPatch, *, wine: list[str], ready: bool,
) -> list[str]:
    cleared: list[str] = []
    monkeypatch.setattr(handler.watch, "wine_pids", lambda p: wine)
    monkeypatch.setattr(
        handler.watch, "stop_stale_session", lambda p: cleared.append(str(p)),
    )

    async def fake_wait_ready(
        p: Any, t: float, poll: float = 2.0, proc: Any = None,
    ) -> bool:
        return ready

    monkeypatch.setattr(handler.watch, "wait_for_client_ready", fake_wait_ready)
    return cleared


def test_a_stale_session_is_cleared_before_phase_a(
    plan: _Plan, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wine processes but no client that ever becomes ready → clear it.

    Phase A runs waitforexitandrun, which blocks on the prefix's existing
    wineserver, so a dead session wedges the next launch rather than just
    sitting there.
    """
    cleared = _arm_stale(monkeypatch, wine=["12838", "12740"], ready=False)
    asyncio.run(handler._clear_stale_session(plan))
    assert cleared == [str(plan.prefix_path)]


def test_a_client_that_is_still_starting_is_left_alone(
    plan: _Plan, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slow to come up is not the same as stale."""
    cleared = _arm_stale(monkeypatch, wine=["12838"], ready=True)
    asyncio.run(handler._clear_stale_session(plan))
    assert cleared == []


def test_a_cold_prefix_costs_nothing(
    plan: _Plan, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No Wine processes → no grace period, no teardown."""
    waited: list[float] = []
    cleared = _arm_stale(monkeypatch, wine=[], ready=False)

    async def _record(p: Any, t: float, poll: float = 2.0) -> bool:
        waited.append(t)
        return False

    monkeypatch.setattr(handler.watch, "wait_for_client_ready", _record)
    asyncio.run(handler._clear_stale_session(plan))
    assert cleared == []
    assert waited == []


# --------------------------------------------------------------------------
# phase A: observable, and over when the client is gone
# --------------------------------------------------------------------------


class _Exited:
    """A phase-A process handle that has already exited."""

    def __init__(self, rc: int = 1) -> None:
        self.returncode = rc


class _Running:
    """A phase-A process handle still running (asyncio leaves rc None)."""

    returncode = None


def test_a_client_that_exits_without_starting_ends_the_wait(
    plan: _Plan, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The measured failure: exited in ~20 s, then 4½ minutes of nothing.

    The old wait had no liveness condition, so a client that was provably
    gone still cost the full 300 s and reported "not ready" — which reads
    as "still starting" and pointed the diagnosis at the wrong subsystem.
    """
    monkeypatch.setattr(watch, "client_ready", lambda p: False)
    monkeypatch.setattr(watch, "wine_pids", lambda p: [])
    slept: list[float] = []

    async def _no_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(watch.asyncio, "sleep", _no_sleep)
    ready = asyncio.run(
        watch.wait_for_client_ready(plan.prefix_path, 300.0, proc=_Exited()),
    )
    assert ready is False
    # Returned on the first pass rather than polling out the deadline.
    assert slept == []


def test_a_live_wine_session_is_not_treated_as_gone(
    plan: _Plan, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The outer umu process exiting is normal; Wine still running is not gone.

    umu execs through several wrappers and the client is detached, so a
    returncode on its own must never end the wait.
    """
    monkeypatch.setattr(watch, "client_ready", lambda p: False)
    monkeypatch.setattr(watch, "wine_pids", lambda p: ["4242"])
    assert watch._client_gave_up(plan.prefix_path, _Exited()) is False


def test_a_running_process_is_never_treated_as_gone(
    plan: _Plan, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty prefix in the first seconds is normal — wineboot has not run."""
    monkeypatch.setattr(watch, "wine_pids", lambda p: [])
    assert watch._client_gave_up(plan.prefix_path, _Running()) is False
    assert watch._client_gave_up(plan.prefix_path, None) is False


def test_phase_a_output_goes_to_the_game_log(
    plan: _Plan, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Never DEVNULL again.

    A tester's client died in ~20 s and left nothing at all to read, so a
    five-minute failure had to be reasoned about from surrounding logs.
    """
    log = tmp_path / "launch.game.log"
    opened = log.open("a", encoding="utf-8")
    monkeypatch.setattr(handler, "open_game_log", lambda: opened)
    monkeypatch.setattr(handler, "escape_argv", lambda argv, env, _x: argv)
    spawned: dict[str, Any] = {}

    async def _fake_exec(*argv: str, **kwargs: Any) -> Any:
        spawned["stdout"] = kwargs["stdout"]
        spawned["stderr"] = kwargs["stderr"]
        return _Running()

    monkeypatch.setattr(handler.asyncio, "create_subprocess_exec", _fake_exec)
    asyncio.run(handler._start_client_detached(plan, Path("/c/Battle.net Launcher.exe")))

    assert spawned["stdout"] is opened
    assert spawned["stderr"] is asyncio.subprocess.STDOUT
    # Closed after the spawn: the child holds its own duplicated descriptor.
    assert opened.closed


# --------------------------------------------------------------------------
# the client is installed with the Proton that will later run it
# --------------------------------------------------------------------------


def test_the_install_uses_the_launch_plan_proton(
    plan: _Plan, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One Proton builds the prefix and runs the client, not two.

    The backend-side installer used to resolve its own, and on a host
    where the two differ the prefix ends up created by a Wine build
    nobody selected. See ``test_wrapper_store_proton_choice``.
    """
    from unifideck.launcher.proton.handlers import battlenet_bootstrap as boot

    plan.env["PROTONPATH"] = "/compat/GE-Proton11-5"
    seen: dict[str, Any] = {}

    async def _fake_bootstrap(prefix: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return type("R", (), {"success": True, "error": None, "error_code": None})()

    monkeypatch.setattr(boot, "launcher_toast", lambda *a, **k: None)
    monkeypatch.setitem(
        sys.modules,
        "unifideck.stores.battlenet.prefix.client_install",
        type("M", (), {"bootstrap_client": _fake_bootstrap}),
    )
    asyncio.run(boot.install_client(plan))

    assert seen["proton_path"] == "/compat/GE-Proton11-5"


def test_an_empty_plan_proton_is_passed_as_none(
    plan: _Plan, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty string is not a Proton path; the resolver must be free to choose."""
    from unifideck.launcher.proton.handlers import battlenet_bootstrap as boot

    plan.env["PROTONPATH"] = ""
    seen: dict[str, Any] = {}

    async def _fake_bootstrap(prefix: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return type("R", (), {"success": True, "error": None, "error_code": None})()

    monkeypatch.setattr(boot, "launcher_toast", lambda *a, **k: None)
    monkeypatch.setitem(
        sys.modules,
        "unifideck.stores.battlenet.prefix.client_install",
        type("M", (), {"bootstrap_client": _fake_bootstrap}),
    )
    asyncio.run(boot.install_client(plan))

    assert seen["proton_path"] is None


def test_an_incomplete_client_is_announced_as_a_repair(
    plan: _Plan, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """"Installing" reads as "nothing was ever set up" — this was set up."""
    from unifideck.launcher.proton.handlers import battlenet_bootstrap as boot

    # The plan fixture already installed a complete client; drop the payload
    # to leave exactly what an interrupted install produces.
    payload = client.find_payload_dir(tmp_path)
    for leftover in payload.iterdir():
        leftover.unlink()
    payload.rmdir()

    toasts: list[str] = []
    monkeypatch.setattr(boot, "launcher_toast", lambda key, **kw: toasts.append(key))
    boot._announce_install(tmp_path)

    assert toasts == ["toasts.launcher.battlenetRepairingClientMessage"]


def test_a_bare_prefix_is_announced_as_an_install(
    plan: _Plan, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from unifideck.launcher.proton.handlers import battlenet_bootstrap as boot

    toasts: list[str] = []
    monkeypatch.setattr(boot, "launcher_toast", lambda key, **kw: toasts.append(key))
    boot._announce_install(tmp_path / "nothing-here")

    assert toasts == ["toasts.launcher.battlenetInstallingClientMessage"]


# --------------------------------------------------------------------------
# an incomplete client is repaired, not launched
# --------------------------------------------------------------------------


def _break_payload(prefix: Path) -> None:
    """Leave exactly what an interrupted client install leaves behind."""
    payload = client.find_payload_dir(prefix)
    for leftover in payload.iterdir():
        leftover.unlink()
    payload.rmdir()


def test_an_incomplete_client_triggers_a_reinstall(
    plan: _Plan, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The tester's prefix: shim present, payload absent, launch impossible.

    Before this, the exes were there so nothing reinstalled, phase A
    started a launcher with nothing to hand off to, and every Battle.net
    install failed the same way indefinitely.
    """
    from unifideck.launcher.proton.handlers import battlenet_bootstrap as boot

    _break_payload(tmp_path)
    calls: list[Path] = []

    async def _fake_install(p: Any) -> Any:
        calls.append(p.prefix_path)
        # A real installer completes the payload; do the same.
        build = client.find_client_exe(p.prefix_path).parent / "Battle.net.17651"
        build.mkdir()
        (build / client.CLIENT_DLL).write_bytes(b"MZ")
        return type("R", (), {"success": True, "error": None, "error_code": None})()

    monkeypatch.setattr(boot, "install_client", _fake_install)
    asyncio.run(boot.ensure_client(plan, "battlenetPrefixNotReady", fail=handler._fail))

    assert calls == [tmp_path], "an incomplete client must be reinstalled"


def test_a_complete_client_is_left_alone(
    plan: _Plan, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The normal path must not pay for the repair path."""
    from unifideck.launcher.proton.handlers import battlenet_bootstrap as boot

    async def _never(_p: Any) -> Any:
        raise AssertionError("a healthy prefix must not be reinstalled")

    monkeypatch.setattr(boot, "install_client", _never)
    asyncio.run(boot.ensure_client(plan, "battlenetPrefixNotReady", fail=handler._fail))


def test_an_install_that_leaves_the_shim_still_fails(
    plan: _Plan, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Failing here names the problem; passing spends 300 s not naming it."""
    from unifideck.launcher.proton.handlers import battlenet_bootstrap as boot

    _break_payload(tmp_path)

    async def _incomplete(_p: Any) -> Any:
        return type("R", (), {"success": True, "error": None, "error_code": None})()

    monkeypatch.setattr(boot, "install_client", _incomplete)
    monkeypatch.setattr(handler, "launcher_toast", lambda *a, **k: None)
    with pytest.raises(GameFailedError):
        asyncio.run(
            boot.ensure_client(plan, "battlenetPrefixNotReady", fail=handler._fail),
        )


# --------------------------------------------------------------------------
# the WSI retry: measured, and only once
# --------------------------------------------------------------------------


def _arm_start(monkeypatch: pytest.MonkeyPatch, results: list[bool]) -> list[dict]:
    """Make _try_start return each of ``results`` in turn, recording the env."""
    seen: list[dict] = []
    pending = list(results)

    async def _fake_try_start(plan_: Any, exe: Path) -> bool:
        seen.append(dict(plan_.env))
        return pending.pop(0)

    monkeypatch.setattr(handler, "_try_start", _fake_try_start)
    monkeypatch.setattr(handler, "launcher_toast", lambda *a, **k: None)
    return seen


def test_a_healthy_client_never_touches_the_wsi_layer(
    plan: _Plan, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The whole point of measuring: a working host pays nothing.

    Disabling the layer costs the XWayland-bypass path, and the game
    inherits it from the client — so a host whose client starts must never
    see the variable at all.
    """
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    seen = _arm_start(monkeypatch, [True])
    monkeypatch.setattr(handler, "_release_other_clients", _noop)
    monkeypatch.setattr(handler, "_clear_stale_session", _noop)
    monkeypatch.setattr(handler.session, "inject_into", _noop)
    monkeypatch.setattr(handler.bootstrap, "ensure_tweaks", _noop)

    asyncio.run(handler._start_client_here(plan, Path("/c/Launcher.exe")))

    assert len(seen) == 1, "a healthy client must not be retried"
    assert wsi.DISABLE_VAR not in seen[0]
    assert wsi.workaround_recorded() is False


def test_the_angle_abort_is_retried_with_the_layer_off(
    plan: _Plan, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """First attempt dies, the log names why, the second attempt succeeds."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    log = tmp_path / "launch.game.log"
    log.write_text(
        "[Gamescope WSI] pEngineName: ANGLE\n"
        "vkroots.h:129: insert(Object, DispatchPtr) "
        "[with Object = VkQueue_T*]: Assertion `obj' failed.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "unifideck.launcher.proton.infrastructure.game_log.game_log_path",
        lambda: log,
    )
    seen = _arm_start(monkeypatch, [False, True])
    monkeypatch.setattr(handler, "_clear_stale_session", _noop)
    monkeypatch.setattr(handler, "_release_other_clients", _noop)
    monkeypatch.setattr(handler.session, "inject_into", _noop)
    monkeypatch.setattr(handler.bootstrap, "ensure_tweaks", _noop)

    asyncio.run(handler._start_client_here(plan, Path("/c/Launcher.exe")))

    assert len(seen) == 2
    assert wsi.DISABLE_VAR not in seen[0], "the first attempt is the honest one"
    assert seen[1][wsi.DISABLE_VAR] == "1"
    # Recorded, so the next launch skips the doomed first attempt entirely.
    assert wsi.workaround_recorded() is True


def test_a_client_that_died_for_another_reason_is_not_retried(
    plan: _Plan, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The workaround is for one named crash, not for "it did not start"."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    log = tmp_path / "launch.game.log"
    log.write_text("wine: could not load ntdll.so\n", encoding="utf-8")
    monkeypatch.setattr(
        "unifideck.launcher.proton.infrastructure.game_log.game_log_path",
        lambda: log,
    )
    seen = _arm_start(monkeypatch, [False])
    monkeypatch.setattr(handler, "_clear_stale_session", _noop)
    monkeypatch.setattr(handler, "_release_other_clients", _noop)
    monkeypatch.setattr(handler.session, "inject_into", _noop)
    monkeypatch.setattr(handler.bootstrap, "ensure_tweaks", _noop)

    with pytest.raises(GameFailedError):
        asyncio.run(handler._start_client_here(plan, Path("/c/Launcher.exe")))

    assert len(seen) == 1
    assert wsi.workaround_recorded() is False


def test_the_layer_is_not_disabled_twice(
    plan: _Plan, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Already off and still dying means the layer was never the problem."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    log = tmp_path / "launch.game.log"
    log.write_text(
        "[Gamescope WSI] x\nvkroots.h:129: Assertion `obj' failed.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "unifideck.launcher.proton.infrastructure.game_log.game_log_path",
        lambda: log,
    )
    plan.env[wsi.DISABLE_VAR] = "1"
    seen = _arm_start(monkeypatch, [False])
    monkeypatch.setattr(handler, "_clear_stale_session", _noop)
    monkeypatch.setattr(handler, "_release_other_clients", _noop)
    monkeypatch.setattr(handler.session, "inject_into", _noop)
    monkeypatch.setattr(handler.bootstrap, "ensure_tweaks", _noop)

    with pytest.raises(GameFailedError):
        asyncio.run(handler._start_client_here(plan, Path("/c/Launcher.exe")))

    assert len(seen) == 1, "no second attempt when the layer is already off"
