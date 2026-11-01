"""An xCloud library that could not be read must never look empty.

The sync contract (``core/sync_run_mixin.py``): ``get_library`` returning a
list, even an empty one, is authoritative, and the post-sync reconcile
deletes every shortcut of that store missing from it. ``None`` means "could
not read" and keeps them all.

``MicrosoftStore`` used to return ``[]`` on every failure: a refresh hiccup,
a subscription probe that failed with no cached tier, a missing xCloud
session, a ``/v2/titles`` error. Each one deleted every xCloud shortcut the
user had. ``[]`` is now reserved for Microsoft saying the account has no
subscription.
"""
from __future__ import annotations

import asyncio
from typing import Any

from unifideck.core.types import Events, Game, GameTag, SubscriptionTier
from unifideck.services.microsoft_subscription.service import (
    MicrosoftSubscriptionService,
    TierAnswer,
)
from unifideck.stores.microsoft.library_gate import (
    GateVerdict,
    check_subscription_gate,
)
from unifideck.stores.microsoft.microsoft_catalog import MicrosoftCatalogReader
from unifideck.stores.microsoft.microsoft_store import MicrosoftStore
from unifideck.stores.microsoft.session_health import SessionHealth
from unifideck.stores.microsoft.tokens import TokenState


class _Bus:
    def __init__(self) -> None:
        self.emitted: list[tuple[Any, dict[str, Any]]] = []

    async def emit(self, event: Any, **payload: Any) -> None:
        self.emitted.append((event, payload))

    def skip_reasons(self) -> list[str]:
        return [p["reason"] for e, p in self.emitted if e == Events.SYNC_SKIPPED]


class _Service:
    def __init__(self, answer: TierAnswer | Exception, session: Any = None) -> None:
        self.answer = answer
        self.session = session

    async def get_tier_checked(self, tokens: Any) -> TierAnswer:
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    async def get_session(self, tokens: Any) -> Any:
        return self.session


class _Tokens:
    client_mismatch = False
    mismatch_notified = False
    last_token_error = ""

    def __init__(self, *, loaded: bool = True, state: TokenState = TokenState.FRESH) -> None:
        self.loaded = loaded
        self.state = state

    async def load(self) -> bool:
        return self.loaded

    async def refresh_if_stale(self) -> TokenState:
        return self.state

    async def clear(self) -> None:
        pass


class _Config:
    def is_valid(self) -> bool:
        return True


class _Session:
    gs_token = "gs"
    regions = [{"baseUri": "https://region.example", "isDefault": True}]
    market = "US"


class _XboxPc:
    """The facade, as the store's xCloud read sees it."""

    def __init__(self) -> None:
        self.notified = 0

    def notify(self) -> None:
        self.notified += 1


class _PcLibrary:
    """Passes xCloud through and records what the store handed it."""

    def __init__(self, extra: list[Any] | None = None) -> None:
        self.extra = extra or []
        self.reads: list[str | None] = []
        self.composed: list[dict[str, Any]] = []
        self.pending: asyncio.Future[Any] | None = None
        self.notices: list[str] = []

    async def notify_once(self, reason: str) -> None:
        self.notices.append(reason)

    def start_read(self, market: str | None) -> asyncio.Future[Any]:
        self.reads.append(market)
        self.pending = asyncio.get_running_loop().create_future()
        return self.pending

    def now(self) -> float:
        return 1000.0

    async def compose(self, xcloud: list[Any], *, market: str | None, pending_read: Any = None,
                      started: float | None = None) -> list[Any]:
        assert started == 1000.0, "the budget counts from the start of get_library"
        self.composed.append({"xcloud": list(xcloud), "market": market, "pending": pending_read})
        return [*xcloud, *self.extra]


class _Catalog:
    def __init__(self, result: Any, *, has_subscription: bool | None = None) -> None:
        self.result = result
        self.has_subscription = has_subscription

    async def fetch_games(self, session: Any) -> Any:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _store(
    service: Any, *, catalog: Any = None, tokens: _Tokens | None = None, pc: _PcLibrary | None = None,
) -> MicrosoftStore:
    store = MicrosoftStore.__new__(MicrosoftStore)
    store._cached_available = False
    store._last_market = None
    store._xbox_pc = _XboxPc()  # type: ignore[assignment]
    store._pc_library = pc or _PcLibrary()  # type: ignore[assignment]
    store._ms_config = _Config()
    store._tokens = tokens or _Tokens()
    store._bus = _Bus()
    store._health = SessionHealth(store._bus, store._tokens)
    store._subscription_service = service
    store._catalog = catalog or _Catalog([])
    store._poll_task = None
    return store


