"""Every user-facing toast goes out on the one channel the UI consumes.

Three emitters — the circuit breaker, the launcher-error catch-all, and the
shortcut write-refusal guard — spent their whole lifetime emitting
``TOAST_NOTIFICATION``, an event with no subscriber in either process. So a
game blocked by the circuit breaker, a launch that died on a ``LauncherError``,
and a sync that declined to touch ``shortcuts.vdf`` were all completely silent.

These tests assert against the *consumer's* contract, not the emitter's. That
distinction is the whole lesson of the ``GAME_INSTALLED`` audit item: the two
guards that existed there each locked in one half of a payload mismatch, so
between them they described a working system and caught nothing. Concretely,
here that means:

* the event name must be the one ``frontend_bridge`` forwards and
  ``WATCHED_EVENTS`` polls, not merely "some event";
* the params key must be ``i18n_params``, the name both renderers read —
  the old emitters said ``params`` and would have rendered
  "Couldn't launch  ()." even with delivery fixed;
* the ``i18n_key`` must exist in ``en-US.json``, because a key the locale
  file lacks renders as the raw key string.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from unifideck.core.types import Events

REPO_ROOT = Path(__file__).resolve().parents[2]
EN_LOCALE = REPO_ROOT / "src" / "i18n" / "locales" / "en-US.json"


class _FakeBus:
    """Records every ``emit`` so tests can assert what reached the bus."""

    def __init__(self) -> None:
        self.emitted: list[tuple[Any, dict[str, Any]]] = []

    async def emit(self, event: Any, **kwargs: Any) -> None:
        self.emitted.append((event, kwargs))


class _FakeCtx:
    """The two fields ``LaunchContext`` exposes to a toast emitter."""

    store = "battlenet"
    game_id = "fenris"

    @property
    def game_key(self) -> str:
        return f"{self.store}:{self.game_id}"


class _FakeLauncherService:
    """``svc._bus`` is the only attribute the toast emitters touch."""

    def __init__(self, bus: _FakeBus) -> None:
        self._bus = bus
        self._launch_history = None


def _lookup(locale: dict[str, Any], dotted_key: str) -> Any:
    """Resolve ``a.b.c`` against the nested locale dict, or None."""
    node: Any = locale
    for part in dotted_key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


@pytest.fixture(scope="module")
def en_locale() -> dict[str, Any]:
    return json.loads(EN_LOCALE.read_text(encoding="utf-8"))


@pytest.fixture
def bus() -> _FakeBus:
    return _FakeBus()


def _sole_emit(bus: _FakeBus) -> dict[str, Any]:
    """The kwargs of the one LAUNCHER_STAGE event the emitter produced."""
    assert len(bus.emitted) == 1, f"expected one emit, got {bus.emitted!r}"
    event, kwargs = bus.emitted[0]
    assert event is Events.LAUNCHER_STAGE
    return kwargs


# ── the retired channel ──────────────────────────────────────────


def test_toast_notification_no_longer_exists() -> None:
    """The dead channel is gone, so nothing can emit into it again.

    Deleting the member rather than leaving it unused is deliberate: an
    ``Events.X`` that resolves is an invitation to emit, and the emit
    looks correct at the call site whether or not anything listens.
    """
    assert not hasattr(Events, "TOAST_NOTIFICATION")
    assert "toast_notification" not in {e.value for e in Events}


# ── circuit breaker ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_circuit_open_toast_reaches_the_live_channel(
    bus: _FakeBus, en_locale: dict[str, Any],
) -> None:
    from unifideck.services.launcher.circuit_breaker import emit_circuit_open_toast

    await emit_circuit_open_toast(_FakeLauncherService(bus), _FakeCtx(), 3)

    kwargs = _sole_emit(bus)
    assert kwargs["i18n_key"] == "toasts.launcher.errorCircuitBreakerOpen"
    assert _lookup(en_locale, kwargs["i18n_key"]) is not None
    assert kwargs["severity"] == "error"
    assert "params" not in kwargs, "the renderers read i18n_params, not params"
    assert kwargs["i18n_params"]["count"] == 3


@pytest.mark.asyncio
async def test_circuit_open_toast_names_the_game(
    bus: _FakeBus, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The message says "Diablo IV", not "battlenet:fenris".

    The string interpolates ``{{game_key}}``, so the resolved display title
    is fed into that placeholder — a nicer toast for zero locale churn.
    """
    registry = tmp_path / "shortcuts_registry.json"
    registry.write_text(
        json.dumps({"battlenet:fenris": {"appid": -1, "title": "Diablo IV"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "unifideck.launcher.game_title.REGISTRY_PATH", registry,
    )

    from unifideck.services.launcher.circuit_breaker import emit_circuit_open_toast

    await emit_circuit_open_toast(_FakeLauncherService(bus), _FakeCtx(), 3)

    kwargs = _sole_emit(bus)
    assert kwargs["i18n_params"]["game_key"] == "Diablo IV"
    assert kwargs["game_title"] == "Diablo IV"


@pytest.mark.asyncio
async def test_circuit_open_toast_falls_back_to_the_launch_key(
    bus: _FakeBus, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No registry entry costs a nicer name, never the toast."""
    monkeypatch.setattr(
        "unifideck.launcher.game_title.REGISTRY_PATH", tmp_path / "absent.json",
    )

    from unifideck.services.launcher.circuit_breaker import emit_circuit_open_toast

    await emit_circuit_open_toast(_FakeLauncherService(bus), _FakeCtx(), 3)

    assert _sole_emit(bus)["i18n_params"]["game_key"] == "battlenet:fenris"


# ── launcher error ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_launcher_error_toast_reaches_the_live_channel(
    bus: _FakeBus, en_locale: dict[str, Any],
) -> None:
    from unifideck.services.launcher.error_toasts import emit_launcher_error_toast

    await emit_launcher_error_toast(
        _FakeLauncherService(bus), _FakeCtx(), "prefix_init_failed",
    )

    kwargs = _sole_emit(bus)
    assert kwargs["i18n_key"] == "toasts.launcher.launcherError"
    assert _lookup(en_locale, kwargs["i18n_key"]) is not None
    assert kwargs["severity"] == "error"
    assert "params" not in kwargs
    assert kwargs["i18n_params"]["error_code"] == "prefix_init_failed"


# ── shortcut write refusal ───────────────────────────────────────


@pytest.mark.asyncio
async def test_write_refused_toast_reaches_the_live_channel(
    bus: _FakeBus, tmp_path: Path, en_locale: dict[str, Any],
) -> None:
    from unifideck.services.shortcut.service import ShortcutService

    svc = ShortcutService(
        bus,  # type: ignore[arg-type]  # _FakeBus implements the one method used
        shortcuts_path=str(tmp_path / "shortcuts.vdf"),
        games_map_path=str(tmp_path / "games.map"),
    )
    await svc._emit_write_refused("foreign_entry_would_drop")

    kwargs = _sole_emit(bus)
    assert kwargs["i18n_key"] == "toasts.shortcuts.writeRefused"
    assert _lookup(en_locale, kwargs["i18n_key"]) is not None
    assert "params" not in kwargs
    assert kwargs["i18n_params"]["reason"] == "foreign_entry_would_drop"
    # The string is a full paragraph; the renderers' 7.5s error default
    # cuts the read short, so this one asks for longer explicitly.
    assert kwargs["duration_ms"] == 12000


# ── the cross-process leg ────────────────────────────────────────


@pytest.mark.asyncio
async def test_forwarder_mirrors_launcher_stage_to_the_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LAUNCHER_STAGE — and only it — crosses out of the launcher process.

    The two launcher toasts run in the out-of-process launcher, whose bus
    dies with the process. ``install_bus_forwarder`` is the entire reason
    they reach the UI at all, and it forwards exactly one event name.
    """
    from unifideck.event_bus import EventBus
    from unifideck.launcher import frontend_bridge
    from unifideck.launcher.frontend_bridge import install_bus_forwarder
    from unifideck.launcher.rpc import emit_stage

    events_file = tmp_path / "launcher_events.jsonl"
    monkeypatch.setattr(frontend_bridge, "EVENTS_FILE", events_file)

    bus = EventBus()
    install_bus_forwarder(bus)
    await emit_stage(
        bus,
        i18n_key="toasts.launcher.errorCircuitBreakerOpen",
        game_title="Diablo IV",
        severity="error",
        duration_ms=10000,
        i18n_params={"game_key": "Diablo IV", "count": 3},
    )

    lines = [
        json.loads(line)
        for line in events_file.read_text(encoding="utf-8").splitlines()
        if line
    ]
    assert len(lines) == 1
    assert lines[0]["event"] == "launcher_stage"
    assert lines[0]["kwargs"]["i18n_params"]["count"] == 3
    assert lines[0]["kwargs"]["duration_ms"] == 10000
