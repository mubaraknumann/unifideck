"""UD-126: Epic must launch the game itself, not hand it to legendary.

``legendary launch`` ends in ``subprocess.Popen(full_params, …)`` with no
``wait()`` (``legendary/cli.py::launch_game`` in the bundled zipapp), so
awaiting it returned ~2s after the game was forked and the launcher
exited while the game was still starting. Steam ended the session there;
in Gaming Mode gamescope only raises a window whose app Steam still
believes is running, so a slow-starting title (Rocket League) came up as
audio with no window while Desktop Mode looked fine. Stop, playtime and
cloud sync-up all keyed off the same dead session.

The fix asks legendary for the recipe (``--json``, which returns *before*
that ``Popen``) and spawns umu-run ourselves. These tests pin the two
halves that must not drift from legendary's own assembly in ``cli.py``::

    full_params = launch_command + [join(game_directory, game_executable)]
                + game_parameters + user_parameters + egl_parameters

and the failure contract: legendary's real complaint ("Game is out of
date…") must reach the user instead of a bare exit code.
"""
from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from unifideck.launcher.proton.compat import epic_launch_params as elp
from unifideck.launcher.proton.infrastructure.core import ProtonLaunchPlan
from unifideck.launcher.types.errors import GameFailedError

WRAPPER = [
    "env", "-u", "LD_LIBRARY_PATH", "-u", "LD_PRELOAD",
    "/usr/bin/python3", "/plugin/bin/umu/umu/umu-run",
]


def _plan(_wrappers: list[str] | None = None) -> ProtonLaunchPlan:
    """``_wrappers`` is accepted and ignored: ``RuntimeState.wrappers``
    was deleted in audit register item 23b (Steam applies wrapper words
    pre-exec, so the field could only ever be empty)."""
    return ProtonLaunchPlan(
        context=types.SimpleNamespace(
            game_id="abc123", store="epic",
            exe_path=Path("/install/Game.exe"),
            work_dir=Path("/install"),
        ),
        state=types.SimpleNamespace(
            game_args=[], umu_id=None,
        ),
        python_bin=Path("/usr/bin/python3"),
        umu_wrapper=Path("/plugin/bin/umu/umu/umu-run"),
        prefix_path=Path("/tmp/prefix"),  # noqa: S108
        env={},
        on_process_start=None,
    )


def _params(**overrides) -> dict:
    base = {
        "launch_command": list(WRAPPER),
        "game_executable": "Binaries/Win64/Game.exe",
        "game_directory": "/games/RocketLeague",
        "working_directory": "/games/RocketLeague/Binaries/Win64",
        "game_parameters": ["-nostartupmovies"],
        "user_parameters": ["-windowed"],
        "egl_parameters": [
            "-AUTH_LOGIN=unused", "-AUTH_PASSWORD=deadbeef",
            "-AUTH_TYPE=exchangecode", "-epicapp=Sugar", "-EpicPortal",
        ],
        "environment": {},
        "pre_launch_command": "",
        "pre_launch_wait": False,
    }
    base.update(overrides)
    return base


# ── argv assembly ────────────────────────────────────────────────────


def test_argv_matches_legendarys_own_ordering():
    argv = elp.build_umu_argv(_plan(), _params())

    assert argv == [
        *WRAPPER,
        "/games/RocketLeague/Binaries/Win64/Game.exe",
        "-nostartupmovies",          # game_parameters
        "-windowed",                 # user_parameters
        "-AUTH_LOGIN=unused",        # egl_parameters, last
        "-AUTH_PASSWORD=deadbeef",
        "-AUTH_TYPE=exchangecode",
        "-epicapp=Sugar",
        "-EpicPortal",
    ]


def test_absolute_executable_override_wins_over_game_directory():
    """``--override-exe`` resolves to an absolute path (user "Change
    executable", and the Rockstar-on-Epic launch shim). ``os.path.join``
    semantics — which legendary itself relies on — must be preserved, or
    the override would be appended to the install dir and not exist."""
    argv = elp.build_umu_argv(
        _plan(), _params(game_executable="/games/GTAV/PlayGTAV.exe"),
    )

    assert argv[len(WRAPPER)] == "/games/GTAV/PlayGTAV.exe"


def test_the_argv_starts_with_the_umu_wrapper():
    """No user wrapper is prepended any more, and umu's own still leads.

    This asserted ``argv[0] == "gamemoderun"``, re-applying a Steam
    launch-option wrapper from ``RuntimeState.wrappers``. That field is
    deleted (register item 23b): Steam applies wrapper words **pre-exec**,
    measured — ``env %command% epic:Salt`` ran the launcher *under* ``env``
    and still delivered ``argv[1]``. So by the time this builder runs the
    wrapping has already happened, and the field it read could only ever be
    empty. umu's ``--wrapper`` is a different thing and still leads.
    """
    argv = elp.build_umu_argv(_plan(), _params())

    assert argv[:len(WRAPPER)] == WRAPPER


