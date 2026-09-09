"""Regression: Epic's --wrapper invocation must not leak env pollution.

Bug report: an Epic launch failed with "python3: error while loading shared
libraries: libz.so.1" inside the pressure-vessel container, right after
umu-run started. legendary (bin/legendary) spawns the ``--wrapper`` command
(python3 + umu-run) as its own subprocess; if it hands down its own
LD_LIBRARY_PATH/LD_PRELOAD instead of the clean env it was launched with,
that pollution rides umu-run straight into the Steam Runtime container.
The fix force-clears both vars right at the legendary -> umu-run boundary.

(bin/legendary was a PyInstaller onefile binary when this was written; it
is a Python zipapp as of 0.20.40. The boundary and the fix are unchanged —
if anything a zipapp is MORE sensitive to a polluted env, since it runs
under the system python3 rather than carrying its own libraries.)

NOTE: this file originally claimed "GOG/Amazon/Ubisoft are unaffected —
they spawn umu-run directly with Unifideck's own sanitized env". That was
wrong: the sanitizer restored LD_LIBRARY_PATH from LD_LIBRARY_PATH_ORIG,
so those stores DID inherit a host library path and hit the identical
libz.so.1 failure once Steam itself went containerised (SteamOS 3.8+).
Epic's ``env -u`` here was simply masking it. Both vars are now stripped
at the shared umu spawn point too — see test_umu_runtime_env_scrub.py and
test_proton_env_sanitize.py.
"""
from __future__ import annotations

import types
from pathlib import Path

from unifideck.launcher.proton.compat import epic as compat_epic
from unifideck.launcher.proton.handlers.epic import _build_legendary_argv
from unifideck.launcher.proton.infrastructure.core import ProtonLaunchPlan


def _plan(_wrappers: list[str] | None = None) -> ProtonLaunchPlan:
    """``_wrappers`` is accepted and ignored: ``RuntimeState.wrappers``
    was deleted in audit register item 23b (Steam applies wrapper words
    pre-exec, so the field could only ever be empty)."""
    return ProtonLaunchPlan(
        context=types.SimpleNamespace(
            game_id="abc123", store="epic",
            exe_path=Path("/install/abc123.exe"),
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


def test_wrapper_force_clears_ld_env(monkeypatch):
    monkeypatch.setattr(compat_epic, "detect_offline", lambda: False)

    argv = _build_legendary_argv(_plan(), "/plugin/bin/legendary")

    wrapper_cmd = argv[argv.index("--wrapper") + 1]
    assert wrapper_cmd == (
        "env -u LD_LIBRARY_PATH -u LD_PRELOAD "
        "/usr/bin/python3 /plugin/bin/umu/umu/umu-run"
    )


def test_json_mode_asks_for_the_recipe_without_user_wrappers(monkeypatch):
    """UD-126: the ``--json`` call resolves parameters and exits before
    legendary can fork the game. A user's launch-option wrapper belongs
    on the game (re-applied by ``epic_launch_params.build_umu_argv``), not
    on a metadata query — but the umu ``--wrapper`` must still be there,
    because it comes back as the JSON's ``launch_command``."""
    monkeypatch.setattr(compat_epic, "detect_offline", lambda: False)

    argv = _build_legendary_argv(
        _plan(["gamemoderun"]), "/plugin/bin/legendary", json_mode=True,
    )

    assert "--json" in argv
    assert "gamemoderun" not in argv
    assert argv[0] == "/plugin/bin/legendary"
    assert "--wrapper" in argv


def test_launch_mode_argv_starts_with_legendary(monkeypatch):
    """The non-json argv leads with the binary, not a user wrapper.

    It asserted ``argv[0] == "gamemoderun"`` from ``RuntimeState.wrappers``,
    deleted in register item 23b — Steam applies wrapper words pre-exec, so
    the field could only ever be empty here. The UD-126 distinction this
    file exists for is unaffected: the umu ``--wrapper`` still belongs on the
    launch argv and not on the ``--json`` metadata query, which its sibling
    test pins.
    """
    monkeypatch.setattr(compat_epic, "detect_offline", lambda: False)

    argv = _build_legendary_argv(_plan(), "/plugin/bin/legendary")

    assert argv[0] == "/plugin/bin/legendary"
    assert "--json" not in argv
