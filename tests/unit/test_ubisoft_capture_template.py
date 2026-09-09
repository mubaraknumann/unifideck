"""Capture-back must keep the `.template` prefix pristine.

The `.template` is a golden image that may change ONLY on an explicit sign-in
(auth→template hydration) or sign-out (purge). A game/launch/uninstall capture
must never write it, and a logged-out (shrunken) source must never propagate
anywhere. These pin `UbisoftSession.capture()`'s target selection + logout guard.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from unifideck.stores.ubisoft.session.facade import UbisoftSession


def _session(tmp_path: Path):
    auth = tmp_path / ".upc-auth"
    auth.mkdir()
    template = tmp_path / ".template"
    template.mkdir()
    s = UbisoftSession.__new__(UbisoftSession)
    s._config = SimpleNamespace(
        auth_prefix_dir_expanded=str(auth),
        template_dir_expanded=str(template),
    )
    s._reader = MagicMock()
    s._payload = MagicMock()
    s._reader.has_valid_credentials.return_value = True
    s._reader.get_credential_mtime.return_value = 100.0
    s._reader.is_signed_in.return_value = True  # signed in everywhere
    s._read_stored_mtime = lambda: 0.0  # type: ignore[method-assign]
    s._write_stored_mtime = lambda _m: None  # type: ignore[method-assign]
    s._serialised = _no_lock  # type: ignore[method-assign]
    return s, auth, template


@contextmanager
def _no_lock():
    """Stand in for the cross-process flock — nothing to serialise here."""
    yield True


def _synced_targets(s) -> list[str]:
    return [c.args[1] for c in s._payload.sync_credentials_to_prefix.call_args_list]


def test_game_source_targets_auth_only_never_template(tmp_path: Path):
    s, auth, template = _session(tmp_path)
    game = tmp_path / "game"
    game.mkdir()

    s.capture(str(game))

    targets = _synced_targets(s)
    assert str(auth) in targets
    assert str(template) not in targets  # the whole point


def test_auth_source_hydrates_template(tmp_path: Path):
    s, auth, template = _session(tmp_path)

    s.capture(str(auth))  # the sign-in path

    targets = _synced_targets(s)
    assert str(template) in targets       # auth→template sign-in hydration
    assert str(auth) not in targets       # auth→auth self-skips


def test_logged_out_source_is_skipped_entirely(tmp_path: Path):
    s, _auth, _template = _session(tmp_path)
    game = tmp_path / "game"
    game.mkdir()
    # The game prefix holds no account (no ``user.dat``) → signed out. Note
    # this is a question about the SOURCE alone: it used to be decided by
    # comparing sizes against the auth prefix, which froze auth on the first
    # token it saw once a rotation shrank the vault (GH #435).
    s._reader.is_signed_in.side_effect = lambda p: str(p) != str(game)

    result = s.capture(str(game))

    assert result is None
    s._payload.sync_credentials_to_prefix.assert_not_called()
    s._payload.sync_auth_artifacts_to_prefix.assert_not_called()
