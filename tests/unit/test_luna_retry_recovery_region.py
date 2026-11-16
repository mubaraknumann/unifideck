"""``LunaStore.get_library``: retry/recovery and the API status mapping.

Drives :meth:`LunaStore.get_library` through the real retry/recovery loop
(:meth:`LunaStore._with_retry` → :meth:`LunaStore._sync_once`) and a real
:class:`LunaCatalog` over a fake ``fetch_library``, so only the HTTP layer is
replaced. The store is built with ``__new__`` + private-attr injection and a
recording ``_Bus``, as in the Microsoft store tests.

One test per row of the status table in the Luna design:

* no usable session → ``None``, entries kept;
* ``LunaSessionError`` → ``None`` + ``session_expired`` (not retried);
* host without region constants → ``[]`` + ``region_unavailable``;
* transport / HTTP / shape error → 4 attempts, then ``None`` + ``sync_error``;
* 200 with titles → the titles + ``connected``;
* 200 with zero titles → ``[]`` + ``connected``;
* 200 with malformed tiles → valid titles + carried-forward entries.

And ``get_library`` never launches or attaches a browser.

_Requirements: 4.2, 4.7, 4.9, 11.5, 11.6, 13.2_
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from unifideck.core.types import Events, Game
from unifideck.launcher.browser_games import BROWSER_URL_KEY
from unifideck.stores.luna.luna_api import (
    LunaApiError,
    LunaApiShapeError,
    LunaSessionError,
    ParsedLibrary,
)
from unifideck.stores.luna.luna_catalog import LunaCatalog, LunaTitle
from unifideck.stores.luna.luna_config import LunaConfig
from unifideck.stores.luna.luna_store import (
    MAX_ATTEMPTS,
    MAX_RETRIES,
    LunaStatus,
    LunaStore,
)

_URL = "https://luna.amazon.de/game/marbles-on-stream/B0HPX7NT7F"
_TITLE = LunaTitle(
    luna_product_id="amzn1.luna.product.aaaa", name="Marbles On Stream",
    browser_url=_URL, asin="B0HPX7NT7F",
)


class _Bus:
    """Records every emit so a test can read the status channel."""

    def __init__(self) -> None:
        self.emitted: list[tuple[Any, dict[str, Any]]] = []

    async def emit(self, event: Any, **payload: Any) -> None:
        self.emitted.append((event, payload))

    def skip_reasons(self) -> list[str]:
        return [p["reason"] for e, p in self.emitted if e == Events.SYNC_SKIPPED]


class _Edge:
    is_installed = True


class _Session:
    def __init__(self, usable: bool = True) -> None:
        self._usable = usable

    def is_usable(self) -> bool:
        return self._usable


class _NoBrowser:
    """A ``LunaBrowser`` that fails the test if anything opens a window."""

    def launch(self, url: str) -> bool:
        raise AssertionError("get_library must not launch a browser")

    def cdp_port(self) -> int:
        raise AssertionError("get_library must not attach to a browser")


class _Fetch:
    """A ``fetch_library`` replacement: raise ``errors`` in order, then answer."""

    def __init__(
        self, *, errors: list[Exception] | None = None,
        library: ParsedLibrary | None = None,
    ) -> None:
        self._errors = list(errors or [])
        self._library = library or ParsedLibrary(titles=[_TITLE], malformed=0)
        self.calls = 0

    async def __call__(self) -> ParsedLibrary:
        self.calls += 1
        if self._errors:
            raise self._errors.pop(0)
        return self._library


def _store(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fetch: Callable[[], Awaitable[ParsedLibrary]] | None = None,
    usable: bool = True,
    host: str = "https://luna.amazon.de",
    previous: list[dict[str, object]] | None = None,
) -> tuple[LunaStore, _Bus]:
    """A ``LunaStore`` over a real :class:`LunaCatalog` and a fake fetch."""
    store = LunaStore.__new__(LunaStore)
    store._edge = _Edge()
    store._session = _Session(usable)
    store._browser = _NoBrowser()  # type: ignore[assignment]
    store._bus = _Bus()  # type: ignore[assignment]
    store._catalog = LunaCatalog(fetch) if fetch is not None else None
    store._luna_config = LunaConfig(luna_host=host, profile_dir="/nonexistent/luna")
    store._last_stable_status = LunaStatus.NOT_CONNECTED
    monkeypatch.setattr(store, "_previous_luna_entries", lambda: list(previous or []))
    monkeypatch.setattr(
        "unifideck.stores.luna.luna_store._RETRY_BACKOFF_SECONDS", 0.0,
    )
    return store, store._bus  # type: ignore[return-value]


# ── no usable session ────────────────────────────────────────────────


async def test_no_usable_session_keeps_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    fetch = _Fetch()
    store, _bus = _store(monkeypatch, fetch=fetch, usable=False)

    assert await store.get_library() is None
    assert fetch.calls == 0


# ── LunaSessionError → session_expired, not retried ──────────────────


@pytest.mark.parametrize("reason", ["Unauthenticated", "no_cookies", "http_401"])
async def test_rejected_session_reports_session_expired(
    monkeypatch: pytest.MonkeyPatch, reason: str,
) -> None:
    fetch = _Fetch(errors=[LunaSessionError(reason)])
    store, bus = _store(monkeypatch, fetch=fetch)

    assert await store.get_library() is None
    assert fetch.calls == 1
    assert store._last_stable_status == LunaStatus.SESSION_EXPIRED
    assert LunaStatus.RECOVERY_IN_PROGRESS not in bus.skip_reasons()


# ── unmapped host → region_unavailable ───────────────────────────────


async def test_unmapped_host_excludes_all_titles(monkeypatch: pytest.MonkeyPatch) -> None:
    store, bus = _store(monkeypatch, host="https://luna.amazon.co.jp")

    assert await store.get_library() == []
    assert store._last_stable_status == LunaStatus.REGION_UNAVAILABLE
    assert LunaStatus.REGION_UNAVAILABLE in bus.skip_reasons()


async def test_mapped_host_builds_api_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without an injected catalog the store builds the API one for the region."""
    store, _bus = _store(monkeypatch)
    catalog = store._catalog_for_region()
    assert isinstance(catalog, LunaCatalog)


