"""A lost Microsoft sign-in is explained once, and old-client tokens survive.

Before this, a refresh token Microsoft rejected was deleted with no event:
the QAM row just flipped to "Sign in". And nothing recorded which OAuth
client issued the saved tokens, so after the client changes every saved
sign-in would be refreshed against the wrong client and wiped.

Pinned here:

- a dead session is deleted and ``STORE_LOGOUT`` flips the store to signed
  out; no toast (Steam truncates toast text, the reason goes to the log);
- tokens from another client are kept on disk but read as signed out, and
  the warning is logged once, remembered across restarts;
- the token file records ``client_id``; a file without one is the
  pre-0.7.6 login.live.com client.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from unifideck.core.types import Events
from unifideck.security import SecureTokenStore
from unifideck.stores.microsoft.microsoft_config import MicrosoftConfig
from unifideck.stores.microsoft.session_health import SessionHealth
from unifideck.stores.microsoft.tokens.persistence import (
    UNRECORDED_CLIENT_ID,
    PersistenceMixin,
)

NEW_CLIENT = "1f907974-e22b-4810-a9de-d9647380c97e"


class _Bus:
    def __init__(self) -> None:
        self.emitted: list[tuple[Any, dict[str, Any]]] = []

    async def emit(self, event: Any, **payload: Any) -> None:
        self.emitted.append((event, payload))


class _Tokens(PersistenceMixin):
    def __init__(self, token_file: Path, client_id: str) -> None:
        self._config = MicrosoftConfig(token_file=str(token_file), client_id=client_id)
        self._secure_store = SecureTokenStore()
        self._bus = None
        self._ms_access_token = None
        self._ms_refresh_token = None
        self._token_saved_at = 0.0
        self._client_mismatch = False
        self._mismatch_notified = False
        self._loaded_payload = None


@pytest.fixture
def token_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path / ".config/unifideck/microsoft_tokens.json"


async def _save(token_file: Path, client_id: str) -> None:
    tokens = _Tokens(token_file, client_id)
    tokens._ms_access_token = "a"
    tokens._ms_refresh_token = "r"
    tokens._token_saved_at = 1.0
    assert await tokens.save()


async def _payload(token_file: Path) -> dict[str, Any]:
    tokens = _Tokens(token_file, "")
    data = await tokens._file.read(str(token_file))
    assert isinstance(data, dict)
    return data


# ── the token file records its client ────────────────────────────────

async def test_save_records_the_client(token_file: Path) -> None:
    await _save(token_file, NEW_CLIENT)
    assert (await _payload(token_file))["client_id"] == NEW_CLIENT


async def test_same_client_loads(token_file: Path) -> None:
    await _save(token_file, NEW_CLIENT)
    tokens = _Tokens(token_file, NEW_CLIENT)
    assert await tokens.load() is True
    assert tokens.client_mismatch is False


async def test_pre_076_file_is_the_legacy_client(token_file: Path) -> None:
    token_file.parent.mkdir(parents=True)
    legacy = _Tokens(token_file, UNRECORDED_CLIENT_ID)
    assert await legacy._file.write(str(token_file), {"access_token": "a", "refresh_token": "r"})
    assert await _Tokens(token_file, UNRECORDED_CLIENT_ID).load() is True
    switched = _Tokens(token_file, NEW_CLIENT)
    assert await switched.load() is False
    assert switched.client_mismatch is True


async def test_other_client_tokens_are_kept_on_disk(token_file: Path) -> None:
    await _save(token_file, UNRECORDED_CLIENT_ID)
    tokens = _Tokens(token_file, NEW_CLIENT)
    assert await tokens.load() is False
    assert token_file.exists()
    assert (await _payload(token_file))["refresh_token"] == "r"
    # Rolling the config back makes them usable again, no sign-in needed.
    assert await _Tokens(token_file, UNRECORDED_CLIENT_ID).load() is True


# ── one toast per lost sign-in ───────────────────────────────────────

async def test_client_change_is_logged_once_across_restarts(
    token_file: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    await _save(token_file, UNRECORDED_CLIENT_ID)
    bus = _Bus()
    for _ in range(2):  # two plugin starts
        tokens = _Tokens(token_file, NEW_CLIENT)
        await tokens.load()
        await SessionHealth(bus, tokens).on_client_changed()  # type: ignore[arg-type]
    assert sum("sign-in method changed" in r.message for r in caplog.records) == 1
    assert bus.emitted == []
    assert (await _payload(token_file))["notified_for_client"] == NEW_CLIENT


async def test_no_client_change_warning_when_clients_match(
    token_file: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    await _save(token_file, NEW_CLIENT)
    tokens = _Tokens(token_file, NEW_CLIENT)
    await tokens.load()
    await SessionHealth(_Bus(), tokens).on_client_changed()  # type: ignore[arg-type]
    assert not any("sign-in method changed" in r.message for r in caplog.records)


async def test_dead_session_is_cleared_and_shows_signed_out(token_file: Path) -> None:
    await _save(token_file, NEW_CLIENT)
    bus = _Bus()
    tokens = _Tokens(token_file, NEW_CLIENT)
    await tokens.load()
    await SessionHealth(bus, tokens).on_session_dead("invalid_grant")  # type: ignore[arg-type]
    assert not token_file.exists()
    assert [e for e, _ in bus.emitted] == [Events.STORE_LOGOUT]
