"""How the Luna play activator starts a title (verified on a Steam Deck).

* The trigger targets only the detail page's primary Play button. A generic
  ``[data-test-id*="play"]`` match used to hit the nav's "Remote Play" entry.
* Luna ignores a scripted ``element.click()``: the button is clicked with
  trusted ``Input.dispatchMouseEvent`` events at its centre.
* Luna may show a "Hardware Acceleration is Off" dialog after Play; the
  activator confirms it (Continue Anyway) and Luna then provisions.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import unifideck.stores.luna.luna_play_activator as activator_mod
from unifideck.stores.luna.luna_play_activator import (
    _CONTINUE_LOCATE_EXPRESSION,
    _PLAY_LOCATE_EXPRESSION,
    LunaPlayActivator,
    click_located,
)

_SID = "page-session"
_PLAY_XY = (194.0, 356.0)
_CONTINUE_XY = (830.0, 500.0)


class _Session:
    """Scripted Luna page: Play → (optional dialog → Continue) → player."""

    def __init__(self, *, dialog_on_play: bool = False, button: bool = True) -> None:
        self.dialog_on_play = dialog_on_play
        self.button = button
        self.dialog = False
        self.started = False
        self.events: list[tuple[str, float, float]] = []

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        params = params or {}
        if method == "Input.dispatchMouseEvent":
            self._pointer(params["type"], params["x"], params["y"])
            return {}
        return self._evaluate(params.get("expression", ""))

    def _pointer(self, kind: str, x: float, y: float) -> None:
        self.events.append((kind, x, y))
        if kind != "mouseReleased":
            return
        if (x, y) == _CONTINUE_XY and self.dialog:
            self.dialog, self.started = False, True
        elif (x, y) == _PLAY_XY:
            if self.dialog_on_play:
                self.dialog = True
            else:
                self.started = True

    def _evaluate(self, expr: str) -> dict[str, Any]:
        if expr == _PLAY_LOCATE_EXPRESSION:
            return {"result": {"value": list(_PLAY_XY) if self.button else None}}
        if expr == _CONTINUE_LOCATE_EXPRESSION:
            return {"result": {"value": list(_CONTINUE_XY) if self.dialog else None}}
        if "onPlayer" in expr:
            return {"result": {"value": {"onPlayer": self.started,
                                         "playing": self.started}}}
        return {"result": {}}

    def attached_targets(self, idle_timeout: float | None = 2.0) -> Any:
        raise NotImplementedError


def _activator() -> LunaPlayActivator:
    return LunaPlayActivator(SimpleNamespace(player_path="/player"))  # type: ignore[arg-type]


def test_locate_targets_only_the_primary_play_button() -> None:
    assert "primary_entitled_button_game_session_async" in _PLAY_LOCATE_EXPRESSION
    # No catch-all that would match the nav's "Remote Play" entry.
    assert 'data-test-id*="play"' not in _PLAY_LOCATE_EXPRESSION
    assert "includes('play')" not in _PLAY_LOCATE_EXPRESSION


async def test_click_is_a_trusted_pointer_sequence_at_the_button_centre() -> None:
    session = _Session()
    assert await click_located(session, _SID, _PLAY_LOCATE_EXPRESSION) is True
    assert [e[0] for e in session.events] == [
        "mouseMoved", "mousePressed", "mouseReleased",
    ]
    assert {e[1:] for e in session.events} == {_PLAY_XY}


async def test_absent_button_dispatches_nothing() -> None:
    session = _Session(button=False)
    assert await click_located(session, _SID, _PLAY_LOCATE_EXPRESSION) is False
    assert session.events == []


async def test_activation_without_dialog_reaches_player(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(activator_mod, "_CONFIRM_POLL_INTERVAL_S", 0)
    session = _Session()
    result = await _activator().run_activate(
        session, _SID, "https://luna.amazon.de/game/x/B0", "/player",
    )
    assert result.success is True


async def test_activation_confirms_hardware_acceleration_dialog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(activator_mod, "_CONFIRM_POLL_INTERVAL_S", 0)
    session = _Session(dialog_on_play=True)
    result = await _activator().run_activate(
        session, _SID, "https://luna.amazon.de/game/x/B0", "/player",
    )
    assert result.success is True
    released = [e[1:] for e in session.events if e[0] == "mouseReleased"]
    assert released == [_PLAY_XY, _CONTINUE_XY]
