"""Browser sign-in: one shared start, and a clean session nobody declares.

Every store's login form must start from a clean session: a live session in the
shared sign-in browser redirects at once, past the capture and with whichever
account signed in last (EA, 2026-11-15). The orchestrator derives the sites to
clear from what each flow already passes it; stores add nothing.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from unifideck.auth import orchestrator as orchestrator_mod
from unifideck.auth.edge_browser.profile import EdgeProfileManager
from unifideck.auth.sign_in_cookies import sign_in_sites, site_of
from unifideck.core.types import AuthResult
from unifideck.stores.amazon.amazon_auth import _AMAZON_REDIRECT_URIS
from unifideck.stores.amazon.amazon_store import AmazonStore
from unifideck.stores.ea.auth import EA_REDIRECT
from unifideck.stores.ea.store import EAStore
from unifideck.stores.epic.auth import _EPIC_REDIRECT_URIS
from unifideck.stores.epic.store import EpicStore
from unifideck.stores.gog.store import GOGStore
from unifideck.stores.itch.auth import KEY_CAPTURE
from unifideck.stores.itch.store import ItchStore

_STORES = [EpicStore, GOGStore, AmazonStore, EAStore, ItchStore]


@pytest.mark.parametrize(("url", "allowed", "content", "intercept", "sites"), [
    ("https://legendary.gl/epiclogin", _EPIC_REDIRECT_URIS, None, None, ["epicgames.com", "legendary.gl"]),
    ("https://auth.gog.com/auth?client_id=1", ["https://embed.gog.com/on_login_success"], None, None, ["gog.com"]),
    ("https://www.amazon.com/ap/signin?x=1", _AMAZON_REDIRECT_URIS, None, None, ["amazon.com"]),
    ("https://itch.io/login", [], KEY_CAPTURE, None, ["itch.io"]),
    ("https://accounts.ea.com/connect/auth?client_id=JUNO_PC_CLIENT", [], None, EA_REDIRECT, ["ea.com"]),
    ("https://login.live.com/oauth20_authorize.srf", ["https://login.microsoftonline.com/common/oauth2/nativeclient"],
     None, None, ["live.com", "microsoftonline.com"]),
], ids=["epic", "gog", "amazon", "itch", "ea", "microsoft"])
def test_each_flow_names_its_own_sites(
    url: str, allowed: list[str], content: Any, intercept: Any, sites: list[str],
) -> None:
    assert sign_in_sites(url, allowed, content, intercept) == sites


@pytest.mark.parametrize("value", ["http://localhost:31033/callback", "http://127.0.0.1:8080/", "qrc:/html/x"])
def test_local_and_custom_scheme_addresses_have_no_site(value: str) -> None:
    assert site_of(value) is None


async def test_run_flow_clears_the_sites_before_the_window_opens(monkeypatch: pytest.MonkeyPatch) -> None:
    cleared: list[list[str]] = []

    async def record(sites: Any) -> None:
        cleared.append(list(sites))

    monkeypatch.setattr(orchestrator_mod, "clear_sign_in_cookies", record)
    orch = orchestrator_mod.AuthOrchestrator(bus=MagicMock(emit=AsyncMock()), browser_monitor=MagicMock(), store_name="ea")
    monkeypatch.setattr(orch, "_spawn_background_task", lambda **_: AuthResult(success=True, store="ea"))

    async def url() -> str:
        return "https://accounts.ea.com/connect/auth?client_id=JUNO_PC_CLIENT"

    await orch.run_flow(get_url=url, allowed_uris=[], exchange_code=AsyncMock(), background=True, intercept=EA_REDIRECT)

    assert cleared == [["ea.com"]]


@pytest.mark.parametrize("cls", _STORES, ids=[c.__name__ for c in _STORES])
async def test_stores_open_the_window_and_clear_nothing_themselves(cls: type[Any]) -> None:
    store = object.__new__(cls)
    store._auth = MagicMock(start_auth=AsyncMock(return_value=AuthResult(success=True, store="x")))
    store._edge = MagicMock(is_installed=True)

    await store.start_auth()

    store._edge.clear_store_cookies.assert_not_called()
    store._auth.start_auth.assert_awaited_once()
    assert not hasattr(cls, "auth_cookie_domains")


@pytest.mark.parametrize("cls", _STORES, ids=[c.__name__ for c in _STORES])
async def test_without_the_sign_in_browser_the_window_never_opens(cls: type[Any]) -> None:
    store = object.__new__(cls)
    store._auth = MagicMock(start_auth=AsyncMock())
    store._edge = MagicMock(is_installed=False)

    result = await store.start_auth()

    assert result.error == "edge_not_installed"
    store._auth.start_auth.assert_not_awaited()


def test_a_site_clear_matches_the_site_and_its_subdomains_only(tmp_path: Path) -> None:
    (tmp_path / "Default").mkdir()
    db = sqlite3.connect(tmp_path / "Default" / "Cookies")
    db.execute("CREATE TABLE cookies (host_key TEXT)")
    hosts = ["ea.com", ".ea.com", "signin.ea.com", "idea.com", ".korea.com", "gog.com"]
    db.executemany("INSERT INTO cookies VALUES (?)", [(h,) for h in hosts])
    db.commit()
    db.close()

    EdgeProfileManager(
        profile_dir=str(tmp_path), log_file=str(tmp_path / "log"),
        legacy_profile_dir=str(tmp_path / "old"), legacy_log_file=str(tmp_path / "old.log"),
        cookie_domain_patterns=[],
    ).clear_cookies_for_domain("ea.com")

    left = sorted(r[0] for r in sqlite3.connect(tmp_path / "Default" / "Cookies").execute("SELECT host_key FROM cookies"))
    assert left == [".korea.com", "gog.com", "idea.com"]