_ACTIVE = TierAnswer(SubscriptionTier.ULTIMATE, authoritative=True)


# ── the gate keeps "no subscription" and "could not check" apart ─────

async def test_gate_authoritative_none_is_an_empty_library() -> None:
    bus = _Bus()
    verdict = await check_subscription_gate(
        _Service(TierAnswer(SubscriptionTier.NONE, authoritative=True)), _Tokens(), bus,
    )
    assert verdict is GateVerdict.EMPTY
    assert bus.skip_reasons() == ["no_active_subscription"]


async def test_gate_guessed_none_is_unreadable() -> None:
    """The bug: a failed probe with no cache answered NONE and wiped the library."""
    bus = _Bus()
    verdict = await check_subscription_gate(
        _Service(TierAnswer(SubscriptionTier.NONE, authoritative=False)), _Tokens(), bus,
    )
    assert verdict is GateVerdict.UNREADABLE
    assert bus.skip_reasons() == ["subscription_check_error"]


async def test_gate_exception_is_unreadable() -> None:
    bus = _Bus()
    verdict = await check_subscription_gate(_Service(RuntimeError("boom")), _Tokens(), bus)
    assert verdict is GateVerdict.UNREADABLE
    assert bus.skip_reasons() == ["subscription_check_error"]


async def test_gate_unknown_tier_is_unreadable() -> None:
    bus = _Bus()
    verdict = await check_subscription_gate(
        _Service(TierAnswer(SubscriptionTier.ACTIVE_UNKNOWN, authoritative=True)), _Tokens(), bus,
    )
    assert verdict is GateVerdict.UNREADABLE
    assert bus.skip_reasons() == ["subscription_tier_unknown"]


async def test_gate_active_tier_proceeds_silently() -> None:
    bus = _Bus()
    assert await check_subscription_gate(_Service(_ACTIVE), _Tokens(), bus) is GateVerdict.PROCEED
    assert bus.skip_reasons() == []


# ── get_library: None for every "could not read" ─────────────────────

async def test_library_is_none_when_signed_out() -> None:
    store = _store(_Service(_ACTIVE), tokens=_Tokens(loaded=False))
    assert await store.get_library() is None


async def test_library_is_none_when_microsoft_cannot_be_reached() -> None:
    """Offline: the session is kept and so are the shortcuts."""
    store = _store(_Service(_ACTIVE), tokens=_Tokens(state=TokenState.TRANSIENT))
    assert await store.get_library() is None


async def test_library_is_none_when_the_tier_was_guessed() -> None:
    store = _store(_Service(TierAnswer(SubscriptionTier.NONE, authoritative=False)))
    assert await store.get_library() is None


async def test_library_is_empty_only_when_microsoft_says_no_subscription() -> None:
    store = _store(_Service(TierAnswer(SubscriptionTier.NONE, authoritative=True)))
    assert await store.get_library() == []


async def test_library_is_none_without_an_xcloud_session() -> None:
    store = _store(_Service(_ACTIVE, session=None))
    assert await store.get_library() is None


async def test_library_is_none_when_the_catalog_raises() -> None:
    store = _store(_Service(_ACTIVE, session=_Session()), catalog=_Catalog(RuntimeError("x")))
    assert await store.get_library() is None


async def test_library_passes_the_catalog_answer_through() -> None:
    games = ["game"]
    store = _store(_Service(_ACTIVE, session=_Session()), catalog=_Catalog(games))
    assert await store.get_library() == games


# ── the Xbox PC half rides on the xCloud read ────────────────────────

async def test_the_pc_read_starts_with_the_session_market_and_is_handed_on() -> None:
    pc = _PcLibrary()
    store = _store(_Service(_ACTIVE, session=_Session()), catalog=_Catalog(["game"]), pc=pc)
    await store.get_library()
    assert pc.reads == ["US"]
    assert pc.composed == [{"xcloud": ["game"], "market": "US", "pending": pc.pending}]


async def test_a_failed_catalog_cancels_the_pc_read_and_keeps_everything() -> None:
    """``None`` keeps every Microsoft shortcut, PC ones included."""
    pc = _PcLibrary()
    store = _store(_Service(_ACTIVE, session=_Session()), catalog=_Catalog(RuntimeError("x")), pc=pc)
    assert await store.get_library() is None
    assert pc.pending is not None and pc.pending.cancelled()
    assert pc.composed == []


async def test_an_unreadable_catalog_cancels_the_pc_read() -> None:
    pc = _PcLibrary()
    store = _store(_Service(_ACTIVE, session=_Session()), catalog=_Catalog(None), pc=pc)
    assert await store.get_library() is None
    assert pc.pending is not None and pc.pending.cancelled()


