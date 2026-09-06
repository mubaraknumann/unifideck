"""Post-play UPC capture: budget, sign-out guard, and durability.

Three defects found together on 2026-09-05, all from the cross-process
session-locking change. The user's report was "lost UPC credentials after
switching to a different Proton before launching an install"; the Proton
switch was a coincidence, and the real chain was:

1. ``_capture_upc_session_on_stop`` waits up to ``_EXIT_WAIT_SECONDS`` (20s)
   for UPC to exit and up to ``_ACQUIRE_TIMEOUT_SECONDS`` (10s) for the
   session lock, inside the bus watchdog's 5s default. Two of four
   consecutive post-play captures were cancelled at 4.998s and 4.965s.
2. Each cancellation silently dropped a rotated refresh token, so
   ``.upc-auth`` kept one Ubisoft had already retired. Nothing retried and
   nothing noticed.
3. Nine minutes later a fresh install seeded that dead token into a new
   prefix, UPC signed itself out, and the capture guard — which decided
   "signed out" by the ABSENCE of ``user.dat``, a file every template clone
   carries — failed to stop the signed-out vault being captured into
   ``.upc-auth`` and fanned out over every other prefix.
"""
from __future__ import annotations

import dataclasses
import time
from pathlib import Path

import pytest

from unifideck.event_bus.supervision.watchdog_handler import (
    DEFAULT_HANDLER_TIMEOUT_SEC,
)
from unifideck.stores.shared.wrapper_session_hooks import _EXIT_WAIT_SECONDS
from unifideck.stores.ubisoft.config import UbisoftConfig
from unifideck.stores.ubisoft.paths import UbisoftPrefixPaths
from unifideck.stores.ubisoft.session import UbisoftSession
from unifideck.stores.ubisoft.session.lock import _ACQUIRE_TIMEOUT_SECONDS
from unifideck.stores.ubisoft.post_play_capture import (
    _CAPTURE_SLACK_SECONDS,
    _CAPTURE_TIMEOUT_SECONDS,
)

MACHINE_GUID = "684264b1-fd38-45a0-a8c1-bcc6da53b19f"
UPC_SUBDIR = Path("AppData/Local/Ubisoft Game Launcher")


# ── 1. the deadline inversion ─────────────────────────────────────


def test_capture_budget_exceeds_the_waits_it_performs():
    """The handler's budget must cover its own bounded waits.

    This is the defect stated as an invariant: lengthening either wait
    without raising the budget puts the handler back under a deadline it
    cannot meet, and the failure mode is a silently lost login rather than
    anything that looks like a timeout to the user.
    """
    assert _CAPTURE_TIMEOUT_SECONDS > _EXIT_WAIT_SECONDS + _ACQUIRE_TIMEOUT_SECONDS
    # And with room left for the copies and the fan-out that follow them.
    assert (
        _CAPTURE_TIMEOUT_SECONDS
        - (_EXIT_WAIT_SECONDS + _ACQUIRE_TIMEOUT_SECONDS)
        >= _CAPTURE_SLACK_SECONDS
    )


def test_capture_budget_is_declared_on_the_handler():
    """A budget that isn't forwarded to the watchdog is inert.

    ``@subscribe(timeout=...)`` used to reach only the introspection-only
    registry, so a declared override changed nothing. Assert the decorator
    actually stamped it.
    """
    from unifideck.stores.ubisoft.store import UbisoftStore

    meta = UbisoftStore._capture_upc_session_on_stop.__subscribe_meta__
    assert meta.timeout == _CAPTURE_TIMEOUT_SECONDS
    assert meta.timeout > DEFAULT_HANDLER_TIMEOUT_SEC


# ── 2 & 3. the sign-out guard and durability, over real files ─────


def _vault(prefix: Path) -> Path:
    return prefix / "drive_c/users/steamuser" / UPC_SUBDIR / "ConnectSecureStorage.dat"


def _write_vault(prefix: Path, *, signed_in: bool, tag: str = "t") -> None:
    """Mirror what UPC writes; see test_ubisoft_session_rotation for why.

    ``user.dat`` is written either way — that is the measured behaviour the
    old guard assumed away.
    """
    vault = _vault(prefix)
    vault.parent.mkdir(parents=True, exist_ok=True)
    body = f"{tag}\n".encode()
    if signed_in:
        body += b"RememberMeTicket\nblob" * 20
    vault.write_bytes(body + b"x" * 400)
    (vault.parent / "user.dat").write_bytes(b"account-blob" * 40)
    time.sleep(0.01)


