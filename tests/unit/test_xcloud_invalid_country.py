"""Cloud Gaming refusing the connection's location (``InvalidCountry``).

It is not a plan problem. The sync still lists the Xbox PC games and keeps
every xCloud shortcut it had; the stream is what is blocked, at launch, with a
toast.
"""
from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_microsoft_library_unreadable import _Bus, _PcLibrary, _Service, _store, _Tokens

from unifideck.core.types import Game, GameTag, SubscriptionTier
from unifideck.launcher.xcloud_player import credentials
from unifideck.services.launcher import xcloud_player_game
from unifideck.services.microsoft_subscription.service import TierAnswer
from unifideck.stores.microsoft import microsoft_store, microsoft_subscription, xcloud_kept
from unifideck.stores.microsoft.library_gate import GateVerdict, check_subscription_gate
from unifideck.stores.microsoft.microsoft_subscription import INVALID_COUNTRY

_URL = "https://xgpuweb.example/v2/login/user"


def _probe_with(monkeypatch: pytest.MonkeyPatch, status: int, body: bytes) -> Any:
    def refused(*_a: Any, **_k: Any) -> Any:
        raise urllib.error.HTTPError(_URL, status, "refused", {}, io.BytesIO(body))  # type: ignore[arg-type]

    monkeypatch.setattr(microsoft_subscription.urllib.request, "urlopen", refused)
    return microsoft_subscription._probe_sync("uhs", "xsts", _URL, 5, "xgpuweb")


def test_invalid_country_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    body = json.dumps({"code": "InvalidCountry", "statusCode": 403, "message": ""}).encode()
    result = _probe_with(monkeypatch, 403, body)
    assert not result.ok and result.error == INVALID_COUNTRY and result.http_status == 403


@pytest.mark.parametrize("body", [b"<html>forbidden</html>", b'{"code":"Other"}', b""])
def test_any_other_403_stays_forbidden(monkeypatch: pytest.MonkeyPatch, body: bytes) -> None:
    assert _probe_with(monkeypatch, 403, body).error == "forbidden"


# ── the gate ─────────────────────────────────────────────────────────

async def test_gate_invalid_country_has_its_own_verdict_and_no_toast() -> None:
    bus = _Bus()
    answer = TierAnswer(SubscriptionTier.NONE, authoritative=False, reason=INVALID_COUNTRY)
    assert await check_subscription_gate(_Service(answer), _Tokens(), bus) is GateVerdict.INVALID_COUNTRY
    assert bus.skip_reasons() == []


async def test_gate_other_failures_are_unchanged() -> None:
    bus = _Bus()
    answer = TierAnswer(SubscriptionTier.NONE, authoritative=False, reason="forbidden")
    assert await check_subscription_gate(_Service(answer), _Tokens(), bus) is GateVerdict.UNREADABLE
    assert bus.skip_reasons() == ["subscription_check_error"]


async def test_the_service_carries_the_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    from unifideck.services.microsoft_subscription.service import MicrosoftSubscriptionService

    svc = MicrosoftSubscriptionService.__new__(MicrosoftSubscriptionService)
    import asyncio

    svc._lock = asyncio.Lock()
    failed = microsoft_subscription.SubscriptionProbeResult(
        tier=SubscriptionTier.NONE, ok=False, error=INVALID_COUNTRY)

    async def key(_t: Any) -> str:
        return "k"

    async def probe(_t: Any) -> Any:
        return failed

    monkeypatch.setattr(svc, "_resolve_cache_key", key)
    monkeypatch.setattr(svc, "_run_probe", probe)
    monkeypatch.setattr(svc, "_read_cache", lambda _k: None)
    assert (await svc.get_tier_checked(None)).reason == INVALID_COUNTRY  # type: ignore[arg-type]
    stale = SimpleNamespace(is_fresh=lambda: False, tier=SubscriptionTier.ULTIMATE, detected_at=0.0)
    monkeypatch.setattr(svc, "_read_cache", lambda _k: stale)
    answer = await svc.get_tier_checked(None)  # type: ignore[arg-type]
    assert not answer.authoritative and answer.reason == INVALID_COUNTRY


# ── the kept xCloud rows ─────────────────────────────────────────────

def _game(sgid: str, tags: list[str], **meta: Any) -> dict[str, Any]:
    return {"app_id": 1, "store": "microsoft", "store_game_id": sgid, "title": sgid,
            "tags": tags, "metadata": meta, "installed": False}


@pytest.fixture
def cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "library_cache.json"
    monkeypatch.setattr(xcloud_kept, "library_cache_path", lambda _c: path)
    return path


