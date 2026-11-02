"""Xbox achievements: title id, row normalization, paging, auth errors."""
from __future__ import annotations

import copy
from typing import Any

import pytest

from unifideck.stores.microsoft import achievements as xa
from unifideck.stores.microsoft.tokens.endpoint import HttpReply
from unifideck.stores.microsoft.tokens.xbl_chain import XBLTokenChain

TITLE = 1810111157  # 0x6BE41AB5
SCID = "00000000-0000-0000-0000-00006BE41AB5"
ICON = "https://images-eds-ssl.xboxlive.com/image?url=abc"


def _row(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": "11",
        "serviceConfigId": SCID,
        "name": "First step",
        "titleAssociations": [{"name": "Game", "id": TITLE}],
        "progressState": "Achieved",
        "progression": {"timeUnlocked": "2026-10-07T21:05:11.9770000Z"},
        "mediaAssets": [{"name": "i", "type": "Icon", "url": ICON}],
        "isSecret": False,
        "description": "Did the thing",
        "lockedDescription": "Do the thing",
        "achievementType": "Persistent",
        "rewards": [{"type": "Gamerscore", "value": "15"}],
        "rarity": {"currentCategory": "Common", "currentPercentage": 48.4},
        "isRevoked": False,
    }
    row.update(over)
    return row


# -- title id ---------------------------------------------------------------


def test_hex_title_id_from_install_record() -> None:
    assert xa.hex_title_id("6BE41AB5") == TITLE
    assert xa.hex_title_id("") is None
    assert xa.hex_title_id("not-hex") is None
    assert xa.hex_title_id(None) is None
    assert xa.hex_title_id("0") is None


def test_catalog_title_id_from_alternate_ids() -> None:
    product = {"AlternateIds": [
        {"IdType": "LegacyWindowsStoreProductId", "Value": "a3f9"},
        {"IdType": "XboxTitleId", "Value": str(TITLE)},
    ]}
    assert xa.catalog_title_id(product) == TITLE
    assert xa.catalog_title_id({"AlternateIds": []}) is None
    assert xa.catalog_title_id(None) is None
    assert xa.catalog_title_id({"AlternateIds": [{"IdType": "XboxTitleId", "Value": "x"}]}) is None


# -- normalization ----------------------------------------------------------


def test_unlocked_row() -> None:
    a = xa.normalize_row(_row(), TITLE)
    assert a is not None
    assert a["key"] == f"{SCID.lower()}:11"
    assert a["unlocked"] is True
    assert a["unlocked_at"] is not None
    assert a["description"] == "Did the thing"
    assert a["image_unlocked"] == a["image_locked"] == ICON
    assert a["rarity"] == 48.4
    assert a["gamerscore"] == 15
    assert a["hidden"] is False


def test_locked_row_uses_locked_description_and_ignores_placeholder_date() -> None:
    a = xa.normalize_row(_row(
        progressState="NotStarted",
        progression={"timeUnlocked": "0001-01-01T00:00:00.0000000Z"},
    ), TITLE)
    assert a is not None
    assert a["unlocked"] is False
    assert a["unlocked_at"] is None
    assert a["description"] == "Do the thing"


def test_in_progress_and_revoked_are_locked() -> None:
    progress = xa.normalize_row(_row(progressState="InProgress"), TITLE)
    revoked = xa.normalize_row(_row(isRevoked=True), TITLE)
    assert progress is not None and progress["unlocked"] is False
    assert revoked is not None and revoked["unlocked"] is False
    assert revoked["unlocked_at"] is None


def test_secret_row_is_hidden() -> None:
    a = xa.normalize_row(_row(isSecret=True, progressState="NotStarted"), TITLE)
    assert a is not None and a["hidden"] is True


def test_rows_for_another_title_or_type_are_rejected() -> None:
    assert xa.normalize_row(_row(titleAssociations=[{"id": 42}]), TITLE) is None
    assert xa.normalize_row(_row(achievementType="Challenge"), TITLE) is None
    assert xa.normalize_row(_row(name=""), TITLE) is None


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "https://evil.test/icon",
    "https://user:pw@images-eds-ssl.xboxlive.com/a",
    "http://images-eds-ssl.xboxlive.com/a",
])
def test_icons_off_the_allowlist_are_dropped(url: str) -> None:
    a = xa.normalize_row(_row(mediaAssets=[{"type": "Icon", "url": url}]), TITLE)
    assert a is not None and a["image_unlocked"] == ""


