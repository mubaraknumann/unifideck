"""Guard test — a user's launch options survive Force Sync.

Since 0.7.5 launch options are the supported way to enable LSFG and to set
per-game environment variables (``docs/launch-options.md``, audit §2.9). That
made a latent bug matter: ``_update_existing_shortcut`` overwrote the whole
``LaunchOptions`` field with the canonical ``"<store>:<id>"``, so a Force Sync
deleted whatever the user had configured. Force Sync is precisely what a user
is told to run when something looks wrong, so the setting could not survive
ordinary troubleshooting. ``_reclaim_orphan``, eleven lines below in the same
file, already preserved it.

The fix is not "preserve everything", and that is the interesting half. Our own
``UNIFIDECK_*`` action flags must still be dropped: the frontend writes one into
a shortcut for a single run and restores the originals afterwards, so a crash
mid-launch can strand one on a *game* shortcut, and the full overwrite is what
has always cleaned that up. Preserving it wholesale would convert a recoverable
state into a permanent one -- a tile that opens a sign-in window and never
launches the game.

What is pinned:

1. user params (``LSFG=1``, ``MANGOHUD=1``, a wrapper before ``%command%``)
   survive a force sync, and survive it repeatedly;
2. our own ``UNIFIDECK_*`` flags are still stripped, quoted values included, so
   the stranded-flag self-heal is not lost;
3. a changed ``store_game_id`` is still swapped in, without taking the user's
   params with it;
4. malformed and non-string fields fall back to the canonical id rather than
   raising on the sync path;
5. a protected auth shortcut is never rewritten into a game shortcut;
6. ``_reclaim_orphan`` behaves identically, because a half-applied fix here is
   what this audit keeps finding.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from unifideck.services.shortcut.launch_options import (
    rewrite_for_sync,
    strip_unifideck_env_tokens,
)
from unifideck.services.shortcut.reconcile_phases import _ReconcilePhasesMixin

LAUNCHER = "/home/deck/homebrew/plugins/Unifideck/bin/unifideck-launcher"


class _Host(_ReconcilePhasesMixin):
    """Minimal host exposing the two rewrite methods under test."""

    _launcher_path = LAUNCHER


@pytest.fixture
def host() -> _Host:
    return _Host()


@pytest.fixture
def game() -> Any:
    return SimpleNamespace(
        title="Salt", store="epic", store_game_id="Salt",
        installed=True, icon_url="",
    )


def _entry(launch_options: Any, appid: int = 1234) -> dict[str, Any]:
    return {
        "appid": appid,
        "AppName": "stale name",
        "Exe": '"/old/launcher"',
        "LaunchOptions": launch_options,
        "tags": {},
    }


# ========================================================= #
# 1. The user's settings survive
# ========================================================= #
@pytest.mark.parametrize("options", [
    "epic:Salt LSFG=1",
    "epic:Salt ENABLE_LSFG=1",
    "epic:Salt LSFG=1 MANGOHUD=1",
    "epic:Salt WINEDLLOVERRIDES=icuuc=b",
    "mangohud %command% epic:Salt",
    "MANGOHUD=1 %command% epic:Salt",
    'epic:Salt WINEDLLOVERRIDES="icuuc=b,d3d11=n"',
])
def test_force_sync_preserves_user_launch_options(
    host: _Host, game: Any, options: str,
) -> None:
    """The regression this file exists for: these all used to become 'epic:Salt'."""
    entry = _entry(options)
    host._update_existing_shortcut(entry, game, 1234, LAUNCHER)
    assert entry["LaunchOptions"] == options


def test_force_sync_is_idempotent(host: _Host, game: Any) -> None:
    """Ten syncs must not accumulate or erode the field.

    ``preserve_user_params`` rewrites around a regex match, so a duplicated
    id or a slowly-truncating string would show up here and nowhere else.
    """
    entry = _entry("epic:Salt LSFG=1 MANGOHUD=1")
    for _ in range(10):
        host._update_existing_shortcut(entry, game, 1234, LAUNCHER)
    assert entry["LaunchOptions"] == "epic:Salt LSFG=1 MANGOHUD=1"


def test_force_sync_still_updates_the_rest_of_the_entry(
    host: _Host, game: Any,
) -> None:
    """Preserving options must not stop the fields force sync exists to fix."""
    entry = _entry("epic:Salt LSFG=1")
    host._update_existing_shortcut(entry, game, 1234, LAUNCHER)

    assert entry["AppName"] == "Salt"
    assert entry["Exe"] == f'"{LAUNCHER}"'
    assert entry["appid"] == 1234, "appid must survive so artwork does"
    assert entry["tags"]["1"] == "epic"


# ========================================================= #
# 2. Our own flags are still stripped
# ========================================================= #
@pytest.mark.parametrize(("before", "expected"), [
    ("epic:Salt UNIFIDECK_EPIC_ACTION=auth", "epic:Salt"),
    ("epic:Salt UNIFIDECK_EPIC_ACTION=auth LSFG=1", "epic:Salt LSFG=1"),
    ("epic:Salt UNIFIDECK_EPIC_ACTION=install", "epic:Salt"),
    ('epic:Salt UNIFIDECK_X_NAME="a b" LSFG=1', "epic:Salt LSFG=1"),
    (
        "epic:Salt UNIFIDECK_A=1 UNIFIDECK_B=2 MANGOHUD=1",
        "epic:Salt MANGOHUD=1",
    ),
])
def test_stranded_action_flags_are_dropped(
    host: _Host, game: Any, before: str, expected: str,
) -> None:
    """The self-heal that the full overwrite used to provide, kept.

    A stranded ``UNIFIDECK_*_ACTION`` makes a game tile open a sign-in window
    instead of launching. Preserving it would make that permanent.
    """
    entry = _entry(before)
    host._update_existing_shortcut(entry, game, 1234, LAUNCHER)
    assert entry["LaunchOptions"] == expected


def test_strip_leaves_a_lookalike_user_variable_alone() -> None:
    """Only our exact prefix is ours. ``UNIFIDECKISH`` is the user's."""
    assert strip_unifideck_env_tokens("epic:Salt UNIFIDECKISH=1") == (
        "epic:Salt UNIFIDECKISH=1"
    )
    assert strip_unifideck_env_tokens("epic:Salt MY_UNIFIDECK_VAR=1") == (
        "epic:Salt MY_UNIFIDECK_VAR=1"
    )


