"""Epic's legendary launcher-token auth — audit register item 47.

``EpicAchievements`` and ``EpicSessions`` each carried their own copy of this,
and ``sessions.py``'s header declared the duplication deliberate: *"Kept
self-contained rather than shared with achievements to avoid disturbing that
working path."* Measuring the copies is what overturned that. Three of the
four functions were byte-identical, but ``_refresh_token`` had drifted and
**each copy was missing a fix the other had** — so neither was the direction
to consolidate toward, and the shared version is neither of them.

These tests pin the four differences, because each is invisible in normal
operation: they only show up when the token refresh is already failing.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from unifideck.stores.epic.launcher_auth import (
    TOKEN_SKEW_SECONDS,
    LegendaryLauncherAuth,
)


class _Auth(LegendaryLauncherAuth):
    """Minimal host supplying the three attributes the mixin declares."""

    _LOG_TAG = "test.auth"

    def __init__(self, user_file: Path, cli_path: str | None = "/bin/legendary") -> None:
        self._user_file = user_file
        self._cli_path = cli_path
        self._info_timeout = 5.0


@pytest.fixture()
def user_file(tmp_path: Path) -> Path:
    return tmp_path / "user.json"


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


# ── the defect the merge fixed ──────────────────────────────────────
async def test_a_missing_legendary_binary_does_not_raise(
    user_file: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The achievements copy raised ``UnboundLocalError`` here.

    It referenced ``proc`` in its ``except`` block without binding it first,
    so when ``create_subprocess_exec`` itself raised ``FileNotFoundError``
    — a stale ``cli_path``, which the ``if not self._cli_path`` guard does
    not catch because it only checks the string is non-empty — the handler
    blew up *inside* the handler and masked the real error.
    """
    async def _boom(*_a: Any, **_k: Any) -> Any:
        raise FileNotFoundError("/bin/legendary")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)
    auth = _Auth(user_file)

    await auth._refresh_token()  # must return, not raise


async def test_a_missing_binary_degrades_to_no_credentials(
    user_file: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The caller-visible consequence: "not signed in", not a crash."""
    async def _boom(*_a: Any, **_k: Any) -> Any:
        raise FileNotFoundError("/bin/legendary")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)
    _write(user_file, {"expires_at": "2000-01-01T00:00:00Z"})

    assert await _Auth(user_file)._resolve_auth() == (None, None)


# ── the arguments each copy was missing ─────────────────────────────
@pytest.fixture()
def spawn_spy(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Capture the kwargs ``legendary status`` is spawned with."""
    seen: dict[str, Any] = {}

    async def _spawn(*argv: Any, **kwargs: Any) -> Any:
        seen["argv"] = list(argv)
        seen.update(kwargs)

        class _Proc:
            returncode = 0

            async def communicate(self) -> tuple[bytes, bytes]:
                return b"", b""

            def kill(self) -> None:
                pass

        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    return seen


async def test_the_refresh_runs_with_a_cleaned_environment(
    user_file: Path, spawn_spy: dict[str, Any],
) -> None:
    """The achievements copy passed no ``env`` at all.

    ``clean_cli_env`` exists because the plugin's environment leaks
    ``LD_LIBRARY_PATH`` and friends into bundled CLIs, which is how umu
    reached ``rc=127`` on a missing ``libz.so.1``.
    """
    await _Auth(user_file)._refresh_token()

    assert spawn_spy.get("env") is not None
    assert "LD_LIBRARY_PATH" not in spawn_spy["env"]


async def test_stdin_is_closed_for_the_child(
    user_file: Path, spawn_spy: dict[str, Any],
) -> None:
    """The sessions copy left stdin attached to whatever the parent had.

    legendary prompts on a bare ``input()`` in places ``--yes`` does not
    gate (UD-026); with a live stdin such a prompt waits instead of failing.
    """
    await _Auth(user_file)._refresh_token()

    assert spawn_spy.get("stdin") == asyncio.subprocess.DEVNULL


async def test_it_invokes_legendary_status(
    user_file: Path, spawn_spy: dict[str, Any],
) -> None:
    await _Auth(user_file)._refresh_token()

    assert spawn_spy["argv"] == ["/bin/legendary", "status"]


async def test_no_cli_path_skips_the_subprocess_entirely(
    user_file: Path, spawn_spy: dict[str, Any],
) -> None:
    await _Auth(user_file, cli_path=None)._refresh_token()

    assert "argv" not in spawn_spy


# ── expiry ──────────────────────────────────────────────────────────
def test_an_absent_expiry_counts_as_expired() -> None:
    """Unknown state provokes one refresh rather than a doomed request."""
    assert LegendaryLauncherAuth._is_expired({}) is True


def test_an_unparseable_expiry_counts_as_expired() -> None:
    assert LegendaryLauncherAuth._is_expired({"expires_at": "soon"}) is True


def test_a_token_inside_the_skew_window_is_already_expired() -> None:
    """Refresh before the wire expiry, so a request issued now still lands."""
    import time

    nearly = time.time() + (TOKEN_SKEW_SECONDS / 2)

    assert LegendaryLauncherAuth._is_expired({"expires_at": nearly}) is True


def test_a_token_well_inside_its_lifetime_is_not_expired() -> None:
    import time

    later = time.time() + (TOKEN_SKEW_SECONDS * 10)

    assert LegendaryLauncherAuth._is_expired({"expires_at": later}) is False


# ── reading user.json ───────────────────────────────────────────────
def test_a_missing_user_file_reads_as_empty(user_file: Path) -> None:
    assert _Auth(user_file)._read_user() == {}


def test_malformed_json_reads_as_empty(user_file: Path) -> None:
    user_file.write_text("{not json", encoding="utf-8")

    assert _Auth(user_file)._read_user() == {}


def test_a_json_list_reads_as_empty(user_file: Path) -> None:
    """``user.json`` must be an object; a list would break every ``.get``."""
    user_file.write_text("[1, 2]", encoding="utf-8")

    assert _Auth(user_file)._read_user() == {}


async def test_a_valid_token_is_returned_without_refreshing(
    user_file: Path, spawn_spy: dict[str, Any],
) -> None:
    import time

    _write(user_file, {
        "access_token": "tok", "account_id": "acct",
        "expires_at": time.time() + 9999,
    })

    assert await _Auth(user_file)._resolve_auth() == ("tok", "acct")
    assert "argv" not in spawn_spy, "no refresh for a live token"


async def test_force_refresh_refreshes_a_token_that_looks_valid(
    user_file: Path, spawn_spy: dict[str, Any],
) -> None:
    """Recovers from a present-but-rejected token — clock skew, revocation —
    where the local expiry says nothing is wrong."""
    import time

    _write(user_file, {
        "access_token": "tok", "account_id": "acct",
        "expires_at": time.time() + 9999,
    })

    await _Auth(user_file)._resolve_auth(force_refresh=True)

    assert spawn_spy["argv"] == ["/bin/legendary", "status"]