def test_missing_optional_lists_are_tolerated():
    """An offline launch has no egl auth params; absent keys must not
    crash the assembly."""
    argv = elp.build_umu_argv(
        _plan(),
        {
            "launch_command": list(WRAPPER),
            "game_executable": "Game.exe",
            "game_directory": "/games/X",
        },
    )

    assert argv == [*WRAPPER, "/games/X/Game.exe"]


# ── environment / cwd ────────────────────────────────────────────────


def test_legendary_env_section_overlays_ours():
    merged = elp.merge_environment(
        {"STORE": "none", "GAMEID": "umu-1"},
        _params(environment={"DXVK_HUD": "fps", "STORE": "egs"}),
    )

    assert merged["DXVK_HUD"] == "fps"
    assert merged["STORE"] == "egs", "legendary config wins, as in cli.py"
    assert merged["GAMEID"] == "umu-1"


def test_empty_env_section_returns_the_same_mapping():
    env = {"GAMEID": "umu-1"}

    assert elp.merge_environment(env, _params()) is env


def test_cwd_is_none_when_working_directory_is_missing(tmp_path):
    assert elp.resolve_cwd(_params(working_directory="")) is None
    assert elp.resolve_cwd(_params(working_directory="/no/such/dir")) is None
    assert elp.resolve_cwd(_params(working_directory=str(tmp_path))) == tmp_path


# ── resolving the parameters from legendary ──────────────────────────


@pytest.fixture()
def fake_legendary(monkeypatch):
    """Script legendary's rc/stdout/stderr for ``resolve_launch_params``."""
    calls: list[list[str]] = []

    def _install(rc: int, stdout: bytes, stderr: bytes = b""):
        async def _spawn(*argv, **_kwargs):
            calls.append(list(argv))

            async def _communicate():
                return stdout, stderr

            return types.SimpleNamespace(
                communicate=_communicate, returncode=rc, kill=lambda: None,
            )

        monkeypatch.setattr(elp.asyncio, "create_subprocess_exec", _spawn)
        return calls

    return _install


async def test_parameters_are_parsed_from_stdout(fake_legendary):
    fake_legendary(
        0, json.dumps(_params()).encode(), b"[cli] INFO: Logging in...\n",
    )

    params = await elp.resolve_launch_params(["legendary", "launch", "x", "--json"], {})

    assert params["game_executable"] == "Binaries/Win64/Game.exe"


async def test_legendary_failure_surfaces_its_stderr(fake_legendary):
    """The whole point of capturing stderr: "Game is out of date" used to
    vanish into game.log and reach the user as a bare exit code."""
    fake_legendary(
        1, b"",
        b"[cli] INFO: Logging in...\n"
        b"[cli] ERROR: Game is out of date, please update!\n",
    )

    with pytest.raises(GameFailedError) as err:
        await elp.resolve_launch_params(["legendary"], {})

    assert "Game is out of date" in str(err.value)
    assert err.value.subprocess_rc == 1
    assert "Game is out of date" in err.value.context["legendary_stderr"]


async def test_json_survives_chatter_around_it(fake_legendary):
    """Under Steam's Force-Compat the call is escaped through
    ``steam-runtime-launch-client``, which sits between legendary and this
    pipe. A stray line must not fail the launch."""
    fake_legendary(
        0, b"pv-launch: connecting\n" + json.dumps(_params()).encode() + b"\n",
    )

    params = await elp.resolve_launch_params(["legendary"], {})

    assert params["game_executable"] == "Binaries/Win64/Game.exe"


async def test_unparsable_output_fails_loudly(fake_legendary):
    fake_legendary(0, b"not json at all", b"[cli] ERROR: something odd\n")

    with pytest.raises(GameFailedError):
        await elp.resolve_launch_params(["legendary"], {})


async def test_payload_without_executable_fails(fake_legendary):
    fake_legendary(0, json.dumps({"launch_command": WRAPPER}).encode())

    with pytest.raises(GameFailedError, match="no launchable executable"):
        await elp.resolve_launch_params(["legendary"], {})


async def test_payload_without_wrapper_fails(fake_legendary):
    """Running the .exe with no umu wrapper would execute a Windows binary
    on the host with no Proton — fail instead."""
    fake_legendary(
        0, json.dumps(_params(launch_command=[])).encode(),
    )

    with pytest.raises(GameFailedError, match="dropped the umu wrapper"):
        await elp.resolve_launch_params(["legendary"], {})