def test_strip_is_whitespace_clean() -> None:
    """A removed token must not leave a double space or a trailing one."""
    assert strip_unifideck_env_tokens(
        "epic:Salt UNIFIDECK_A=1 LSFG=1",
    ) == "epic:Salt LSFG=1"
    assert strip_unifideck_env_tokens("epic:Salt UNIFIDECK_A=1") == "epic:Salt"
    assert strip_unifideck_env_tokens("UNIFIDECK_A=1 epic:Salt") == "epic:Salt"


# ========================================================= #
# 3. A changed id still gets swapped in
# ========================================================= #
def test_a_changed_store_game_id_is_swapped_without_losing_params(
    host: _Host, game: Any,
) -> None:
    entry = _entry("epic:OldId LSFG=1 MANGOHUD=1")
    host._update_existing_shortcut(entry, game, 1234, LAUNCHER)
    assert entry["LaunchOptions"] == "epic:Salt LSFG=1 MANGOHUD=1"


# ========================================================= #
# 4. Degenerate input falls back, never raises
# ========================================================= #
@pytest.mark.parametrize("options", ["", "   ", "garbage tokens", None, 123, [], {}])
def test_unusable_options_fall_back_to_the_canonical_id(
    host: _Host, game: Any, options: Any,
) -> None:
    """Force sync runs over a whole library; one bad row must not abort it."""
    entry = _entry(options)
    host._update_existing_shortcut(entry, game, 1234, LAUNCHER)
    assert entry["LaunchOptions"] == "epic:Salt"


