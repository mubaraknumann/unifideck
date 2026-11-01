"""The one Microsoft sign-in: the ticket from the Xodus credential.

- ``parse_reply`` takes the service's ``MSA_TOKEN_RESPONSE`` body;
- ``XodusTicket`` caches until near expiry, keeps a still-valid ticket when a
  download holds the credentials, and applies only with Xbox PC on and a
  Xodus credential on disk;
- the token manager uses it instead of OAuth when it applies.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from unifideck.launcher.xbox_pc import layout
from unifideck.stores.microsoft.tokens.endpoint import TokenState
from unifideck.stores.microsoft.xodus import ticket as tk


@pytest.fixture(autouse=True)
def data_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    return tmp_path


def _reply(token: str, expires_in: float) -> bytes:
    exp = int(time.time() + expires_in)
    return f"<MSATokenResponse><Token>{token}</Token><Expiry>{exp}</Expiry><DeviceRps></DeviceRps><DeviceExpiry>0</DeviceExpiry></MSATokenResponse>".encode()


def test_parse_reply() -> None:
    parsed = tk.parse_reply(_reply("abc", 3600))
    assert parsed is not None and parsed[0] == "abc" and parsed[1] > time.time()
    assert tk.parse_reply(b"") is None, "the service's empty answer (refused client id)"
    assert tk.parse_reply(_reply("abc", -10)) is None, "already expired"
    assert tk.parse_reply(b"<oops") is None


def test_the_request_names_the_xbox_app_client_and_full_trust() -> None:
    raw = tk._request()
    magic, kind, size = tk._HEADER.unpack(raw[: tk._HEADER.size])
    body = raw[tk._HEADER.size:].decode()
    assert (magic, kind, size) == (tk.XML_MAGIC, tk.MSA_TOKEN_REQUEST, len(body))
    assert f"<ClientId>{tk.XBOX_APP_CLIENT_ID}</ClientId>" in body
    assert "<MSAFullTrust>true</MSAFullTrust>" in body


class FakeCli:
    def __init__(self) -> None:
        self.credential_lock = asyncio.Lock()


def _source(enabled: bool = True) -> tuple[tk.XodusTicket, list[tuple[str, float] | None]]:
    minted: list[tuple[str, float] | None] = []
    source = tk.XodusTicket(FakeCli(), lambda: enabled)  # type: ignore[arg-type]

    async def mint() -> tuple[str, float] | None:
        value = minted.pop(0) if minted else None
        return value

    source._mint = mint  # type: ignore[method-assign]
    return source, minted


def test_active_needs_the_toggle_and_a_xodus_credential() -> None:
    source, _ = _source()
    assert not source.active()
    cred = layout.credential_dir()
    cred.mkdir(parents=True)
    (cred / "user-session").write_text("{}")
    assert source.active()
    off, _ = _source(enabled=False)
    assert not off.active()


async def test_get_caches_and_refreshes_near_expiry() -> None:
    source, minted = _source()
    minted.append(("one", time.time() + 86400))
    assert await source.get() == "one"
    assert await source.get() == "one", "cached: no second mint"
    source._cached = ("one", time.time() + tk.REFRESH_MARGIN_S - 5)
    minted.append(("two", time.time() + 86400))
    assert await source.get() == "two"
    source.forget()
    assert await source.get() is None, "nothing to mint, nothing cached"


async def test_a_download_holding_the_credentials_keeps_the_cached_ticket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tk, "LOCK_WAIT_S", 0.05)
    source, minted = _source()
    source._cached = ("cached", time.time() + tk.REFRESH_MARGIN_S - 5)
    minted.append(("new", time.time() + 86400))
    async with source._cli.credential_lock:
        assert await source.get() == "cached"
    assert minted, "no service started while the download held the lock"


async def test_the_token_manager_uses_the_ticket_when_it_applies(tmp_path: Path) -> None:
    from unifideck.stores.microsoft.microsoft_config import MicrosoftConfig
    from unifideck.stores.microsoft.tokens import MicrosoftTokenManager

    class Store:
        async def load(self, *_: Any, **__: Any) -> None:
            return None

    tokens = MicrosoftTokenManager(MicrosoftConfig(client_id="c"), lambda: "en-US", secure_store=Store())  # type: ignore[arg-type]

    class Source:
        def __init__(self) -> None:
            self.on, self.value, self.last_error = True, "ticket", ""

        def active(self) -> bool:
            return self.on

        async def get(self) -> str | None:
            return self.value

    source = Source()
    tokens.xodus_ticket = source  # type: ignore[assignment]
    assert await tokens.load() is True and tokens.access_token == "ticket"
    assert await tokens.refresh_if_stale() is TokenState.FRESH
    source.value, source.last_error = None, "service: OSError"
    assert await tokens.load() is False and tokens.last_token_error == "service: OSError"
    assert await tokens.refresh_if_stale() is TokenState.TRANSIENT, "a ticket hiccup never signs out"