# ── transport / HTTP / shape errors → retried, then sync_error ───────


@pytest.mark.parametrize("error", [
    LunaApiError("Luna getPage returned HTTP 503"),
    LunaApiError("Luna request failed: TimeoutError"),
    LunaApiShapeError("Luna getPage response has no game grid"),
])
async def test_api_error_retries_then_reports_sync_error(
    monkeypatch: pytest.MonkeyPatch, error: Exception,
) -> None:
    fetch = _Fetch(errors=[error] * MAX_ATTEMPTS)
    store, bus = _store(monkeypatch, fetch=fetch)

    assert await store.get_library() is None
    assert fetch.calls == MAX_ATTEMPTS == 4
    reasons = bus.skip_reasons()
    assert reasons.count(LunaStatus.RECOVERY_IN_PROGRESS) == MAX_RETRIES == 3
    assert reasons[-1] == LunaStatus.SYNC_ERROR
    assert store._last_stable_status == LunaStatus.SYNC_ERROR


async def test_succeed_on_second_attempt_stops_early(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetch = _Fetch(errors=[LunaApiError("Luna getPage returned HTTP 500")])
    store, bus = _store(monkeypatch, fetch=fetch)

    games = await store.get_library()

    assert games is not None and [g.store_game_id for g in games] == [_TITLE.luna_product_id]
    assert fetch.calls == 2
    assert bus.skip_reasons().count(LunaStatus.RECOVERY_IN_PROGRESS) == 1
    assert store._last_stable_status == LunaStatus.CONNECTED


# ── 200 responses ────────────────────────────────────────────────────


async def test_titles_are_authoritative_and_connected(monkeypatch: pytest.MonkeyPatch) -> None:
    store, _bus = _store(monkeypatch, fetch=_Fetch())

    games = await store.get_library()

    assert games is not None and len(games) == 1
    game = games[0]
    assert isinstance(game, Game)
    assert game.store == "luna"
    assert game.metadata[BROWSER_URL_KEY] == _URL
    assert store._last_stable_status == LunaStatus.CONNECTED


async def test_zero_titles_remove_all_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    fetch = _Fetch(library=ParsedLibrary(titles=[], malformed=0))
    store, _bus = _store(monkeypatch, fetch=fetch)

    assert await store.get_library() == []
    assert store._last_stable_status == LunaStatus.CONNECTED


async def test_malformed_tiles_carry_previous_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    previous = [{
        "store_game_id": "amzn1.adg.product.old",
        "title": "Old Title",
        "metadata": {BROWSER_URL_KEY: "https://luna.amazon.de/game/old/B000000000"},
    }]
    fetch = _Fetch(library=ParsedLibrary(titles=[_TITLE], malformed=1))
    store, _bus = _store(monkeypatch, fetch=fetch, previous=previous)

    games = await store.get_library()

    assert games is not None
    assert {g.store_game_id for g in games} == {
        _TITLE.luna_product_id, "amzn1.adg.product.old",
    }
    assert store._last_stable_status == LunaStatus.CONNECTED


async def test_browser_missing_reads_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    fetch = _Fetch()
    store, _bus = _store(monkeypatch, fetch=fetch)
    store._edge = type("_Missing", (), {"is_installed": False})()

    assert await store.get_library() is None
    assert fetch.calls == 0