# ========================================================= #
# 5. Protected auth shortcuts are never rewritten
# ========================================================= #
@pytest.mark.parametrize("auth_options", [
    "ubisoft:upc-auth UNIFIDECK_UBISOFT_ACTION=auth "
    "UNIFIDECK_UBISOFT_PREFIX_NAME=.upc-auth",
    "battlenet:bnet-auth UNIFIDECK_BATTLENET_ACTION=auth",
    "epic:epic-auth",
    "gog:gog-auth",
    "amazon:amazon-auth",
])
def test_a_protected_auth_shortcut_is_left_completely_alone(
    host: _Host, game: Any, auth_options: str,
) -> None:
    """Reachable only by an appid collision, and unrecoverable if it happens.

    An auth forwarder *is* ours, and the appid fallback that selects the entry
    to rewrite matches on appid + ``is_ours`` without consulting the protected
    set. Rewriting one turns a sign-in tile into a game tile and the user
    cannot get back without a re-auth. Asserted on the whole entry, not just
    the options, because the old code rewrote AppName and Exe too.
    """
    entry = _entry(auth_options, appid=7)
    entry["AppName"] = "Ubisoft Connect"
    before = dict(entry)

    host._update_existing_shortcut(entry, game, 7, LAUNCHER)

    assert entry == before, "a protected shortcut was mutated"


# ========================================================= #
# 6. The orphan-reclaim path agrees
# ========================================================= #
@pytest.mark.parametrize(("before", "expected"), [
    ("epic:Salt LSFG=1", "epic:Salt LSFG=1"),
    ("epic:OldId LSFG=1", "epic:Salt LSFG=1"),
    ("epic:Salt UNIFIDECK_EPIC_ACTION=auth LSFG=1", "epic:Salt LSFG=1"),
    ("", "epic:Salt"),
])
def test_reclaim_orphan_matches_force_update(
    host: _Host, game: Any, before: str, expected: str,
) -> None:
    """Both rewrite paths, one behaviour.

    They disagreed before this change (one preserved, one overwrote), which is
    how the bug survived: whichever path a reader looked at, the other was the
    counterexample.
    """
    entry = _entry(before)
    host._reclaim_orphan(entry, game, 1234)
    assert entry["LaunchOptions"] == expected


# ── item 36: a leading %command% is repaired, not preserved ─────────
@pytest.mark.parametrize(
    ("before", "expected"),
    [
        pytest.param("%command% epic:Salt", "epic:Salt", id="bare-leading"),
        pytest.param("  %command%   epic:Salt", "epic:Salt", id="whitespace"),
        pytest.param("%command%", "epic:Salt", id="only-the-token"),
    ],
)
def test_a_leading_command_token_is_dropped(before: str, expected: str) -> None:
    """A shortcut starting with ``%command%`` does not launch at all.

    Measured on-device in audit §2.9: two attempts out of two never
    launched, while the same string with any token in front launched fine.
    The audit recorded it as "not chased further" and it had no register row.

    It is reachable through sync: ``preserve_user_params`` splices the new
    store id in and keeps the prefix verbatim, so the broken form survived a
    Force Sync unchanged. Register item 24a's fix is what made that matter —
    replacing ``_update_existing_shortcut``'s wholesale overwrite with
    preservation removed the only thing that had ever cleaned these up.
    """
    assert rewrite_for_sync(before, "epic:Salt") == expected


@pytest.mark.parametrize(
    "before",
    [
        pytest.param("mangohud %command% epic:Salt", id="wrapper-word"),
        pytest.param("gamemoderun %command% epic:Salt", id="gamemode"),
        pytest.param("VAR=1 %command% epic:Salt", id="env-assignment"),
    ],
)
def test_a_command_token_with_something_in_front_is_left_alone(
    before: str,
) -> None:
    """``%command%`` is only meaningful as a separator, and here it separates.

    Steam applies wrapper words and assignments before it pre-exec — §2.9
    measured `env %command% epic:Salt` running the launcher *under* ``env``
    and still delivering ``argv[1]``. Repairing these would break a working,
    documented user customisation.
    """
    assert rewrite_for_sync(before, "epic:Salt") == before
