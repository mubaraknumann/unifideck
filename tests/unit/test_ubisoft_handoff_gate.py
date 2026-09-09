"""A post-run capture needs proof that UPC wrote the vault.

Measured on-device 2026-09-05. A game prefix whose ``ConnectSecureStorage.dat``
had been corrupted was launched. The seed correctly declined to repair it (the
target's vault was newer), UPC could not use it and exited rc=1 **without
rewriting anything**, and the post-run capture then took that untouched corrupt
file into the auth prefix and propagated it to every other Ubisoft prefix. One
bad prefix poisoned them all — the incident this whole layer exists to prevent.

Nothing local can validate a vault's *contents*: the corrupt file was a
plausible size and still had its ``user.dat``, so neither the old size
heuristic nor the signed-in shape test could see anything wrong. What can be
established is whether UPC wrote the file at all, and a capture is precisely
the claim that UPC produced a session. So the capture is gated on the vault
changing across the run.

The ``finally`` placement stays: a crashed run may still have rotated the
token. The gate is what makes that safe.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from unifideck.launcher.proton.handlers import ubisoft_handoff

VAULT_REL = Path("drive_c/users/steamuser/AppData/Local/Ubisoft Game Launcher")


@pytest.fixture
def prefix(tmp_path: Path) -> Path:
    """A prefix holding a plausible, signed-in-looking vault."""
    root = tmp_path / "game"
    (root / VAULT_REL).mkdir(parents=True)
    (root / VAULT_REL / "ConnectSecureStorage.dat").write_bytes(b"v" * 7612)
    (root / VAULT_REL / "user.dat").write_bytes(b"u" * 516)
    return root


@pytest.fixture
def session(monkeypatch) -> MagicMock:
    """Intercept the standalone session the handoff builds."""
    fake = MagicMock()
    fake.capture.return_value = "credentials_captured"
    monkeypatch.setattr(
        "unifideck.stores.ubisoft.session.build_standalone_session",
        lambda: fake,
    )
    monkeypatch.setattr(ubisoft_handoff, "_await_upc_exit", lambda _p: None)
    return fake


def test_untouched_vault_is_not_captured(prefix: Path, session: MagicMock):
    """The live incident: UPC exited without writing, so nothing is a session."""
    before = ubisoft_handoff.vault_fingerprint(prefix)

    ubisoft_handoff.capture_after_exit(prefix, before)

    session.capture.assert_not_called()
    session.propagate_all_to_all.assert_not_called()


def test_rewritten_vault_is_captured(prefix: Path, session: MagicMock):
    """UPC rotated the token — that is a real session, capture and fan out."""
    before = ubisoft_handoff.vault_fingerprint(prefix)
    vault = prefix / VAULT_REL / "ConnectSecureStorage.dat"
    vault.write_bytes(b"w" * 7000)  # UPC rewrote it (smaller is normal)

    ubisoft_handoff.capture_after_exit(prefix, before)

    session.capture.assert_called_once_with(str(prefix))
    session.propagate_all_to_all.assert_called_once()


def test_same_size_rewrite_is_still_captured(prefix: Path, session: MagicMock):
    """A rotation that keeps the byte count must not be mistaken for a no-op.

    The observed device writes a constant 7612-byte vault across rotations, so
    size alone cannot decide this — the fingerprint carries mtime too.
    """
    before = ubisoft_handoff.vault_fingerprint(prefix)
    vault = prefix / VAULT_REL / "ConnectSecureStorage.dat"
    import os
    vault.write_bytes(b"x" * 7612)
    os.utime(vault, ns=(before[1] + 10_000_000, before[1] + 10_000_000))

    ubisoft_handoff.capture_after_exit(prefix, before)

    session.capture.assert_called_once()


def test_missing_before_still_captures(prefix: Path, session: MagicMock):
    """No baseline (a prefix that had no vault) must not block a real capture."""
    ubisoft_handoff.capture_after_exit(prefix, None)

    session.capture.assert_called_once()


def test_prefix_without_a_vault_captures_nothing(tmp_path: Path, session: MagicMock):
    """Nothing to fingerprint and nothing to capture."""
    bare = tmp_path / "bare"
    bare.mkdir()

    ubisoft_handoff.capture_after_exit(bare, None)

    session.capture.assert_not_called()


def test_fingerprint_finds_the_pfx_view(tmp_path: Path):
    """Prefixes vary in layout; the fingerprint must find the vault anyway."""
    root = tmp_path / "p"
    alt = root / "pfx/drive_c/users/steamuser/AppData/Local/Ubisoft Game Launcher"
    alt.mkdir(parents=True)
    (alt / "ConnectSecureStorage.dat").write_bytes(b"z" * 100)

    fp = ubisoft_handoff.vault_fingerprint(root)

    assert fp is not None and fp[0] == 100
