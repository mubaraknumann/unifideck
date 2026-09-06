"""Ubisoft's prefix clones go through ``shared/prefix_clone``, with
``--checksum`` on exactly the ones that repair an existing tree.

Audit §3.3 recorded that Ubisoft kept a private ``rsync_clone``. What it did
not record is the part that matters: the private copy had no ``checksum``
parameter at all, and ``prefix_clone.rsync_clone``'s docstring says why one
is required — *"identity files are small and are frequently rewritten within
the same second at the same length, so the quick check skips them and the
repair silently does nothing."*

Three of the five Ubisoft clones write into a tree that may already be
populated, and one of them can never delete first: the game-prefix identity
repair, because for a wrapper store the game's files live inside the prefix.
That is the clone whose whole job is replacing a diverged ``MachineGuid`` —
a fixed-length value, in a prefix that inherited its source's mtime from
``rsync -a``. Both halves of rsync's quick check can match, and then the
repair copies nothing while reporting success, leaving the DPAPI vault
undecryptable and the game opening signed out.

The fresh-clone paths deliberately stay on the default: the destination is
empty, where the quick check is both correct and faster.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from unifideck.stores.shared import prefix_clone
from unifideck.stores.ubisoft.prefix import helpers as ubi_helpers
from unifideck.stores.ubisoft.prefix import manager as ubi_manager


def _record_clones(monkeypatch: pytest.MonkeyPatch, module: Any) -> list[dict]:
    """Capture every ``rsync_clone`` call ``module`` makes."""
    calls: list[dict] = []

    async def _fake(src: Path, dst: Path, **kwargs: Any) -> bool:
        calls.append({"src": str(src), "dst": str(dst), **kwargs})
        return True

    monkeypatch.setattr(module, "rsync_clone", _fake)
    return calls


def _helpers(tmp_path: Path) -> ubi_helpers._PrefixHelpers:
    parent = MagicMock()
    parent._config.template_dir_expanded = str(tmp_path / "template")
    parent._config.bootstrap_marker = "unifideck_ubisoft_bootstrap.marker"
    return ubi_helpers._PrefixHelpers(parent)


# ── the repair paths ───────────────────────────────────────────────────────


def test_the_game_prefix_identity_repair_forces_a_content_comparison(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The one clone that can never delete its destination first.

    If this reverts to the quick check, the repair silently no-ops and the
    caller still logs success — which is exactly the shape of failure the
    user reads as "it keeps asking me to sign in".
    """
    calls = _record_clones(monkeypatch, ubi_manager)
    mgr = ubi_manager.UbisoftPrefixManager.__new__(
        ubi_manager.UbisoftPrefixManager,
    )
    mgr._config = MagicMock()
    mgr._config.template_dir_expanded = str(tmp_path / "template")
    mgr._helpers = MagicMock()
    mgr._game_prefix_needs_identity_repair = MagicMock(return_value=True)

    asyncio.run(
        mgr._reuse_existing_game_prefix("83", str(tmp_path / "83"), "/auth"),
    )

    assert len(calls) == 1
    assert calls[0]["checksum"] is True
    # The game lives inside this prefix; excluding it is the other half of
    # not eating the install.
    assert calls[0]["exclude_games"] is True


def test_the_template_refresh_from_auth_forces_a_content_comparison(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Reached from sign-in with the old template still in place.

    Only ``regenerate_template_from_auth_if_diverged`` rmtrees first; the
    sign-in path does not, so this is a repair too.
    """
    calls = _record_clones(monkeypatch, ubi_helpers)
    helpers = _helpers(tmp_path)
    (tmp_path / "auth").mkdir()

    asyncio.run(helpers.create_template_from_auth_prefix(str(tmp_path / "auth")))

    assert len(calls) == 1
    assert calls[0]["checksum"] is True


# ── the fresh paths ────────────────────────────────────────────────────────


def test_a_fresh_clone_keeps_the_quick_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """An empty destination makes the quick check correct, and faster.

    A 1.6 GB prefix is ~4600 files; forcing a content comparison here would
    buy nothing and cost real seconds on every install.
    """
    calls = _record_clones(monkeypatch, ubi_helpers)
    helpers = _helpers(tmp_path)

    asyncio.run(
        helpers.clone_prefix_from_template("83", str(tmp_path / "83")),
    )

    assert len(calls) == 1
    assert calls[0].get("checksum", False) is False


# ── the flag actually reaches rsync ────────────────────────────────────────


def test_checksum_is_passed_through_to_rsync(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Assert on the argv, not just on the keyword.

    A test that only checks the call site would still pass if the shared
    function dropped the flag on the floor.
    """
    seen: list[list[str]] = []

    async def _fake_exec(*args: str, **_kwargs: Any) -> Any:
        seen.append(list(args))
        proc = MagicMock()
        proc.returncode = 0

        async def _communicate() -> tuple[bytes, bytes]:
            return b"", b""

        proc.communicate = _communicate
        return proc

    monkeypatch.setattr(
        prefix_clone.asyncio, "create_subprocess_exec", _fake_exec,
    )

    asyncio.run(
        prefix_clone.rsync_clone(
            tmp_path / "src", tmp_path / "dst",
            exclude_games=True, checksum=True,
        ),
    )
    assert "--checksum" in seen[0]

    seen.clear()
    asyncio.run(prefix_clone.rsync_clone(tmp_path / "src", tmp_path / "dst"))
    assert "--checksum" not in seen[0]
    # Never ``--delete`` unless asked: for these stores it would remove the
    # game sitting inside the prefix.
    assert "--delete" not in seen[0]


def test_ubisoft_no_longer_owns_a_private_rsync(tmp_path: Path) -> None:
    """The duplicate is gone, not merely bypassed.

    A private copy left in place is how the two drifted apart in the first
    release — ``prefix_clone``'s header says the mechanics moved here, and
    the originals stayed.
    """
    assert not hasattr(_helpers(tmp_path), "rsync_clone")
