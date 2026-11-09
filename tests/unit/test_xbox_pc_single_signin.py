"""Connect Microsoft = the Xbox PC sign-in (one sign-in, one window).

``start_connect`` waits for the components, starts the Xodus sign-in in
connect mode, and reports its end as the Microsoft store's sign-in events,
which is what the frontend's normal Connect flow waits for.
"""
from __future__ import annotations

import asyncio
from typing import Any

from unifideck.core.types import Events
from unifideck.stores.microsoft.xodus import single_signin


class FakeBus:
    def __init__(self) -> None:
        self.events: list[tuple[Any, dict[str, Any]]] = []

    async def emit(self, event: Any, **kwargs: Any) -> None:
        self.events.append((event, kwargs))


class FakeFacade:
    def __init__(self, runtime: str = "installed", pending: bool = True) -> None:
        self.runtime = runtime
        self.pending = pending
        self.sign_in_listeners: list[Any] = []
        self.started: list[bool] = []
        self.installs = 0

    async def refresh_components(self) -> dict[str, Any]:
        return await self.status()

    async def status(self) -> dict[str, Any]:
        return {"runtime": {"state": self.runtime}}

    async def install_components(self) -> dict[str, Any]:
        self.installs += 1
        self.runtime = "installing"
        asyncio.get_running_loop().call_later(0.05, setattr, self, "runtime", "installed")
        return {"started": True, "reason": None}

    async def start_sign_in(self, *, connect: bool = False) -> dict[str, Any]:
        self.started.append(connect)
        return {"pending": self.pending, "launch_shortcut": "microsoft", "reason": None if self.pending else "edge_busy"}

    def end(self, account: dict[str, Any]) -> None:
        for listener in list(self.sign_in_listeners):
            listener(account)


async def test_connect_starts_the_xodus_sign_in_and_reports_success() -> None:
    facade, bus = FakeFacade(), FakeBus()
    result = await single_signin.start_connect(facade, bus)  # type: ignore[arg-type]
    assert result.success and result.metadata == {"pending": True, "flow": "xbox_pc"}
    assert facade.started == [True]
    facade.end({"state": "signed_in", "gamertag": "Gamer"})
    await asyncio.sleep(0)
    assert bus.events == [(Events.STORE_AUTH_COMPLETE, {"store": "microsoft"})]
    assert facade.sign_in_listeners == [], "one-shot"


async def test_a_failed_sign_in_is_a_failed_microsoft_sign_in() -> None:
    facade, bus = FakeFacade(), FakeBus()
    await single_signin.start_connect(facade, bus)  # type: ignore[arg-type]
    facade.end({"state": "signed_out", "error": "window_closed"})
    await asyncio.sleep(0)
    assert bus.events == [(Events.STORE_AUTH_FAILED, {"store": "microsoft", "error": "window_closed"})]


async def test_connect_waits_for_the_components_first() -> None:
    facade, bus = FakeFacade(runtime="missing"), FakeBus()
    result = await single_signin.start_connect(facade, bus)  # type: ignore[arg-type]
    assert result.success and facade.installs == 1 and facade.started == [True]


async def test_a_refused_start_is_reported_at_once() -> None:
    facade, bus = FakeFacade(pending=False), FakeBus()
    result = await single_signin.start_connect(facade, bus)  # type: ignore[arg-type]
    assert not result.success and result.error == "edge_busy"
    assert facade.sign_in_listeners == []
