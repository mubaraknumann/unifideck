"""Guard test — the launch-options parser is now on the launch path.

Audit §2.9. ``launcher/types/options.py`` had zero importers for a release
while its destination was fully built: ``ctx.env_overrides`` was consumed in
two places and ``state.wrappers`` / ``state.game_args`` in eleven, and nothing
wrote any of them. This file pins the half that is now wired, and, just as
importantly, pins the half that is deliberately NOT.

What is pinned:

1. the no-options baseline is untouched, because that is what every existing
   launch does and it is the only regression that would matter;
2. a user ``KEY=value`` token reaches ``ctx.env_overrides``, which both env
   builders already apply last;
3. LSFG opt-in is detected, and the overlay merges *under* an explicit user
   value rather than over it;
4. ``promote_env_tokens`` survives a quoted value containing a space, which
   its old ``raw_options.split()`` truncated;
5. ``state.wrappers`` / ``state.game_args`` stay EMPTY. Wiring them would
   append the user's wrapper words to the game's own argv;
6. the parser's behaviour against the argv tails Steam was *observed* to
   deliver (measured on a Deck, 2026-08-25; table in audit §2.9). Steam
   performs wrapping itself and does not export an assignment written after
   the game key, which is why the env half needed wiring and ``wrappers``
   cannot be reached from here at all.
"""
from __future__ import annotations

import os

import pytest

from unifideck.launcher.argv_options import (
    env_overrides_from,
    parse_argv,
    promote_env_tokens,
)
from unifideck.launcher.types.context import LaunchContext, RuntimeState
from unifideck.launcher.types.options import parse_launch_options, tokenize_options
from unifideck.services.launcher.service import LauncherService


def _ctx(raw_options: str, tmp_path) -> LaunchContext:
    """A launch context carrying ``raw_options``, as the dispatcher builds it."""
    return LaunchContext(
        store="epic",
        game_id="abc123",
        exe_path=tmp_path / "game.exe",
        work_dir=tmp_path,
        plugin_dir=tmp_path,
        raw_options=raw_options,
        env_overrides=env_overrides_from(raw_options),
    )


# ========================================================= #
# 1. The baseline: no options at all
# ========================================================= #
@pytest.mark.parametrize("raw", ["", "   "])
def test_no_options_changes_nothing(raw: str, tmp_path) -> None:
    """The overwhelmingly common case. If this moves, the wiring is wrong.

    A Unifideck shortcut's ``LaunchOptions`` is just ``store:game_id``, which
    lands in ``argv[1]`` and never reaches ``raw_options`` (that is
    ``argv[2:]``). So the normal launch parses an empty string.
    """
    ctx = _ctx(raw, tmp_path)
    state = LauncherService._build_runtime_state(ctx)

    assert ctx.env_overrides == {}
    assert not hasattr(state, "wrappers")
    assert state.game_args == []
    assert state.lsfg_requested is False


# ========================================================= #
# 2. Env overrides now reach the game
# ========================================================= #
def test_user_env_token_reaches_env_overrides(tmp_path) -> None:
    """Before §2.9 this dict was always empty, so the token was dropped."""
    ctx = _ctx("WINEDLLOVERRIDES=winemenubuilder.exe=d", tmp_path)
    assert ctx.env_overrides == {"WINEDLLOVERRIDES": "winemenubuilder.exe=d"}


def test_lowercase_token_is_not_an_env_override(tmp_path) -> None:
    """The regex is uppercase-only, which is load-bearing.

    ``service.py`` used to read ``ctx.env_overrides["started_at"]`` as an
    internal data channel. That read is gone, but the dict is user-controlled
    now, so a lowercase key must not be able to arrive through it.
    """
    ctx = _ctx("started_at=999 lowercase=x", tmp_path)
    assert ctx.env_overrides == {}