def test_missing_rarity_and_gamerscore_are_none() -> None:
    a = xa.normalize_row(_row(rarity=None, rewards=[]), TITLE)
    assert a is not None and a["rarity"] is None and a["gamerscore"] is None


def test_duplicates_dropped() -> None:
    out = xa.normalize_rows([_row(), copy.deepcopy(_row())], TITLE)
    assert len(out) == 1


# -- paging and HTTP --------------------------------------------------------


def test_read_page_statuses() -> None:
    with pytest.raises(xa.MicrosoftAchievementsError) as e:
        xa.read_page(401, {})
    assert e.value.code == "auth_expired"
    with pytest.raises(xa.MicrosoftAchievementsError) as e:
        xa.read_page(None, {})
    assert e.value.code == "offline"
    with pytest.raises(xa.MicrosoftAchievementsError) as e:
        xa.read_page(200, {"unexpected": True})
    assert e.value.code == "offline"
    rows, token = xa.read_page(200, {"achievements": [_row()], "pagingInfo": {"continuationToken": None}})
    assert len(rows) == 1 and token is None


class _Tokens:
    """A token manager stand-in: hands out chains, counts refreshes."""

    def __init__(self, chains: list[XBLTokenChain | None], access: str | None = "t") -> None:
        self.access_token = access
        self._chains = chains
        self.refreshes = 0
        self.last_xerr: int | None = None

    async def build_chain(self) -> XBLTokenChain | None:
        return self._chains.pop(0) if self._chains else None

    async def refresh_if_stale(self) -> None:
        self.refreshes += 1


def _chain(xsts: str = "xsts") -> XBLTokenChain:
    return XBLTokenChain(xsts_token=xsts, user_hash="uhs", xuid="2535")


def _reader(tokens: _Tokens, monkeypatch, title: int | None = TITLE) -> xa.MicrosoftAchievements:
    reader = xa.MicrosoftAchievements(tokens, market_fn=lambda: "US", locale_fn=lambda: "en-US")  # type: ignore[arg-type]

    async def title_id(_game_id: str) -> int | None:
        return title

    monkeypatch.setattr(reader, "title_id", title_id)
    return reader


async def test_pages_followed_until_no_token(monkeypatch) -> None:
    pages = [
        HttpReply(200, {"achievements": [_row(id="1")], "pagingInfo": {"continuationToken": "p2"}}),
        HttpReply(200, {"achievements": [_row(id="2")], "pagingInfo": {"continuationToken": None}}),
    ]
    urls: list[str] = []

    def fake(method: str, url: str, headers: dict[str, str]) -> HttpReply:
        urls.append(url)
        assert headers["Authorization"] == "XBL3.0 x=uhs;xsts"
        return pages.pop(0)

    monkeypatch.setattr(xa, "request_json", fake)
    payload = await _reader(_Tokens([_chain()]), monkeypatch).get_game_achievements("9N4PT8HGCDHQ")
    assert payload["total"] == 2
    assert payload["store"] == "microsoft"
    assert "continuationToken=p2" in urls[1]
    assert f"titleId={TITLE}" in urls[0]


async def test_pagination_loop_stops(monkeypatch) -> None:
    calls = []

    def fake(method: str, url: str, headers: dict[str, str]) -> HttpReply:
        calls.append(url)
        return HttpReply(200, {"achievements": [_row(id=str(len(calls)))], "pagingInfo": {"continuationToken": "same"}})

    monkeypatch.setattr(xa, "request_json", fake)
    await _reader(_Tokens([_chain()]), monkeypatch).get_game_achievements("G")
    assert len(calls) == 2


async def test_401_remints_once_then_succeeds(monkeypatch) -> None:
    replies = [HttpReply(401, {}), HttpReply(200, {"achievements": [], "pagingInfo": {"continuationToken": None}})]
    monkeypatch.setattr(xa, "request_json", lambda *a, **k: replies.pop(0))
    tokens = _Tokens([_chain("old"), _chain("new")])
    payload = await _reader(tokens, monkeypatch).get_game_achievements("G")
    assert payload["total"] == 0


