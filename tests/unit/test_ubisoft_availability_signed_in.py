"""Ubisoft reads as connected only when the vault holds an account.

The bug this pins, reported by a user and reproduced on-device 2026-09-05:
the QAM showed Ubisoft signed in — green icon, Sign-out button — while
clicking the storefront opened Ubisoft Connect on a login screen. The live
backend agreed with the UI and not with reality::

    check_store_status → {"store_id": "ubisoft", ..., "available": true}

against this ``.upc-auth`` vault::

    SIGNED-OUT  size=6471  .../ConnectSecureStorage.dat

``is_available`` asked ``has_valid_credentials`` — does a plausible vault FILE
exist — but UPC signs out by REWRITING that file in place, stripping the
``RememberMeTicket`` entry and leaving the file (and its ~6.4 KB of unrelated
state) behind. So the check answered "yes, a file" to the question "is the
user signed in", and kept answering it forever after a sign-out.

These tests run against real files through the real ``_CredentialReader``,
because the whole defect was a wrong question asked of real vault bytes; a
mocked reader would have passed either way.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from unifideck.stores.ubisoft.auth.facade import UbisoftAuth
from unifideck.stores.ubisoft.session.facade import UbisoftSession
from unifideck.stores.ubisoft.session.reader import _CredentialReader

# UPC's real marker (reader._SIGNED_IN_MARKER) plus filler, so the vault also
# clears the reader's minimum-size bar the way a real one does.
_FILLER = b"\x00" * 512
_SIGNED_IN = b"RememberMeTicket" + _FILLER
_SIGNED_OUT = _FILLER


def _write_vault(prefix: Path, body: bytes) -> None:
    local = (
        prefix
        / "drive_c"
        / "users"
        / "steamuser"
        / "AppData"
        / "Local"
        / "Ubisoft Game Launcher"
    )
    local.mkdir(parents=True, exist_ok=True)
    (local / "ConnectSecureStorage.dat").write_bytes(body)


def _auth_for(auth_dir: Path) -> UbisoftAuth:
    """A bare ``UbisoftAuth`` wired to a real reader over ``auth_dir``."""
    config = SimpleNamespace(
        auth_prefix_dir_expanded=str(auth_dir),
        upc_local_subdir=str(Path("AppData") / "Local" / "Ubisoft Game Launcher"),
    )
    paths = SimpleNamespace(
        iter_user_homes=lambda prefix, pfx_first=False: [
            (
                str(prefix),
                str(Path(prefix) / "drive_c" / "users" / "steamuser"),
            ),
        ],
    )
    session = UbisoftSession.__new__(UbisoftSession)
    session._config = config
    session._reader = _CredentialReader(config=config, paths=paths)

    auth = UbisoftAuth.__new__(UbisoftAuth)
    auth._config = config
    auth._session = session
    return auth


@pytest.mark.asyncio
async def test_signed_in_vault_is_available(tmp_path: Path):
    """A vault with an account attached → connected."""
    auth_dir = tmp_path / ".upc-auth"
    _write_vault(auth_dir, _SIGNED_IN)
    auth = _auth_for(auth_dir)

    assert auth.credential_state() == "signed_in"
    assert await auth.is_available() is True


@pytest.mark.asyncio
async def test_signed_out_vault_is_not_available(tmp_path: Path):
    """The reported bug: the file survives sign-out, the session does not."""
    auth_dir = tmp_path / ".upc-auth"
    _write_vault(auth_dir, _SIGNED_OUT)
    auth = _auth_for(auth_dir)

    assert auth.credential_state() == "signed_out"
    assert await auth.is_available() is False


@pytest.mark.asyncio
async def test_missing_prefix_is_absent(tmp_path: Path):
    """``logout()`` deletes the prefix — nothing to read, nothing to claim."""
    auth = _auth_for(tmp_path / ".upc-auth")

    assert auth.credential_state() == "absent"
    assert await auth.is_available() is False


@pytest.mark.asyncio
async def test_complete_auth_fails_on_a_signed_out_vault(tmp_path: Path):
    """The auth flow must not report success against a signed-out vault.

    ``complete_auth`` reports whatever ``is_available`` says, so the same
    wrong question used to let a sign-in that never happened resolve as
    connected.
    """
    auth_dir = tmp_path / ".upc-auth"
    _write_vault(auth_dir, _SIGNED_OUT)
    auth = _auth_for(auth_dir)

    result = await auth.complete_auth()

    assert result.success is False
    assert result.error == "not_authenticated"