def test_env_override_wins_over_the_lsfg_overlay(tmp_path, monkeypatch) -> None:
    """Merge order: the explicit token beats the script's value."""
    script = tmp_path / "lsfg"
    script.write_text('export ENABLE_LSFG="0"\nexport LSFG_MULTIPLIER="2"\n')
    monkeypatch.setenv("HOME", str(tmp_path))

    overrides = env_overrides_from("ENABLE_LSFG=1")
    # Non-vacuous: prove the script was actually read before checking who won.
    assert overrides["LSFG_MULTIPLIER"] == "2"
    assert overrides["ENABLE_LSFG"] == "1", "script value beat the user's token"


# ========================================================= #
# 3. LSFG opt-in
# ========================================================= #
@pytest.mark.parametrize(
    ("raw", "expected"),
    [("LSFG=1", True), ("ENABLE_LSFG=1", True), ("", False), ("LSFG=0", False)],
)
def test_lsfg_opt_in_is_detected(raw: str, expected: bool, tmp_path) -> None:
    state = LauncherService._build_runtime_state(_ctx(raw, tmp_path))
    assert state.lsfg_requested is expected


def test_lsfg_overlay_reads_the_script(tmp_path, monkeypatch) -> None:
    script = tmp_path / "lsfg"
    script.write_text(
        "#!/bin/sh\n"
        'export LSFG_MULTIPLIER="3"\n'
        "# a comment\n"
        "exec something\n",
    )
    monkeypatch.setenv("HOME", str(tmp_path))

    overlay = env_overrides_from("LSFG=1")
    assert overlay["ENABLE_LSFG"] == "1"
    assert overlay["LSFG_MULTIPLIER"] == "3"


def test_no_lsfg_overlay_without_the_opt_in(tmp_path, monkeypatch) -> None:
    (tmp_path / "lsfg").write_text('export LSFG_MULTIPLIER="3"\n')
    monkeypatch.setenv("HOME", str(tmp_path))
    assert env_overrides_from("") == {}


# ========================================================= #
# 4. The merged tokenizer
# ========================================================= #
def test_promote_env_tokens_keeps_a_quoted_value_intact() -> None:
    """The bug the tokenizer merge fixed.

    ``promote_env_tokens`` split on whitespace, so ``KEY="a b"`` promoted
    ``'"a'``. The frontend's ``extractUserParams`` regex already matches
    quoted values, so the two were one launch-options string apart.
    """
    os.environ.pop("UNIFIDECK_TEST_QUOTED", None)
    try:
        promote_env_tokens('UNIFIDECK_TEST_QUOTED="alpha beta"')
        assert os.environ["UNIFIDECK_TEST_QUOTED"] == "alpha beta"
    finally:
        os.environ.pop("UNIFIDECK_TEST_QUOTED", None)


def test_promote_env_tokens_ignores_foreign_keys() -> None:
    """Only ``UNIFIDECK_*`` is promoted into the launcher's own environment."""
    os.environ.pop("SOME_USER_VAR", None)
    promote_env_tokens("SOME_USER_VAR=1")
    assert "SOME_USER_VAR" not in os.environ


def test_tokenize_options_falls_back_on_malformed_input() -> None:
    """An unbalanced quote must not raise on the launch path."""
    assert tokenize_options('KEY="unterminated') == ['KEY="unterminated']


# ========================================================= #
# 5. game_args, and the two changes that made wiring it safe
# ========================================================= #
def test_game_args_is_populated_from_the_argv_tail(tmp_path) -> None:
    """Wired 2026-08-26 (register item 23a), after two preconditions.

    It was deferred because the frontend's ``extractUserParams`` preserved
    the user's ``mangohud``/``gamemoderun`` into the tail, so populating
    ``game_args`` handed those to the **game**. That is fixed at the source:
    ``extractUserParams`` now keeps only ``KEY=value`` assignments, because a
    bare word after the game key was never a wrapper — Steam applies wrappers
    pre-exec, which §2.9 measured.
    """
    ctx = _ctx("-windowed --skip-intro", tmp_path)
    state = LauncherService._build_runtime_state(ctx)
    assert not hasattr(state, "wrappers")
    assert state.game_args == ["-windowed", "--skip-intro"]


