"""The post-injection auth rebuild is one body, wired to four stores.

Epic, GOG, Amazon and Microsoft each carried their own
``_rebuild_auth_after_injection`` — statement-for-statement identical apart
from a store-name literal, a log prefix and the auth-flow constructor
(audit §3.4). They now share ``stores/shared/browser_auth_rebuild``.

What these tests protect:

* the injector's contract — it finds the hook by name on the instance, so
  the mixin has to actually reach the four stores;
* GOG's ``_after_auth_flow_built``, which is the one place consolidation
  could have dropped behaviour: without it ``_gogdl_bin`` stays empty,
  ``is_available`` refuses, and every GOG install dies at spawn;
* Ubisoft keeping its own, different hook rather than being swept in;
* Battle.net **not** gaining one, since the injector calls whatever it
  finds.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from unifideck.auth.orchestrator import AuthOrchestrator
from unifideck.stores.shared.browser_auth_rebuild import BrowserAuthRebuildMixin

_BROWSER_AUTH_STORES = [
    ("unifideck.stores.epic.store", "EpicStore"),
    ("unifideck.stores.gog.store", "GOGStore"),
    ("unifideck.stores.amazon.amazon_store", "AmazonStore"),
    ("unifideck.stores.microsoft.microsoft_store", "MicrosoftStore"),
]


def _load(module_path: str, class_name: str) -> type:
    import importlib

    return getattr(importlib.import_module(module_path), class_name)


class _Flow:
    def __init__(self, orchestrator: AuthOrchestrator) -> None:
        self.orchestrator = orchestrator


class _Store(BrowserAuthRebuildMixin):
    """Minimal consumer, standing in for a real store."""

    def __init__(self, monitor: object | None) -> None:
        self._bus = object()  # type: ignore[assignment]
        self._browser_monitor = monitor
        self._auth: Any = None
        self.after_calls = 0

    store_info = type("SI", (), {"name": "teststore"})()  # type: ignore[assignment]

    def _build_auth_flow(self, orchestrator: AuthOrchestrator) -> _Flow:
        return _Flow(orchestrator)

    def _after_auth_flow_built(self) -> None:
        self.after_calls += 1


# ── The shared body ────────────────────────────────────────────────────


def test_builds_the_flow_when_a_monitor_is_present() -> None:
    store = _Store(monitor=object())

    store._rebuild_auth_after_injection()

    assert isinstance(store._auth, _Flow)
    assert store.after_calls == 1


def test_store_name_comes_from_store_info() -> None:
    """Was a hardcoded literal in all four copies."""
    store = _Store(monitor=object())

    store._rebuild_auth_after_injection()

    assert store._auth.orchestrator._store == "teststore"


def test_no_monitor_leaves_auth_unbuilt() -> None:
    store = _Store(monitor=None)

    store._rebuild_auth_after_injection()

    assert store._auth is None
    assert store.after_calls == 0


def test_a_missing_monitor_attribute_is_survivable() -> None:
    """The copies all used ``getattr(..., None)``; auto-discovery can race."""
    store = _Store(monitor=None)
    del store._browser_monitor

    store._rebuild_auth_after_injection()

    assert store._auth is None


def test_is_idempotent() -> None:
    """The injector may call the hook more than once."""
    store = _Store(monitor=object())

    store._rebuild_auth_after_injection()
    first = store._auth
    store._rebuild_auth_after_injection()

    assert store._auth is first
    assert store.after_calls == 1


def test_after_hook_defaults_to_doing_nothing() -> None:
    class _Plain(_Store):
        _after_auth_flow_built = BrowserAuthRebuildMixin._after_auth_flow_built

    store = _Plain(monitor=object())
    store._rebuild_auth_after_injection()

    assert isinstance(store._auth, _Flow)
    assert store.after_calls == 0


def test_build_auth_flow_must_be_implemented() -> None:
    class _Incomplete(BrowserAuthRebuildMixin):
        def __init__(self) -> None:
            self._bus = object()  # type: ignore[assignment]
            self._browser_monitor = object()
            self._auth = None

        store_info = type("SI", (), {"name": "x"})()  # type: ignore[assignment]

    with pytest.raises(NotImplementedError):
        _Incomplete()._rebuild_auth_after_injection()


def test_log_prefix_names_the_concrete_store(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The four copies each hard-coded ``[EpicStore]``-style prefixes."""

    class NamedStore(_Store):
        pass

    with caplog.at_level(logging.INFO):
        NamedStore(monitor=object())._rebuild_auth_after_injection()

    assert "[NamedStore] auth flow wired" in caplog.text


# ── The wiring ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(("module_path", "class_name"), _BROWSER_AUTH_STORES)
def test_browser_auth_stores_use_the_mixin(
    module_path: str, class_name: str,
) -> None:
    store_cls = _load(module_path, class_name)

    assert issubclass(store_cls, BrowserAuthRebuildMixin)
    # The mixin must win the MRO, or StoreBase could shadow the hook.
    assert store_cls.__mro__.index(BrowserAuthRebuildMixin) < store_cls.__mro__.index(
        _load("unifideck.stores.shared.store_base", "StoreBase"),
    )
    # And none of them may keep a private copy of the body.
    assert "_rebuild_auth_after_injection" not in vars(store_cls)
    assert "_build_auth_flow" in vars(store_cls)


def test_only_gog_overrides_the_after_hook() -> None:
    """The single behavioural difference between the four copies."""
    overriding = {
        class_name
        for module_path, class_name in _BROWSER_AUTH_STORES
        if "_after_auth_flow_built" in vars(_load(module_path, class_name))
    }

    assert overriding == {"GOGStore"}


def test_ubisoft_keeps_its_own_different_hook() -> None:
    """Same injector hook, deliberately different body — not a fifth copy."""
    ubisoft = _load("unifideck.stores.ubisoft.store", "UbisoftStore")

    assert not issubclass(ubisoft, BrowserAuthRebuildMixin)
    assert "_rebuild_auth_after_injection" in vars(ubisoft)


def test_battlenet_has_no_rebuild_hook() -> None:
    """``store_injector`` calls whatever it finds, so this must stay absent."""
    battlenet = _load("unifideck.stores.battlenet.store", "BattlenetStore")

    assert not hasattr(battlenet, "_rebuild_auth_after_injection")
