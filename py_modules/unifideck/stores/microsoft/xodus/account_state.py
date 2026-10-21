"""stores/microsoft/xodus/account_state.py — the verified Xodus account.

``state/account.json`` (0600) remembers what the last check found::

    {"schema": 1, "gamertag": "...", "verified_at": <epoch>,
     "mismatch": false, "unverified": false}

* ``mismatch``: Xodus signed in to a different Xbox account than
  Unifideck's Microsoft sign-in. Xodus was signed out again.
* ``unverified``: one of the two gamertags could not be read. The account
  is used, and checked again within ``RECHECK_S``.

The gamertag is not secret, but the file is still private: the support
bundle records only that it exists. Blocking file I/O; call it through
``asyncio.to_thread``.
"""
from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass

from unifideck.launcher.xbox_pc.layout import (
    account_state_path,
    data_root,
    ensure_private_dir,
    read_json_dict,
    state_dir,
    write_private_json,
)

STATE_SCHEMA = 1
#: An unverified account is checked again after this long.
RECHECK_S = 12 * 3600.0


@dataclass(frozen=True)
class AccountState:
    """What the last account check found."""

    gamertag: str | None
    verified_at: float
    mismatch: bool = False
    unverified: bool = False


def read_state() -> AccountState | None:
    """The saved state, or None when missing or malformed."""
    data = read_json_dict(account_state_path())
    if data is None or data.get("schema") != STATE_SCHEMA:
        return None
    tag = data.get("gamertag")
    at = data.get("verified_at")
    return AccountState(
        gamertag=tag if isinstance(tag, str) and tag else None,
        verified_at=float(at) if isinstance(at, (int, float)) else 0.0,
        mismatch=data.get("mismatch") is True,
        unverified=data.get("unverified") is True,
    )


def write_state(state: AccountState) -> None:
    """Save ``state`` atomically (0600)."""
    ensure_private_dir(data_root())
    ensure_private_dir(state_dir())
    write_private_json(account_state_path(), {
        "schema": STATE_SCHEMA,
        "gamertag": state.gamertag,
        "verified_at": state.verified_at,
        "mismatch": state.mismatch,
        "unverified": state.unverified,
    })


def clear_state() -> None:
    """Forget the saved state (sign-out). A missing file is fine."""
    with contextlib.suppress(FileNotFoundError):
        account_state_path().unlink()


def needs_recheck(state: AccountState | None, now: float | None = None) -> bool:
    """Whether the account should be checked again now."""
    if state is None or state.mismatch:
        return state is None
    if not state.unverified:
        return False
    current = time.time() if now is None else now
    return current - state.verified_at >= RECHECK_S or current < state.verified_at