async def test_401_twice_is_auth_expired(monkeypatch) -> None:
    monkeypatch.setattr(xa, "request_json", lambda *a, **k: HttpReply(401, {}))
    with pytest.raises(xa.MicrosoftAchievementsError) as e:
        await _reader(_Tokens([_chain(), _chain()]), monkeypatch).get_game_achievements("G")
    assert e.value.code == "auth_expired"


async def test_signed_out_is_not_authed(monkeypatch) -> None:
    with pytest.raises(xa.MicrosoftAchievementsError) as e:
        await _reader(_Tokens([], access=None), monkeypatch).get_game_achievements("G")
    assert e.value.code == "not_authed"


async def test_no_chain_refreshes_once_then_reports(monkeypatch) -> None:
    tokens = _Tokens([None, None])
    with pytest.raises(xa.MicrosoftAchievementsError) as e:
        await _reader(tokens, monkeypatch).get_game_achievements("G")
    assert tokens.refreshes == 1
    assert e.value.code == "offline"
    tokens = _Tokens([None, None])
    tokens.last_xerr = 2148916233
    with pytest.raises(xa.MicrosoftAchievementsError) as e:
        await _reader(tokens, monkeypatch).get_game_achievements("G")
    assert e.value.code == "auth_expired"


async def test_no_title_id_is_no_client_id(monkeypatch) -> None:
    with pytest.raises(xa.MicrosoftAchievementsError) as e:
        await _reader(_Tokens([_chain()]), monkeypatch, title=None).get_game_achievements("G")
    assert e.value.code == "no_client_id"


async def test_title_id_record_first_then_catalog(monkeypatch) -> None:
    reader = xa.MicrosoftAchievements(_Tokens([]), market_fn=lambda: "US", locale_fn=lambda: "en-US")  # type: ignore[arg-type]
    monkeypatch.setattr(xa, "record_for_game", lambda gid: {"titleId": "6BE41AB5"} if gid == "INST" else None)
    asked: list[list[str]] = []

    async def products(ids: list[str], market: str) -> dict[str, Any]:
        asked.append(ids)
        return {"9N4PT8HGCDHQ": {"AlternateIds": [{"IdType": "XboxTitleId", "Value": "42"}]}}

    monkeypatch.setattr(xa, "fetch_products", products)
    assert await reader.title_id("INST") == TITLE
    assert asked == []
    assert await reader.title_id("9n4pt8hgcdhq") == 42
    assert await reader.title_id("9n4pt8hgcdhq") == 42
    assert len(asked) == 1


async def test_cache_and_clear(monkeypatch) -> None:
    calls = []

    def fake(*a: Any, **k: Any) -> HttpReply:
        calls.append(1)
        return HttpReply(200, {"achievements": [], "pagingInfo": {"continuationToken": None}})

    monkeypatch.setattr(xa, "request_json", fake)
    reader = _reader(_Tokens([_chain(), _chain()]), monkeypatch)
    await reader.get_game_achievements("G")
    await reader.get_game_achievements("G")
    assert len(calls) == 1
    await reader.get_game_achievements("G", force=True)
    assert len(calls) == 2
    reader.clear()
    assert reader._chain is None


async def test_sign_out_and_account_switch_never_show_old_unlocks(monkeypatch) -> None:
    calls = []

    def fake(*a: Any, **k: Any) -> HttpReply:
        calls.append(1)
        return HttpReply(200, {"achievements": [_row()], "pagingInfo": {"continuationToken": None}})

    monkeypatch.setattr(xa, "request_json", fake)
    other = XBLTokenChain(xsts_token="x2", user_hash="u2", xuid="9999")  # noqa: S106 — test stand-in, not a credential
    tokens = _Tokens([_chain(), other])
    reader = _reader(tokens, monkeypatch)
    await reader.get_game_achievements("G")

    tokens.access_token = None  # signed out
    with pytest.raises(xa.MicrosoftAchievementsError) as e:
        await reader.get_game_achievements("G", force=True)
    assert e.value.code == "not_authed"

    tokens.access_token = "t2"  # noqa: S105 — another account signs in (stand-in)
    await reader.get_game_achievements("G")
    assert len(calls) == 2  # not served from the first account's cache
    assert reader._xuid == "9999"
