"""``UNIFIDECK_<STORE>_ACTION=storefront`` must reach the storefront handler.

The cart button in QAM → Store Connections launches a temporary Steam
shortcut carrying this token. It is the third non-launch action, joining
``auth`` and ``install``, and the two existing ones must be untouched by
its arrival — hence the regression half of this file.

Unlike ``install``, ``storefront`` is valid for EVERY store: they all
have a shop. Which *kind* of shop (a web page in Edge, or the vendor
client's own Store tab) is decided later by
``LauncherService._handle_auth_path`` off ``is_wrapper_store``, so what
matters here is only that the context carries the right ``auth_store``.
"""
from __future__ import annotations

import pytest

from unifideck.launcher import dispatcher as d

_BROWSER_STORES = ("epic", "gog", "amazon", "microsoft")
_WRAPPER_STORES = ("ubisoft", "battlenet")


class _FakeShortcutSvc:
    """No games.map row: a shop has no game, so none is ever looked up."""

    async def get_entry_for_game_key(self, _store: str, _game_id: str) -> None:
        return None


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    for store in ("EPIC", "GOG", "AMAZON", "MICROSOFT", "UBISOFT", "BATTLENET"):
        monkeypatch.delenv(f"UNIFIDECK_{store}_ACTION", raising=False)
    monkeypatch.setattr(d, "_resolve_exe_from_install", lambda *a, **k: None)
    monkeypatch.setattr(d, "_install_path_from_cache", lambda *a, **k: "")
    monkeypatch.setattr(d, "_resolve_plugin_dir", lambda: tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize("store", [*_BROWSER_STORES, *_WRAPPER_STORES])
async def test_storefront_action_builds_a_storefront_context(
    monkeypatch, store: str,
) -> None:
    """Every store, browser-OAuth and wrapper alike, gets a shop context."""
    monkeypatch.setenv(f"UNIFIDECK_{store.upper()}_ACTION", "storefront")

    ctx = await d._build_context(
        ["launcher", f"{store}:{store}-store"], _FakeShortcutSvc(),
    )

    assert ctx.is_launch_action is False, "a shop is not a game launch"
    assert ctx.action == "storefront"
    # The handler is selected off auth_store. For a wrapper store the wrong
    # value here would open the other vendor's client entirely.
    assert ctx.auth_store == store


@pytest.mark.asyncio
async def test_storefront_is_not_wrapper_gated(monkeypatch) -> None:
    """Contrast with ``install``, which IS wrapper-only.

    ``install`` for a non-wrapper store falls through to the launch path
    and raises; ``storefront`` must not, because Epic has a shop even
    though it has no vendor client.
    """
    monkeypatch.setenv("UNIFIDECK_EPIC_ACTION", "storefront")

    ctx = await d._build_context(["launcher", "epic:epic-store"], _FakeShortcutSvc())

    assert ctx.action == "storefront"


# ── Regressions: the two pre-existing actions are unchanged ─────────


@pytest.mark.asyncio
@pytest.mark.parametrize("store", [*_BROWSER_STORES, *_WRAPPER_STORES])
async def test_auth_action_still_builds_an_auth_context(
    monkeypatch, store: str,
) -> None:
    monkeypatch.setenv(f"UNIFIDECK_{store.upper()}_ACTION", "auth")

    ctx = await d._build_context(
        ["launcher", f"{store}:{store}-auth"], _FakeShortcutSvc(),
    )

    assert ctx.action == "auth"
    assert ctx.auth_store == store


@pytest.mark.asyncio
async def test_install_is_still_wrapper_gated(monkeypatch) -> None:
    monkeypatch.setenv("UNIFIDECK_BATTLENET_ACTION", "install")

    ctx = await d._build_context(["launcher", "battlenet:s1"], _FakeShortcutSvc())

    assert ctx.action == "install"
    assert ctx.auth_store == "battlenet"


@pytest.mark.asyncio
async def test_install_still_refused_for_a_non_wrapper_store(monkeypatch) -> None:
    monkeypatch.setenv("UNIFIDECK_EPIC_ACTION", "install")

    with pytest.raises(d.GameNotFoundError):
        await d._build_context(["launcher", "epic:abc123"], _FakeShortcutSvc())


@pytest.mark.asyncio
async def test_an_unknown_action_still_means_a_normal_launch(monkeypatch) -> None:
    """The fallthrough must survive a third branch being added above it.

    An unrecognised token is not a non-launch action, so the dispatcher
    goes looking for a games.map row and finds none.
    """
    monkeypatch.setenv("UNIFIDECK_EPIC_ACTION", "bogus")

    with pytest.raises(d.GameNotFoundError):
        await d._build_context(["launcher", "epic:abc123"], _FakeShortcutSvc())


def test_detect_special_action_reports_storefront(monkeypatch) -> None:
    """Pin the tuple the branch returns, not just its downstream effect."""
    monkeypatch.setenv("UNIFIDECK_GOG_ACTION", "storefront")

    assert d._detect_special_action() == ("gog", "storefront", False)
