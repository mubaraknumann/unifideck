"""The post-play capture waits for UPC to exit before reading its vault.

``shared/wrapper_session_hooks`` already knew this: the vendor client
flushes its rotated token *as it shuts down*, so reading too early gets a
torn vault. Battle.net inherited that wait; Ubisoft, which keeps its own
richer session facade, never had it and read the instant ``GAME_STOPPED``
fired.

The downstream cost is what makes it worth a wait: Ubisoft's capture is
followed by ``propagate_all_to_all``, so a single torn read is copied into
*every* Ubisoft prefix. Its own guard is size-only
(``_is_valid_css``), which a long-enough partial write passes.

The bound matters as much as the wait. ``GAME_STOPPED`` also drives playtime
recording and temp-shortcut removal, so a client that refuses to die must
not hold the handler open indefinitely.
"""

from __future__ import annotations

import asyncio

from typing import Any
from unittest.mock import MagicMock

import pytest

from unifideck.stores.shared import wrapper_session_hooks as hooks
from unifideck.stores.ubisoft import store as ubi_store


def _store() -> Any:
    s = ubi_store.UbisoftStore.__new__(ubi_store.UbisoftStore)
    s._paths = MagicMock()
    s._paths.get_prefix_path.return_value = "/prefix/ubisoft/83"
    s._session = MagicMock()
    s._session.capture.return_value = "captured"
    return s


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the bound's arithmetic without paying it in wall-clock."""

    async def _instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr(hooks.asyncio, "sleep", _instant)


def test_the_capture_waits_while_upc_is_still_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Alive for three polls, then gone — the capture happens after."""
    alive = iter([True, True, True, False])
    order: list[str] = []

    def _running(_store: str, _prefix: Any) -> bool:
        result = next(alive, False)
        order.append(f"probe:{result}")
        return result

    monkeypatch.setattr(hooks, "client_running_in", _running)
    s = _store()
    s._session.capture.side_effect = lambda _p: order.append("capture") or "ok"

    asyncio.run(s._capture_upc_session_on_stop(store="ubisoft", game_id="83"))

    assert order == ["probe:True", "probe:True", "probe:True", "probe:False",
                     "capture"]


def test_a_client_that_never_exits_does_not_hold_the_handler_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bounded, then captures anyway — UPC minimises to tray and lingers.

    Giving up on the capture entirely would be worse than a torn read: the
    rotated token would never come back at all.
    """
    probes = 0

    def _always_running(_store: str, _prefix: Any) -> bool:
        nonlocal probes
        probes += 1
        return True

    monkeypatch.setattr(hooks, "client_running_in", _always_running)
    s = _store()

    asyncio.run(s._capture_upc_session_on_stop(store="ubisoft", game_id="83"))

    assert probes == int(hooks._EXIT_WAIT_SECONDS / hooks._EXIT_POLL_SECONDS)
    s._session.capture.assert_called_once_with("/prefix/ubisoft/83")
    s._session.propagate_all_to_all.assert_called_once()


def test_no_wait_when_upc_is_already_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The common case — a clean quit — costs one probe."""
    probes = 0

    def _gone(_store: str, _prefix: Any) -> bool:
        nonlocal probes
        probes += 1
        return False

    monkeypatch.setattr(hooks, "client_running_in", _gone)
    s = _store()

    asyncio.run(s._capture_upc_session_on_stop(store="ubisoft", game_id="83"))

    assert probes == 1
    s._session.capture.assert_called_once()


def test_a_non_ubisoft_stop_never_probes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The store guard runs before the wait, not after.

    Otherwise every GOG or Epic game stop would pay a /proc scan.
    """
    probes = 0

    def _probe(_store: str, _prefix: Any) -> bool:
        nonlocal probes
        probes += 1
        return False

    monkeypatch.setattr(hooks, "client_running_in", _probe)
    s = _store()

    asyncio.run(s._capture_upc_session_on_stop(store="gog", game_id="123"))

    assert probes == 0
    s._session.capture.assert_not_called()


def test_ubisoft_probes_for_upc_not_for_some_other_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The store id is what selects the process images to look for.

    Passing the wrong one is the ``app_id``/``game_id`` failure class: the
    probe returns False immediately and the wait silently does nothing.
    """
    seen: list[tuple[str, str]] = []

    def _probe(store: str, prefix: Any) -> bool:
        seen.append((store, str(prefix)))
        return False

    monkeypatch.setattr(hooks, "client_running_in", _probe)
    s = _store()

    asyncio.run(s._capture_upc_session_on_stop(store="ubisoft", game_id="83"))

    assert seen == [("ubisoft", "/prefix/ubisoft/83")]


def test_the_shared_probe_knows_upc() -> None:
    """``CLIENT_IMAGES`` must carry a ubisoft row for any of this to work.

    Without it ``client_running_in`` returns False for every prefix and the
    wait is a no-op that reads as wired.
    """
    from unifideck.launcher.proton.handlers import wrapper_clients

    images = wrapper_clients.CLIENT_IMAGES["ubisoft"]
    assert "upc.exe" in images