def _make_prefix(prefix: Path) -> None:
    (prefix / "drive_c/users/steamuser" / UPC_SUBDIR).mkdir(parents=True)
    (prefix / "system.reg").write_text(
        f'[Software\\\\Microsoft\\\\Cryptography] 1\n"MachineGuid"="{MACHINE_GUID}"\n',
    )


@dataclasses.dataclass
class Rig:
    session: UbisoftSession
    auth: Path
    game: Path


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    config = dataclasses.replace(
        UbisoftConfig.from_config_manager(None),
        data_dir=str(tmp_path / "data"),
        prefixes_dir=str(tmp_path / "prefixes"),
        upc_session_file=str(tmp_path / "data/upc_session"),
    )
    session = UbisoftSession(
        config, UbisoftPrefixPaths(config), lambda _p: MACHINE_GUID,
    )
    auth = Path(config.auth_prefix_dir_expanded)
    game = Path(config.prefixes_dir_expanded) / "rayman"
    for prefix in (auth, Path(config.template_dir_expanded), game):
        _make_prefix(prefix)
    _write_vault(auth, signed_in=True, tag="auth-token")
    return Rig(session, auth, game)


def test_signed_out_prefix_is_not_captured_over_auth(rig: Rig):
    """The live failure: a signed-out game prefix overwrote the auth vault.

    UPC signs itself out in a game prefix (stale token). ``user.dat`` is
    still there, so the old guard let this through.
    """
    _write_vault(rig.game, signed_in=False, tag="signed-out")
    before = _vault(rig.auth).read_bytes()

    assert rig.session.capture(str(rig.game)) is None
    assert _vault(rig.auth).read_bytes() == before


def test_signed_in_prefix_is_still_captured(rig: Rig):
    """The guard must not swing the other way and freeze auth (GH #435)."""
    _write_vault(rig.game, signed_in=True, tag="rotated")
    assert rig.session.capture(str(rig.game)) is not None
    assert b"rotated" in _vault(rig.auth).read_bytes()


def test_rotated_vault_smaller_than_auth_is_still_captured(rig: Rig):
    """Freshness is content and mtime, never size — the GH #435 ratchet."""
    _write_vault(rig.auth, signed_in=True, tag="a" * 500)
    _write_vault(rig.game, signed_in=True, tag="rotated")
    assert _vault(rig.game).stat().st_size < _vault(rig.auth).stat().st_size
    assert rig.session.capture(str(rig.game)) is not None
    assert b"rotated" in _vault(rig.auth).read_bytes()


def test_seeding_promotes_a_newer_game_vault_into_auth(rig: Rig):
    """Durability: a capture that never ran must not poison the next install.

    Exactly the reported sequence — play rotates the token into the game
    prefix, the post-play capture is cancelled, and the next install seeds
    from an auth prefix that is now behind.
    """
    _write_vault(rig.game, signed_in=True, tag="rotated-during-play")
    # ...and no capture happens (watchdog cancelled it).

    new_prefix = rig.game.parent / "brawlhalla"
    _make_prefix(new_prefix)
    rig.session.ensure_auth_state_in_prefixes([str(new_prefix)])

    assert b"rotated-during-play" in _vault(rig.auth).read_bytes()
    assert b"rotated-during-play" in _vault(new_prefix).read_bytes()


def test_seeding_ignores_a_signed_out_game_vault(rig: Rig):
    """The promotion must inherit the guard, not bypass it."""
    _write_vault(rig.game, signed_in=False, tag="signed-out")
    before = _vault(rig.auth).read_bytes()

    new_prefix = rig.game.parent / "brawlhalla"
    _make_prefix(new_prefix)
    rig.session.ensure_auth_state_in_prefixes([str(new_prefix)])

    assert _vault(rig.auth).read_bytes() == before
    assert b"auth-token" in _vault(new_prefix).read_bytes()


def test_seeding_does_not_move_auth_backwards(rig: Rig):
    """An older signed-in game vault must not displace a newer auth one."""
    _write_vault(rig.game, signed_in=True, tag="older")
    _write_vault(rig.auth, signed_in=True, tag="newer-auth")

    new_prefix = rig.game.parent / "brawlhalla"
    _make_prefix(new_prefix)
    rig.session.ensure_auth_state_in_prefixes([str(new_prefix)])

    assert b"newer-auth" in _vault(rig.auth).read_bytes()
