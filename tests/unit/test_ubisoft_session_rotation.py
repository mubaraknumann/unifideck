"""Signing into one Ubisoft game must not sign the user out of another.

GH #435, reported on a ROG Ally X against Avatar and Star Wars Outlaws:

    Avatar login    → Avatar relaunch  = still signed in
    Outlaws login   → Outlaws relaunch = still signed in
    Avatar relaunch → signed out

The cause was a heuristic, not the crypto the reporter suspected. Ubisoft
retires the previous refresh token on every sign-in, so with a prefix per game
the session is a baton that has to be passed. The capture and propagate paths
both guarded that handoff by comparing ``ConnectSecureStorage.dat`` *sizes* —
"smaller means logged out". A rotated vault is routinely a few hundred bytes
smaller than the one before it, so the first rotation latched the auth prefix
on a token the server had already retired, and it could never be updated again:
the largest vault ever written won permanently. Every prefix then kept whatever
token it had last minted itself, and each new sign-in killed the others.

The reporter's own measurements are the signature: canonical ``.upc-auth`` sat
at 9867 bytes and never moved while the game prefixes sat at 8726 — and copying
that 9867-byte file into a game prefix bit-for-bit was still rejected, because
it was simply a dead token.

These tests drive the REAL ``UbisoftSession`` over real files against a fake
Ubisoft that rotates tokens, and assert the reporter's acceptance criteria.
``sizes`` is a knob rather than a constant so the shrinking case — the one that
used to fail — is pinned explicitly.
"""
from __future__ import annotations

import dataclasses
import time
from pathlib import Path

import pytest

from unifideck.stores.ubisoft.config import UbisoftConfig
from unifideck.stores.ubisoft.paths import UbisoftPrefixPaths
from unifideck.stores.ubisoft.session import UbisoftSession

MACHINE_GUID = "684264b1-fd38-45a0-a8c1-bcc6da53b19f"
UPC_SUBDIR = Path("AppData/Local/Ubisoft Game Launcher")

# Vault shapes. The signed-out one is smaller than the signed-in ones, and the
# in-game sign-in is smaller than the original auth sign-in — that ordering is
# the whole bug.
AUTH_LOGIN_SIZE = 9867
GAME_LOGIN_SIZE = 8726
LOGGED_OUT_SIZE = 4000


class FakeUbisoft:
    """Only the most recently minted token is accepted."""

    def __init__(self) -> None:
        self.live: str | None = None
        self._minted = 0

    def mint(self) -> str:
        self._minted += 1
        self.live = f"token-{self._minted}"
        return self.live

    def accepts(self, token: str | None) -> bool:
        return token is not None and token == self.live


def _vault(prefix: Path) -> Path:
    return prefix / "drive_c/users/steamuser" / UPC_SUBDIR / "ConnectSecureStorage.dat"


def _account_file(prefix: Path) -> Path:
    return _vault(prefix).parent / "user.dat"


def _write_vault(prefix: Path, token: str | None, size: int) -> None:
    """What UPC actually leaves behind, as measured on-device 2026-09-05.

    Sign-out strips the ``RememberMeTicket`` entry from inside the vault and
    rewrites it smaller. It does **not** remove ``user.dat`` — that file is
    byte-identical in the template, the auth prefix and every cloned game
    prefix, and survives a sign-out untouched.

    This fake used to delete ``user.dat`` on sign-out, which is what let the
    "signed in iff ``user.dat`` exists" guard pass its tests while being inert
    on a real device: no cloned prefix ever lacks the file, so the guard never
    fired and a signed-out prefix was captured over the auth prefix.
    """
    vault = _vault(prefix)
    vault.parent.mkdir(parents=True, exist_ok=True)
    head = f"{token or 'SIGNED_OUT'}\n".encode()
    marker = b"RememberMeTicket\n" if token else b""
    body = head + marker
    vault.write_bytes(body + b"x" * max(0, size - len(body)))
    # Always present, signed in or out — the template clone puts it there.
    _account_file(prefix).write_bytes(b"account-blob" * 40)
    # mtime is the ordering the propagation guard now uses, and these writes
    # land inside the same filesystem timestamp tick otherwise.
    time.sleep(0.01)


def _read_token(prefix: Path) -> str | None:
    vault = _vault(prefix)
    if not vault.is_file():
        return None
    token = vault.read_bytes().split(b"\n", 1)[0].decode()
    return None if token == "SIGNED_OUT" else token


