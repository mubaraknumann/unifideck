"""Unit tests for MicrosoftStore's token validity + background refresh.

Regression: ``is_available()`` only checked whether a ``refresh_token``
was present on disk, never whether it actually still worked. A token
Microsoft had already revoked/expired (confirmed live: HTTP 400 on
refresh) still reported ``available: True`` until the next real
library sync exposed it — the QAM showed "logged in" for a session
that was already dead. ``is_available()`` now reuses the same
``refresh_if_stale`` validation ``get_library`` already did.

Also covers the new best-effort background poller
(``start_token_refresh_polling``) that exercises the token
periodically instead of purely on-demand, so a token that simply sits
unused for a while still gets refreshed.

``refresh_if_stale`` answers with a ``TokenState``. Only ``DEAD`` (Microsoft
rejected the token) may sign the user out; ``TRANSIENT`` (no network, an
outage) must keep the session. Collapsing the two signed out every Deck that
refreshed while offline.
"""
from __future__ import annotations

import asyncio

from unifideck.stores.microsoft.microsoft_store import MicrosoftStore
from unifideck.stores.microsoft.session_health import SessionHealth
from unifideck.stores.microsoft.tokens import TokenState


class _FakeConfig:
    def is_valid(self) -> bool:
        return True


class _FakeTokens:
    def __init__(
        self, *, loaded: bool = True, state: TokenState = TokenState.FRESH,
    ) -> None:
        self.loaded = loaded
        self.state = state
        self.cleared = False
        self.load_calls = 0
        self.refresh_calls = 0
        self.client_mismatch = False
        self.mismatch_notified = False
        self.last_token_error = "invalid_grant"

    async def load(self) -> bool:
        self.load_calls += 1
        return self.loaded

    async def refresh_if_stale(self) -> TokenState:
        self.refresh_calls += 1
        return self.state

    async def clear(self) -> None:
        self.cleared = True

    async def mark_mismatch_notified(self) -> None:
        self.mismatch_notified = True


class _Bus:
    def __init__(self) -> None:
        self.emitted: list[tuple[object, dict[str, object]]] = []

    async def emit(self, event: object, **payload: object) -> None:
        self.emitted.append((event, payload))


def _store(tokens: _FakeTokens, *, config_valid: bool = True) -> MicrosoftStore:
    store = MicrosoftStore.__new__(MicrosoftStore)
    store._ms_config = _FakeConfig() if config_valid else _InvalidConfig()
    store._tokens = tokens
    store._bus = _Bus()
    store._health = SessionHealth(store._bus, tokens)
    store._poll_task = None
    return store


class _InvalidConfig:
    def is_valid(self) -> bool:
        return False


# ── is_available: now validates, not just checks presence ──────────

async def test_is_available_false_when_config_invalid():
    store = _store(_FakeTokens(), config_valid=False)

    assert await store.is_available() is False


async def test_is_available_false_when_no_token_on_disk():
    tokens = _FakeTokens(loaded=False)
    store = _store(tokens)

    assert await store.is_available() is False
    assert tokens.refresh_calls == 0  # never got that far


async def test_is_available_true_when_token_present_and_fresh():
    tokens = _FakeTokens(loaded=True, state=TokenState.FRESH)
    store = _store(tokens)

    assert await store.is_available() is True
    assert tokens.cleared is False


async def test_is_available_false_and_clears_when_refresh_fails():
    """The exact regression: a present-but-dead token must report False."""
    tokens = _FakeTokens(loaded=True, state=TokenState.DEAD)
    store = _store(tokens)

    assert await store.is_available() is False
    assert tokens.cleared is True


async def test_is_available_keeps_the_session_when_offline():
    """An offline Deck stays signed in: TRANSIENT never clears."""
    tokens = _FakeTokens(loaded=True, state=TokenState.TRANSIENT)
    store = _store(tokens)

    assert await store.is_available() is True
    assert tokens.cleared is False


# ── background token-refresh poller ──────────────────────────────────

async def test_token_poll_loop_refreshes_when_signed_in(monkeypatch):
    tokens = _FakeTokens(loaded=True, state=TokenState.FRESH)
    store = _store(tokens)
    monkeypatch.setattr(store, "TOKEN_POLL_INTERVAL_SECONDS", 0.01)

    store.start_token_refresh_polling()
    await asyncio.sleep(0.05)
    await store.stop_token_refresh_polling()

    assert tokens.load_calls >= 1
    assert tokens.refresh_calls >= 1
    assert tokens.cleared is False


async def test_token_poll_loop_clears_dead_session(monkeypatch):
    tokens = _FakeTokens(loaded=True, state=TokenState.DEAD)
    store = _store(tokens)
    monkeypatch.setattr(store, "TOKEN_POLL_INTERVAL_SECONDS", 0.01)

    store.start_token_refresh_polling()
    await asyncio.sleep(0.05)
    await store.stop_token_refresh_polling()

    assert tokens.cleared is True


async def test_token_poll_loop_keeps_the_session_when_offline(monkeypatch):
    tokens = _FakeTokens(loaded=True, state=TokenState.TRANSIENT)
    store = _store(tokens)
    monkeypatch.setattr(store, "TOKEN_POLL_INTERVAL_SECONDS", 0.01)

    store.start_token_refresh_polling()
    await asyncio.sleep(0.05)
    await store.stop_token_refresh_polling()

    assert tokens.refresh_calls >= 1
    assert tokens.cleared is False


async def test_token_poll_loop_skips_refresh_when_not_signed_in(monkeypatch):
    tokens = _FakeTokens(loaded=False)
    store = _store(tokens)
    monkeypatch.setattr(store, "TOKEN_POLL_INTERVAL_SECONDS", 0.01)

    store.start_token_refresh_polling()
    await asyncio.sleep(0.05)
    await store.stop_token_refresh_polling()

    assert tokens.load_calls >= 1
    assert tokens.refresh_calls == 0


async def test_start_token_refresh_polling_is_idempotent():
    store = _store(_FakeTokens())

    store.start_token_refresh_polling()
    first_task = store._poll_task
    store.start_token_refresh_polling()

    assert store._poll_task is first_task
    await store.stop_token_refresh_polling()


async def test_stop_token_refresh_polling_without_start_is_a_noop():
    store = _store(_FakeTokens())

    await store.stop_token_refresh_polling()  # must not raise

    assert store._poll_task is None