def test_an_env_only_tail_yields_no_game_args(tmp_path) -> None:
    """The common case must not start passing arguments to the game.

    A user's ``KEY=value`` is an environment override (item 23), not an
    argument, and the parser must keep the two apart.
    """
    ctx = _ctx("MY_VAR=1 LSFG=1", tmp_path)
    state = LauncherService._build_runtime_state(ctx)
    assert state.game_args == []


def test_a_bare_launch_passes_nothing_to_the_game(tmp_path) -> None:
    """The regression guard: no options means no argv tail."""
    ctx = _ctx("", tmp_path)
    state = LauncherService._build_runtime_state(ctx)
    assert state.game_args == []


def test_bare_argv_tokens_would_become_game_args() -> None:
    """The measurement that stopped the wrappers/game_args half of §2.9.

    ``parse_launch_options`` expects a full Steam ``LaunchOptions`` string, in
    which ``%command%`` separates wrapper words from game arguments. The
    dispatcher gets the post-expansion argv tail, which often has no
    ``%command%`` left -- and the parser's fallback then treats every bare
    token as a game argument.

    The input below is the argv tail of the frontend's own wrapper-store
    fixture (``wrapper-shortcut-launch.test.ts``), whose ``extractUserParams``
    deliberately preserves the user's ``mangohud`` / ``gamemoderun``. Feeding
    ``game_args`` from this would append them to the game's own command line,
    because every argv builder does ``argv.extend(state.game_args)``.
    """
    parsed = parse_launch_options(
        "UNIFIDECK_UBISOFT_ACTION=auth mangohud gamemoderun",
    )
    assert parsed.env_overrides == {"UNIFIDECK_UBISOFT_ACTION": "auth"}
    assert not hasattr(parsed, "wrappers")
    assert parsed.game_args == ["mangohud", "gamemoderun"]


def test_a_command_marker_no_longer_collects_wrapper_words() -> None:
    """Tokens before ``%command%`` are dropped, deliberately.

    They used to land in ``ParsedOptions.wrappers``, which six argv builders
    prepended — and which could only ever be empty in production, because
    Steam applies wrapper words pre-exec and this parser only ever sees the
    post-expansion argv tail. The field is gone (register item 23b); what
    survives is the half that is real, the game arguments after the marker.
    """
    parsed = parse_launch_options("mangohud %command% -windowed --skip-intro")
    assert not hasattr(parsed, "wrappers")
    assert parsed.game_args == ["-windowed", "--skip-intro"]


def test_runtime_state_no_longer_reads_started_at(tmp_path) -> None:
    """``started_at`` came off an always-empty dict and nothing read it.

    Elapsed time comes from ``LauncherService._launch_started_at``. Now that
    ``env_overrides`` carries user input, reading an internal timestamp out of
    it would be a trap.
    """
    ctx = _ctx("", tmp_path)
    assert LauncherService._build_runtime_state(ctx).started_at == 0.0
    assert isinstance(LauncherService._build_runtime_state(ctx), RuntimeState)


