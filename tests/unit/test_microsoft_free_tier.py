"""xCloud without Game Pass: the free titles sync, and the user is told why
that is all they got.

Measured 2026-10-05 with a no-Game-Pass account: its ``xgpuweb`` login
succeeds and the token's ``offeringid`` claim is XGPUWEB, like a
subscriber's. Only the ``/v2/titles`` rows tell the two apart:
``details.userSubscriptions`` is ``[]`` for the free account, and it is
entitled to Fortnite alone.
"""
from __future__ import annotations

import base64
import json
from typing import Any

from test_microsoft_library_unreadable import (
    _ACTIVE,
    _Bus,
    _Catalog,
    _Service,
    _Session,
    _store,
    _Tokens,
)

from unifideck.core.types import SubscriptionTier
from unifideck.services.microsoft_subscription.service import TierAnswer
from unifideck.stores.microsoft.library_gate import GateVerdict, check_subscription_gate
from unifideck.stores.microsoft.microsoft_catalog import _account_has_subscription
from unifideck.stores.microsoft.microsoft_subscription import _parse_tier_from_response


def _login(offering_id: str) -> dict[str, Any]:
    claims = base64.urlsafe_b64encode(json.dumps({"offeringid": offering_id}).encode()).decode().rstrip("=")
    return {"gsToken": f"h.{claims}.s", "offeringSettings": {"regions": [{"baseUri": "https://r"}]}}


def _row(plans: Any) -> dict[str, Any]:
    details: dict[str, Any] = {"hasEntitlement": True, "productId": "BT5P2X999VH2"}
    if plans is not ...:
        details["userSubscriptions"] = plans
    return {"titleId": "FORTNITE", "details": details}


# ── tier: an f2p offering is a free session, not "no xCloud at all" ──

def test_f2p_offering_is_free_not_none() -> None:
    """``NONE`` makes the gate return ``[]``, deleting every xCloud shortcut."""
    assert _parse_tier_from_response(_login("XGPUWEBF2P")) == SubscriptionTier.FREE


def test_paid_offering_is_still_ultimate() -> None:
    assert _parse_tier_from_response(_login("XGPUWEB")) == SubscriptionTier.ULTIMATE


async def test_gate_lets_a_free_session_read_the_catalog() -> None:
    bus = _Bus()
    verdict = await check_subscription_gate(
        _Service(TierAnswer(SubscriptionTier.FREE, authoritative=True)), _Tokens(), bus,
    )
    assert verdict is GateVerdict.PROCEED
    assert bus.skip_reasons() == []


# ── catalog: the rows say whether the account has a plan ─────────────

def test_rows_with_no_plans_mean_no_game_pass() -> None:
    assert _account_has_subscription([_row([])]) is False


def test_any_row_with_a_plan_means_game_pass() -> None:
    assert _account_has_subscription([_row([]), _row(["XGPULTIMATE", "XGPSTANDARD", "XGPCORE"])]) is True


def test_a_row_without_the_field_is_unknown() -> None:
    """A changed response shape must never read as "no Game Pass"."""
    assert _account_has_subscription([_row([]), _row(...)]) is None
    assert _account_has_subscription([_row(None)]) is None
    assert _account_has_subscription([]) is None


# ── store: the free titles are kept, and the user hears why ──────────

async def test_free_account_keeps_its_titles_and_is_told() -> None:
    games = ["fortnite"]
    store = _store(_Service(_ACTIVE, session=_Session()), catalog=_Catalog(games, has_subscription=False))
    assert await store.get_library() == games
    assert store._bus.skip_reasons() == ["free_titles_only"]


async def test_subscriber_and_unknown_are_silent() -> None:
    for seen in (True, None):
        store = _store(_Service(_ACTIVE, session=_Session()), catalog=_Catalog(["g"], has_subscription=seen))
        assert await store.get_library() == ["g"]
        assert store._bus.skip_reasons() == []


async def test_unreadable_catalog_says_nothing_about_game_pass() -> None:
    store = _store(_Service(_ACTIVE, session=_Session()), catalog=_Catalog(None, has_subscription=False))
    assert await store.get_library() is None
    assert store._bus.skip_reasons() == []
