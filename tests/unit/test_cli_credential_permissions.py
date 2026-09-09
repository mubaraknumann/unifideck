"""Epic/Amazon store-CLI credential files must not stay world-readable.

Audit §1.4 f. legendary and nile own their own ``user.json`` and write it at
0644 with a live access token and refresh token in plaintext. On a Deck that
is readable by every other local account and by every game the user launches
(Proton maps ``$HOME`` into the prefix), while our own token files are 0600.

``security/ephemeral_creds.py`` was written to close this properly and never
wired up by anything; it is gone. What replaces it is mode-only, so these
tests pin the mode contract and — the part that actually matters — that it is
re-applied after a rotation rather than once at sign-in.
"""
from __future__ import annotations

import stat
from pathlib import Path

import pytest

from unifideck.stores.shared.cli_credentials import harden_cli_credential_file


class _Bus:
    """Records emits without needing a real EventBus.

    ``emit`` is a coroutine because ``audit_emitter._safe_emit`` schedules it
    with ``loop.create_task``, and it only does so when a loop is running —
    which is why the emit tests below are async. That is not an artefact of
    the test: ``harden_cli_credential_file`` is called from the sync
    ``_check_*_authenticated``, which is itself called from the async
    ``is_available``, so the loop is live in production too.
    """

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, event: object, **payload: object) -> None:
        self.events.append((getattr(event, "name", str(event)), dict(payload)))


def _mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


@pytest.fixture
def user_file(tmp_path: Path) -> Path:
    f = tmp_path / "user.json"
    f.write_text('{"access_token": "eg1~secret"}')
    f.chmod(0o644)
    return f


def test_world_readable_credential_file_is_tightened(user_file: Path) -> None:
    assert harden_cli_credential_file(user_file, "epic") == 1
    assert _mode(user_file) == 0o600


def test_content_is_untouched(user_file: Path) -> None:
    """chmod only — the CLI still has to be able to read its own token."""
    before = user_file.read_bytes()
    harden_cli_credential_file(user_file, "epic")
    assert user_file.read_bytes() == before


def test_already_correct_mode_is_a_noop(user_file: Path) -> None:
    user_file.chmod(0o600)
    assert harden_cli_credential_file(user_file, "epic") == 0


def test_group_only_access_still_counts_as_too_open(user_file: Path) -> None:
    user_file.chmod(0o640)
    assert harden_cli_credential_file(user_file, "epic") == 1
    assert _mode(user_file) == 0o600


def test_missing_file_is_not_an_error(tmp_path: Path) -> None:
    """The signed-out case. Must never raise — this runs on the
    store-status path, where an exception surfaces as a false 'signed out'."""
    assert harden_cli_credential_file(tmp_path / "nope.json", "amazon") == 0


def test_quarantined_corrupt_copies_are_swept(user_file: Path) -> None:
    """``nile_lock.quarantine_corrupt_user_file`` renames a broken
    ``user.json`` aside and the rename preserves 0644 — so the stale copy
    keeps the old credentials world-readable indefinitely."""
    corrupt = user_file.with_name("user.json.corrupt-1785320102")
    corrupt.write_text('{"refresh_token": "old-but-still-real"}')
    corrupt.chmod(0o644)

    assert harden_cli_credential_file(user_file, "amazon") == 2
    assert _mode(user_file) == 0o600
    assert _mode(corrupt) == 0o600


def test_reapplied_after_a_token_rotation(user_file: Path) -> None:
    """The load-bearing case. legendary/nile rewrite user.json at 0644 on
    every refresh, so a one-shot chmod at sign-in silently drifts back."""
    harden_cli_credential_file(user_file, "epic")
    assert _mode(user_file) == 0o600

    user_file.write_text('{"access_token": "eg1~rotated"}')
    user_file.chmod(0o644)  # what the CLI does on refresh

    assert harden_cli_credential_file(user_file, "epic") == 1
    assert _mode(user_file) == 0o600


async def test_emits_one_permissions_audit_event_per_change(
    user_file: Path,
) -> None:
    import asyncio

    bus = _Bus()
    harden_cli_credential_file(user_file, "epic", bus)
    await asyncio.sleep(0)  # let the fire-and-forget audit task run
    assert len(bus.events) == 1
    _, payload = bus.events[0]
    assert payload["store"] == "epic"
    assert payload["mode"] == 0o600
    assert payload["path"].endswith("user.json")


async def test_no_event_when_nothing_changed(user_file: Path) -> None:
    import asyncio

    user_file.chmod(0o600)
    bus = _Bus()
    harden_cli_credential_file(user_file, "epic", bus)
    await asyncio.sleep(0)
    assert bus.events == []


def test_unwritable_parent_does_not_raise(tmp_path: Path) -> None:
    """A file we cannot chmod is no worse than before; never propagate."""
    locked = tmp_path / "locked"
    locked.mkdir()
    target = locked / "user.json"
    target.write_text("{}")
    target.chmod(0o644)
    locked.chmod(0o500)
    try:
        # Read-only dir still permits chmod by the owner, so assert only
        # that the call is total — the point is that it never raises.
        harden_cli_credential_file(target, "epic")
    finally:
        locked.chmod(0o700)


def test_does_not_widen_an_already_narrow_file(user_file: Path) -> None:
    """0400 grants less than the target; leave it alone rather than
    'normalising' it to 0600 and handing back write access."""
    user_file.chmod(0o400)
    assert harden_cli_credential_file(user_file, "epic") == 0
    assert _mode(user_file) == 0o400


def test_target_mode_denies_group_and_other() -> None:
    """Guards the constant itself against a careless edit."""
    from unifideck.stores.shared import cli_credentials as cc

    assert not cc._TARGET_MODE & (stat.S_IRWXG | stat.S_IRWXO)