# ========================================================= #
# 6. The argv shapes Steam actually delivers
# ========================================================= #
# Measured on a Steam Deck, 2026-08-25, with a logging script as a non-Steam
# shortcut's Exe driven through SteamClient.Apps.SetShortcutLaunchOptions +
# RunGame. Full table in docs/architecture-audit.md §2.9. These are the real
# argv tails, so the parser is pinned against observation rather than against
# what the parser's own author assumed Steam would send.
@pytest.mark.parametrize(
    ("launch_options", "argv_tail", "expected_env", "expected_game_args"),
    [
        # Steam exports assignments before %command% and strips them from argv,
        # so nothing reaches the parser at all.
        ("PROBE_ENV_A=1 %command%", "", {}, []),
        # A token after %command% arrives as argv.
        ("PROBE_ENV_A=9 %command% tail1", "tail1", {}, ["tail1"]),
        # An assignment AFTER the game key is NOT exported by Steam. It arrives
        # as argv, and the launcher is the only thing that can apply it. This
        # row is the whole justification for wiring the env half.
        ("epic:Salt PROBE_ENV_B=2", "PROBE_ENV_B=2", {"PROBE_ENV_B": "2"}, []),
        # The frontend's own temp-shortcut shape. Steam delivers the wrapper
        # words in the tail, where they are inert -- and where feeding
        # game_args from them would pass them to the game.
        (
            "ubisoft:upc-auth UNIFIDECK_UBISOFT_ACTION=auth mangohud gamemoderun",
            "UNIFIDECK_UBISOFT_ACTION=auth mangohud gamemoderun",
            {"UNIFIDECK_UBISOFT_ACTION": "auth"},
            ["mangohud", "gamemoderun"],
        ),
    ],
)
def test_parser_against_measured_argv_tails(
    launch_options: str,
    argv_tail: str,
    expected_env: dict[str, str],
    expected_game_args: list[str],
) -> None:
    """What the parser makes of each real argv tail.

    ``launch_options`` is documentation: it is what the user typed. ``argv_tail``
    is what Steam actually handed the launcher, which is the parser's input.
    """
    parsed = parse_launch_options(argv_tail)
    assert parsed.env_overrides == expected_env
    assert parsed.game_args == expected_game_args
    # There is no ``wrappers`` field any more: Steam performs wrapping
    # itself, before the launcher exists, so the field could only ever be
    # empty. Deleted in register item 23b; asserted as absence so it cannot
    # quietly come back. See audit §2.9 finding 2.
    assert not hasattr(parsed, "wrappers")


# ========================================================= #
# 7. argv element boundaries survive the join
# ========================================================= #
# Found by launching a real game with `MY_QUOTED="alpha beta"` and reading the
# game's own environment: it received `MY_QUOTED=alpha`. Steam consumes the
# quotes when it splits LaunchOptions into argv, so the value arrives as one
# element `MY_QUOTED=alpha beta`; `parse_argv` then joined the tail on spaces,
# destroying the boundary, and the reparse split it back apart. `shlex.join`
# makes that round trip lossless.
@pytest.mark.parametrize(("tail", "expected_env"), [
    # The measured failure.
    (["MY_QUOTED=alpha beta", "TAIL=z"], {"MY_QUOTED": "alpha beta", "TAIL": "z"}),
    # A path with a space, the realistic version of the same thing.
    (
        ["WINEDLLOVERRIDES=a=b,c=d", "MY_DIR=/run/media/My Card/x"],
        {"WINEDLLOVERRIDES": "a=b,c=d", "MY_DIR": "/run/media/My Card/x"},
    ),
    # Single element, no spaces: must be untouched. No ``ENABLE_LSFG`` here --
    # with no ~/lsfg script on disk the overlay is correctly empty, so this
    # also pins that lsfg-vk not being installed enables nothing.
    (["LSFG=1"], {"LSFG": "1"}),
    # Nothing at all: the common case.
    ([], {}),
])
def test_argv_element_boundaries_survive(
    tail: list[str], expected_env: dict[str, str], monkeypatch, tmp_path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))  # no ~/lsfg script in play
    _, raw = parse_argv(["launcher", "gog:1207658924", *tail])
    got = env_overrides_from(raw)
    for key, value in expected_env.items():
        assert got.get(key) == value, f"{key}: {got.get(key)!r} != {value!r}"


def test_raw_options_round_trips_through_the_tokenizer(monkeypatch, tmp_path) -> None:
    """The invariant behind the fix: join then split returns the input.

    Every consumer of ``raw_options`` splits it with ``shlex``, so this
    property is what makes the joined string a faithful stand-in for the argv
    tail rather than a lossy rendering of it.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    for tail in (
        ["A=1"],
        ["A=x y"],
        ['A=has "inner" quotes'],
        ["~/lsfg"],
        ["A=1", "B=two words", "C=3"],
        [],
    ):
        _, raw = parse_argv(["launcher", "gog:1", *tail])
        assert tokenize_options(raw) == tail, f"round trip lost {tail!r}"
