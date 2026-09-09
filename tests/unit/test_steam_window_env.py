"""The Steam app identity a Proton launch must carry.

Field case (2026-08-25 bundle): Trails in the Sky and Ys I both launched,
rendered and played audio, but stayed BEHIND Steam's loading screen with only
*Abort* available. ``_build_umu_env`` never set ``SteamAppId`` and friends, so
umu defaulted the identity to 0 — every ``game.log`` in that bundle reports
``steam app id: 0`` for the gamescope surface, and Fossilize wrote the shader
cache under the wrong appid. With no identity on the window, the Deck session
never adopts it as the launched app's window and the launch screen never
dismisses.
"""
from __future__ import annotations

from pathlib import Path

from unifideck.launcher.proton.infrastructure import core
from unifideck.launcher.types.context import LaunchContext, RuntimeState
from unifideck.steam.window_env import build_steam_window_env, shortcut_game_id

# Ys I from the bundle: shortcuts.vdf stores the signed form, the reaper and
# CompatToolMapping use the unsigned one.
YS_SIGNED = -325061865
YS_UNSIGNED = 3969905431


def test_encodes_the_unsigned_appid_and_64_bit_gameid():
    env = build_steam_window_env(YS_UNSIGNED, log_tag="t")
    assert env["SteamAppId"] == str(YS_UNSIGNED)
    assert env["SteamGameId"] == str(YS_UNSIGNED)
    assert env["STEAM_COMPAT_APP_ID"] == str(YS_UNSIGNED)
    assert env["UMU_STEAM_GAME_ID"] == str((YS_UNSIGNED << 32) | 0x02000000)


def test_signed_appid_normalises_to_the_same_block():
    """games.map hands us the SIGNED value; encoding it raw would produce a
    negative, meaningless gameID."""
    assert build_steam_window_env(YS_SIGNED, log_tag="t") == (
        build_steam_window_env(YS_UNSIGNED, log_tag="t")
    )


def test_string_appid_accepted():
    """``ctx.steam_app_id`` is a str."""
    assert build_steam_window_env(str(YS_SIGNED), log_tag="t")["SteamAppId"] == (
        str(YS_UNSIGNED)
    )


def test_unresolvable_appid_yields_an_explicit_zero_block():
    """Explicit zeros, not an empty dict — so a stale identity inherited from
    the parent environment is overwritten rather than leaking to the child."""
    for bad in (None, 0, "", "not-a-number"):
        env = build_steam_window_env(bad, log_tag="t")
        assert env == {
            "SteamGameId": "0",
            "STEAM_COMPAT_APP_ID": "0",
            "SteamAppId": "0",
            "UMU_STEAM_GAME_ID": "0",
        }


def test_shortcut_game_id_matches_steams_encoding():
    assert shortcut_game_id(YS_UNSIGNED) == (YS_UNSIGNED << 32) | 0x02000000


# ── wiring into the launch plan ────────────────────────────────────────

def _prepare(tmp_path, monkeypatch, *, steam_app_id, env_overrides=None):
    ctx = LaunchContext(
        store="gog",
        game_id="1422440106",
        exe_path=Path("/dev/ys1plus.exe"),
        work_dir=tmp_path,
        plugin_dir=tmp_path,
        steam_app_id=steam_app_id,
        env_overrides=env_overrides or {},
    )
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    monkeypatch.setattr(core, "_resolve_prefix", lambda c: prefix)
    monkeypatch.setattr(core, "_lookup_umu_id", lambda c, s, p: None)
    monkeypatch.setattr(
        core, "_locate_umu_wrapper", lambda p, d: tmp_path / "umu-run",
    )
    return core.proton_prepare(
        ctx, RuntimeState(),
        python_bin=Path("/usr/bin/python3"),
        proton_path=tmp_path / "proton",
        proton_tool_id="GE-Proton11-5",
    )


def test_launch_plan_carries_the_shortcut_identity(tmp_path, monkeypatch):
    plan = _prepare(tmp_path, monkeypatch, steam_app_id=str(YS_SIGNED))
    assert plan.env["SteamAppId"] == str(YS_UNSIGNED)
    assert plan.env["UMU_STEAM_GAME_ID"] == str((YS_UNSIGNED << 32) | 0x02000000)


def test_launch_plan_zeroes_identity_when_appid_unknown(tmp_path, monkeypatch):
    plan = _prepare(tmp_path, monkeypatch, steam_app_id=None)
    assert plan.env["SteamAppId"] == "0"


def test_user_env_override_still_beats_the_identity_block(tmp_path, monkeypatch):
    """``ctx.env_overrides`` is applied last, so an explicit LaunchOptions
    value wins over everything the plan sets — identity included."""
    plan = _prepare(
        tmp_path, monkeypatch,
        steam_app_id=str(YS_SIGNED),
        env_overrides={"SteamAppId": "12345"},
    )
    assert plan.env["SteamAppId"] == "12345"


def test_stale_inherited_identity_is_overwritten(tmp_path, monkeypatch):
    """A SteamAppId left in the environment by whatever started the launcher
    must not survive into the game's env."""
    monkeypatch.setenv("SteamAppId", "223810")
    monkeypatch.setenv("UMU_STEAM_GAME_ID", "223810")
    plan = _prepare(tmp_path, monkeypatch, steam_app_id=str(YS_SIGNED))
    assert plan.env["SteamAppId"] == str(YS_UNSIGNED)
    assert plan.env["UMU_STEAM_GAME_ID"] != "223810"