async def test_no_subscription_still_lists_pc_rows_and_says_so() -> None:
    row = Game(app_id=0, store="microsoft", store_game_id="9PK087LNGJC5", title="Balatro",
               tags=[GameTag.XBOX_PC])
    pc = _PcLibrary(extra=[row])
    store = _store(_Service(TierAnswer(SubscriptionTier.NONE, authoritative=True)), pc=pc)
    assert await store.get_library() == [row]
    assert pc.composed[0]["xcloud"] == [] and pc.composed[0]["pending"] is None
    assert pc.notices == ["xbox_pc_only"]


async def test_no_subscription_and_no_pc_rows_is_still_empty() -> None:
    pc = _PcLibrary()
    store = _store(_Service(TierAnswer(SubscriptionTier.NONE, authoritative=True)), pc=pc)
    assert await store.get_library() == []
    assert pc.notices == []


async def test_the_sign_in_flip_tells_the_xbox_pc_status() -> None:
    store = _store(_Service(_ACTIVE))
    assert await store.is_available() is True
    assert await store.is_available() is True
    assert store._xbox_pc.notified == 1  # type: ignore[attr-defined]


# ── the catalog reader: a failed or empty /v2/titles is not "no games" ──

def _reader(titles: list[dict[str, Any]]) -> MicrosoftCatalogReader:
    reader = MicrosoftCatalogReader.__new__(MicrosoftCatalogReader)
    reader._config = None
    reader._config_manager = None

    async def fetch(base_uri: str, gs_token: str) -> list[dict[str, Any]]:
        return titles

    reader._fetch_xcloud_titles = fetch  # type: ignore[method-assign]
    return reader


async def test_catalog_failed_titles_fetch_is_none() -> None:
    assert await _reader([]).fetch_games(_Session()) is None


async def test_catalog_with_no_entitled_titles_is_none() -> None:
    titles = [{"details": {"hasEntitlement": False, "productId": "9ABC"}}]
    assert await _reader(titles).fetch_games(_Session()) is None


async def test_catalog_session_without_token_is_none() -> None:
    session = _Session()
    session.gs_token = ""
    assert await _reader([]).fetch_games(session) is None


# ── the service says when its answer is a guess ──────────────────────

class _Cached:
    def __init__(self, tier: SubscriptionTier, *, fresh: bool) -> None:
        self.tier = tier
        self._fresh = fresh
        self.expires_at = 0.0
        self.detected_at = 0.0

    def is_fresh(self) -> bool:
        return self._fresh


class _Probe:
    def __init__(self, ok: bool) -> None:
        self.ok = ok
        self.error = None if ok else "http_503"
        self.tier = SubscriptionTier.ULTIMATE


def _service(cached: _Cached | None, probe: _Probe) -> MicrosoftSubscriptionService:
    svc = MicrosoftSubscriptionService.__new__(MicrosoftSubscriptionService)
    svc._lock = asyncio.Lock()

    async def resolve(tokens: Any) -> str:
        return "ms_sub_1"

    async def run_probe(tokens: Any) -> _Probe:
        return probe

    async def handle(key: str, result: Any) -> SubscriptionTier:
        return result.tier

    svc._resolve_cache_key = resolve  # type: ignore[method-assign]
    svc._read_cache = lambda key: cached  # type: ignore[method-assign]
    svc._run_probe = run_probe  # type: ignore[method-assign]
    svc._handle_probe_success = handle  # type: ignore[method-assign]
    return svc


async def test_fresh_cache_is_authoritative() -> None:
    svc = _service(_Cached(SubscriptionTier.NONE, fresh=True), _Probe(ok=False))
    assert await svc.get_tier_checked(None) == TierAnswer(SubscriptionTier.NONE, True)


async def test_successful_probe_is_authoritative() -> None:
    svc = _service(None, _Probe(ok=True))
    assert await svc.get_tier_checked(None) == TierAnswer(SubscriptionTier.ULTIMATE, True)


async def test_failed_probe_without_cache_is_a_guess() -> None:
    svc = _service(None, _Probe(ok=False))
    assert await svc.get_tier_checked(None) == TierAnswer(SubscriptionTier.NONE, False)


async def test_failed_probe_with_stale_cache_is_a_guess() -> None:
    svc = _service(_Cached(SubscriptionTier.ULTIMATE, fresh=False), _Probe(ok=False))
    assert await svc.get_tier_checked(None) == TierAnswer(SubscriptionTier.ULTIMATE, False)


async def test_get_tier_still_returns_the_bare_tier() -> None:
    svc = _service(None, _Probe(ok=True))
    assert await svc.get_tier(None) == SubscriptionTier.ULTIMATE