def test_kept_rows_are_the_cloud_ones_in_cloud_form(cache: Path) -> None:
    cache.write_text(json.dumps({"libraries": {
        "microsoft": [
            _game("CLOUD", ["xcloud", "browser"], browser_url="u"),
            _game("BOTH", ["xcloud", "browser", "xbox_pc"], xbox_pc={"product_id": "X"}, game_pass=True, browser_url="u"),
            _game("PCONLY", ["xbox_pc"], xbox_pc={"product_id": "Y"}),
        ],
        "gog": [_game("G", ["xcloud"])],
    }}))
    rows = {g.store_game_id: g for g in xcloud_kept.kept_xcloud_rows(None)}
    assert set(rows) == {"CLOUD", "BOTH"}
    assert GameTag.XBOX_PC not in rows["BOTH"].tags
    assert rows["BOTH"].metadata == {"browser_url": "u"}


@pytest.mark.parametrize("content", [None, "not json", "[]", '{"libraries": {"microsoft": "x"}}'])
def test_an_unreadable_cache_keeps_nothing(cache: Path, content: str | None) -> None:
    if content is not None:
        cache.write_text(content)
    assert xcloud_kept.kept_xcloud_rows(None) == []


# ── the store: PC games listed, cloud shortcuts kept ─────────────────

_REFUSED = TierAnswer(SubscriptionTier.NONE, authoritative=False, reason=INVALID_COUNTRY)


async def test_get_library_composes_kept_cloud_rows_with_the_pc_list(monkeypatch: pytest.MonkeyPatch) -> None:
    kept = [Game(app_id=1, store="microsoft", store_game_id="CLOUD", title="c", tags=[GameTag.XCLOUD])]
    pc_row = Game(app_id=2, store="microsoft", store_game_id="PC", title="p", tags=[GameTag.XBOX_PC], installed=True)
    monkeypatch.setattr(microsoft_store, "kept_xcloud_rows", lambda _c: kept)
    pc = _PcLibrary(extra=[pc_row])
    store = _store(_Service(_REFUSED), pc=pc)
    store._config_manager = None
    rows = await store.get_library()
    assert rows == [*kept, pc_row]
    assert pc.composed[0]["xcloud"] == kept and pc.composed[0]["market"] is None
    assert store._bus.skip_reasons() == []  # type: ignore[attr-defined]


async def test_get_library_still_keeps_everything_when_unreadable() -> None:
    store = _store(_Service(TierAnswer(SubscriptionTier.NONE, authoritative=False, reason="forbidden")))
    assert await store.get_library() is None


# ── launch: blocked, with a toast ────────────────────────────────────

def test_stream_login_flags_invalid_country(monkeypatch: pytest.MonkeyPatch) -> None:
    from unifideck.stores.microsoft.tokens.endpoint import HttpReply

    monkeypatch.setattr(credentials, "saved_refresh_token", lambda _p: ("rt", ""))
    monkeypatch.setattr(credentials, "post_form", lambda *_a, **_k: HttpReply(200, {"access_token": "a"}))
    monkeypatch.setattr(credentials, "_gssv_xsts", lambda _a: ("uhs", "xsts"))
    monkeypatch.setattr(credentials, "_probe_sync", lambda *_a: microsoft_subscription.SubscriptionProbeResult(
        tier=SubscriptionTier.NONE, ok=False, error=INVALID_COUNTRY))
    login = credentials.stream_login("x")
    assert login.invalid_country and not login.ok


class _StageBus:
    def __init__(self) -> None:
        self.stages: list[dict[str, Any]] = []

    async def emit(self, _event: Any, **payload: Any) -> None:
        self.stages.append(payload)


def _svc() -> Any:
    opened: list[Any] = []
    return SimpleNamespace(
        _config=SimpleNamespace(get=lambda k, d=None: d), _bus=_StageBus(), _active_subprocess=None,
        _edge_browser=SimpleNamespace(find_cmd=lambda: opened.append(1)), opened=opened)


_CTX = SimpleNamespace(store="microsoft", game_key="microsoft:9P5Z4530318L")
_GAME_URL = "https://www.xbox.com/play/launch/9P5Z4530318L"


async def test_launch_is_blocked_with_the_toast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(credentials, "stream_login",
                        lambda _f: credentials.StreamLogin(reason="refused", invalid_country=True))
    svc = _svc()
    result = await xcloud_player_game.try_own_player(svc, _CTX, _GAME_URL)
    assert result is not None and not result.success and result.error == "xcloud_invalid_country"
    keys = [s.get("i18n_key") for s in svc._bus.stages]
    assert keys == ["toasts.launcher.invalidCountry"]
    assert svc._bus.stages[0]["i18n_title_key"] == "toasts.header.xboxCloud"
    assert svc.opened == []


async def test_other_login_failures_still_fall_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(credentials, "stream_login", lambda _f: credentials.StreamLogin(reason="no sign-in"))
    svc = _svc()
    assert await xcloud_player_game.try_own_player(svc, _CTX, _GAME_URL) is None
    assert svc._bus.stages == []
