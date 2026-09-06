"""Unit tests for the widened stale-shortcut sweep.

The beta-tester bug: shortcuts for a store that returns no games this
sync (logged-out Ubisoft, the legacy ``microsoft:ms-auth`` row) never
got swept, because the sweep only touched stores present in the synced
library. The fix lets the post-sync reconcile pass the full set of
*registered* stores as ``valid_stores`` so those orphans self-heal.

The UD-006 follow-up: the sweep decided ownership from ``LaunchOptions``
/ tags alone and never checked the ``Exe`` field, so a foreign shortcut
(NonSteamLaunchers', or a manually-added one) whose LaunchOptions merely
contained a ``"<store>:<id>"``-shaped token — or our stale
``UNIFIDECK_TAG`` — got deleted. The fix gates deletion on the launcher
``Exe`` (basename ``unifideck-launcher``); the tests below pin both that
genuine Unifideck phantoms stay sweepable and foreign shortcuts survive.

These tests exercise the decision function
``stale_predicate.is_stale_managed_shortcut`` directly.
"""
from __future__ import annotations

from unifideck.services.shortcut.games_map import UNIFIDECK_TAG
from unifideck.services.shortcut.stale_predicate import (
    is_stale_managed_shortcut,
)

_is_stale = is_stale_managed_shortcut

# The launcher path the running plugin resolves to; ownership is a
# basename match on ``unifideck-launcher``, so the exact dir is irrelevant.
_LAUNCHER = "/home/deck/homebrew/plugins/Unifideck/bin/unifideck-launcher"


def _managed(launch: str, appid: int) -> dict:
    """A genuine Unifideck shortcut: launcher ``Exe`` + store:id token."""
    return {
        "appid": appid,
        "Exe": f'"{_LAUNCHER}"',
        "LaunchOptions": launch,
        "tags": {"0": UNIFIDECK_TAG, "1": launch.split(":", 1)[0]},
    }


def test_phantom_ubisoft_swept_when_store_registered():
    """A registered-but-empty store's orphan shortcut is stale."""
    entry = _managed("ubisoft:123", appid=999)
    # ubisoft returned no games (not in valid_app_ids) but IS registered
    assert _is_stale(
        entry, valid_app_ids=set(), valid_stores={"ubisoft", "epic"},
        launcher_path=_LAUNCHER,
    )


def test_legacy_ms_auth_swept_when_microsoft_registered():
    """The legacy persistent microsoft:ms-auth row is sweepable."""
    entry = _managed("microsoft:ms-auth", appid=555)
    assert _is_stale(
        entry, valid_app_ids=set(), valid_stores={"microsoft"},
        launcher_path=_LAUNCHER,
    )


def test_orphan_preserved_when_store_not_in_valid_stores():
    """Narrow valid_stores leaves other stores' shortcuts alone.

    This is the pre-fix behaviour and the reason the widening matters:
    with valid_stores={"epic"} the ubisoft orphan is NOT swept.
    """
    entry = _managed("ubisoft:123", appid=999)
    assert not _is_stale(
        entry, valid_app_ids=set(), valid_stores={"epic"},
        launcher_path=_LAUNCHER,
    )


def test_protected_auth_shortcut_never_swept():
    """Protected auth ids survive even with their store registered."""
    entry = _managed("epic:epic-auth", appid=777)
    assert not _is_stale(
        entry, valid_app_ids=set(), valid_stores={"epic", "ubisoft"},
        launcher_path=_LAUNCHER,
    )


def test_live_game_not_swept():
    """A shortcut whose appid is still valid is kept."""
    entry = _managed("ubisoft:123", appid=999)
    assert not _is_stale(
        entry, valid_app_ids={999}, valid_stores={"ubisoft"},
        launcher_path=_LAUNCHER,
    )


def test_non_managed_shortcut_ignored():
    """A user's own shortcut (no Unifideck markers) is never swept."""
    entry = {"appid": 42, "LaunchOptions": "", "tags": {}}
    assert not _is_stale(
        entry, valid_app_ids=set(), valid_stores={"ubisoft"},
        launcher_path=_LAUNCHER,
    )


# ── UD-006 regression: foreign / manual shortcuts must never be swept ──

def test_foreign_shortcut_with_matching_launchoptions_never_swept():
    """A non-Unifideck shortcut whose LaunchOptions matches the store
    regex is NOT swept — its Exe is not our launcher (UD-006)."""
    entry = {
        "appid": 999,
        # A foreign launcher (e.g. NonSteamLaunchers) whose options
        # happen to carry an ``epic:...`` token.
        "Exe": '"/home/deck/.local/share/NonSteamLaunchers/nsl.sh"',
        "LaunchOptions": "epic:123",
        "tags": {},
    }
    assert not _is_stale(
        entry, valid_app_ids=set(), valid_stores={"epic", "ubisoft"},
        launcher_path=_LAUNCHER,
    )


def test_manual_shortcut_no_exe_never_swept():
    """A manually-added shortcut with an empty Exe is never swept."""
    entry = {
        "appid": 999,
        "Exe": "",
        "LaunchOptions": "gog:456",
        "tags": {},
    }
    assert not _is_stale(
        entry, valid_app_ids=set(), valid_stores={"gog"},
        launcher_path=_LAUNCHER,
    )


def test_foreign_shortcut_with_stale_unifideck_tag_never_swept():
    """Even a stale ``UNIFIDECK_TAG`` can't authorise deleting a foreign
    shortcut — Steam can leave the tag on a shortcut we no longer own."""
    entry = {
        "appid": 999,
        "Exe": '"/opt/other-launcher/run"',
        "LaunchOptions": "amazon:abc",
        "tags": {"0": UNIFIDECK_TAG},
    }
    assert not _is_stale(
        entry, valid_app_ids=set(), valid_stores={"amazon"},
        launcher_path=_LAUNCHER,
    )


def test_launcher_basename_match_survives_different_install_dir():
    """Ownership is a basename match: a shortcut written on another
    machine (different plugin dir) is still ours and sweepable."""
    entry = {
        "appid": 999,
        "Exe": '"/home/reboot/homebrew/plugins/Unifideck/bin/unifideck-launcher"',
        "LaunchOptions": "ubisoft:123",
        "tags": {"0": UNIFIDECK_TAG, "1": "ubisoft"},
    }
    # Even with an empty launcher_path, the frozen basename set matches.
    assert _is_stale(
        entry, valid_app_ids=set(), valid_stores={"ubisoft"},
        launcher_path="",
    )