def _make_prefix(prefix: Path) -> None:
    (prefix / "drive_c/users/steamuser" / UPC_SUBDIR).mkdir(parents=True)
    (prefix / "system.reg").write_text(
        f'[Software\\\\Microsoft\\\\Cryptography] 1\n"MachineGuid"="{MACHINE_GUID}"\n',
    )


@dataclasses.dataclass
class Rig:
    """A signed-in installation with two games, and the fake Ubisoft."""

    session: UbisoftSession
    server: FakeUbisoft
    auth: Path
    avatar: Path
    outlaws: Path

    def play(self, prefix: Path, *, backend_capture: bool = True) -> str:
        """One Play cycle. Returns "ok" or "sign-in"."""
        # Launcher seeds the live session before UPC reads it.
        self.session.inject_into_prefix(str(prefix))
        if self.server.accepts(_read_token(prefix)):
            outcome = "ok"
            _write_vault(prefix, self.server.mint(), GAME_LOGIN_SIZE)
        else:
            outcome = "sign-in"
            _write_vault(prefix, None, LOGGED_OUT_SIZE)  # UPC clears the vault
            _write_vault(prefix, self.server.mint(), GAME_LOGIN_SIZE)
        if backend_capture and self.session.capture(str(prefix)):
            self.session.propagate_all_to_all()
        return outcome


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
    avatar = Path(config.prefixes_dir_expanded) / "avatar"
    outlaws = Path(config.prefixes_dir_expanded) / "outlaws"
    for prefix in (auth, Path(config.template_dir_expanded), avatar, outlaws):
        _make_prefix(prefix)

    server = FakeUbisoft()
    # The user signs in once, in the auth prefix (QAM → Ubisoft → Sign in).
    _write_vault(auth, server.mint(), AUTH_LOGIN_SIZE)
    session.capture(str(auth))
    session.propagate_all_to_all()
    return Rig(session, server, auth, avatar, outlaws)


def test_switching_between_games_keeps_the_session(rig: Rig):
    """The reporter's acceptance criteria, start to finish."""
    assert rig.play(rig.avatar) == "ok"
    assert rig.play(rig.avatar) == "ok"
    assert rig.play(rig.outlaws) == "ok"
    assert rig.play(rig.outlaws) == "ok"
    # The step that failed: back to the first game after the second one ran.
    assert rig.play(rig.avatar) == "ok"


def test_canonical_session_keeps_up_with_rotation(rig: Rig):
    """The auth prefix must track the live token, not the biggest vault.

    The direct observable from the report: canonical was frozen at 9867 bytes
    across many sign-ins.
    """
    rig.play(rig.avatar)

    assert _read_token(rig.auth) == rig.server.live
    assert _vault(rig.auth).stat().st_size == GAME_LOGIN_SIZE  # shrank, and did


def test_handoff_survives_a_missed_game_stopped_event(rig: Rig):
    """The backend capture rides on a frontend event that can go missing.

    With ``GAME_STOPPED`` never delivered, the launcher's own post-run capture
    is the only thing keeping the baton moving — so the sequence must still
    hold. (Modelled here as the seed on the next launch picking the newest
    source; the launcher capture is exercised in the handler tests.)
    """
    assert rig.play(rig.avatar, backend_capture=False) == "ok"
    assert rig.play(rig.avatar, backend_capture=False) == "ok"


def test_a_real_sign_out_still_does_not_propagate(rig: Rig):
    """The incident the old size guard existed for must stay fixed.

    A prefix the user signed OUT of has no ``user.dat``. Capturing from it
    would carry the sign-out into the auth prefix and from there into every
    other game.
    """
    rig.play(rig.avatar)
    live_token = _read_token(rig.auth)
    _write_vault(rig.avatar, None, LOGGED_OUT_SIZE)  # user signs out in-game

    assert rig.session.capture(str(rig.avatar)) is None
    assert _read_token(rig.auth) == live_token
    assert _account_file(rig.auth).is_file()


def test_a_newer_token_flows_even_when_the_vault_shrinks(rig: Rig):
    """The propagation guard orders by time, never by size.

    This is the copy-in half of the same bug: even once the auth prefix held
    the newer token, the fan-out refused to hand it to a prefix whose own
    (dead, larger) vault was bigger.
    """
    rig.play(rig.avatar)
    outlaws_size_before = _vault(rig.outlaws).stat().st_size

    assert _read_token(rig.outlaws) == rig.server.live
    assert _vault(rig.outlaws).stat().st_size < AUTH_LOGIN_SIZE
    assert outlaws_size_before == GAME_LOGIN_SIZE
