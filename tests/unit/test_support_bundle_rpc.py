"""Tests for the ``capture_logs`` RPC and its service facade.

Instantiates the mixin directly against a minimal host, matching the
other RPC tests. Because ``@auto_wrap_rpc_methods`` is applied to the
``Plugin`` class rather than to the mixins, the coroutine under test is
unwrapped here — so these assert on the raw return value, not on the
``{success, error, data}`` envelope the frontend sees.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from unifideck.rpc.errors import RpcError
from unifideck.rpc.mixins.observability import ObservabilityRPCMixin
from unifideck.services.support_bundle import SupportBundleService


class _FakeBundleService:
    """Records how it was called and returns a canned payload."""

    def __init__(self, result: dict[str, Any] | None = None) -> None:
        self.result = result or {"archive_path": "/home/u/Downloads/b.zip"}
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    async def capture(
        self, dest_path: str = "", extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls.append((dest_path, extra))
        return self.result


class _RaisingService:
    """Raises whatever it was constructed with."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    async def capture(
        self, dest_path: str = "", extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        raise self.error


class _Host(ObservabilityRPCMixin):
    """Minimal stand-in for the composed Plugin class."""

    def __init__(self, services: Any) -> None:
        self.services = services
        self.bus = SimpleNamespace()


#: Blocks ``_support_bundle_extra`` contributes unconditionally — they read
#: the plugin instance rather than an optional service, so they are present
#: even on a bare host. Tests asserting "no optional context" check against
#: this set rather than against ``{}``.
ALWAYS_PRESENT = {"bus_health", "config_validation"}


def _host(service: Any, **extra: Any) -> _Host:
    """Build a host whose container exposes ``support_bundle``."""
    return _Host(SimpleNamespace(support_bundle=service, **extra))


# ── happy path ────────────────────────────────────────────────────
async def test_returns_the_service_payload_unwrapped() -> None:
    service = _FakeBundleService({"archive_path": "/x/b.zip", "bytes": 42})
    result = await _host(service).capture_logs()
    assert result == {"archive_path": "/x/b.zip", "bytes": 42}


async def test_destination_override_is_forwarded() -> None:
    service = _FakeBundleService()
    await _host(service).capture_logs("/run/media/deck/SD")
    assert service.calls[0][0] == "/run/media/deck/SD"


# ── error mapping ─────────────────────────────────────────────────
async def test_missing_service_raises_service_unavailable() -> None:
    host = _Host(SimpleNamespace())
    with pytest.raises(RpcError) as caught:
        await host.capture_logs()
    assert caught.value.code == "service_unavailable"
    assert caught.value.context["service"] == "support_bundle"


async def test_unwritable_destination_gets_its_own_code() -> None:
    """The one failure a user can actually act on.

    It has to be distinguishable from a generic crash so the UI can say
    something more useful than "capture failed".
    """
    service = _RaisingService(OSError("no writable destination; tried [...]"))
    with pytest.raises(RpcError) as caught:
        await _host(service).capture_logs()
    assert caught.value.code == "bundle_dest_unwritable"
    assert "writable" in caught.value.context["detail"]


async def test_unexpected_failure_becomes_bundle_failed() -> None:
    service = _RaisingService(ValueError("something odd"))
    with pytest.raises(RpcError) as caught:
        await _host(service).capture_logs()
    assert caught.value.code == "bundle_failed"
    assert "something odd" in caught.value.context["detail"]


async def test_typed_errors_pass_through_unchanged() -> None:
    """A typed error must not be swallowed into the generic one."""
    service = _RaisingService(RpcError("already_typed", detail="keep me"))
    with pytest.raises(RpcError) as caught:
        await _host(service).capture_logs()
    assert caught.value.code == "already_typed"


# ── the extra payload ─────────────────────────────────────────────
async def test_feature_flags_are_folded_in() -> None:
    """Facts only this layer can see reach the environment report."""
    service = _FakeBundleService()
    flags = SimpleNamespace(get_flags=lambda: {"beta": True})
    host = _host(service, feature_flags=flags)
    await host.capture_logs()
    extra = service.calls[0][1] or {}
    assert extra["feature_flags"] == {"beta": True}


async def test_runtime_probes_key_is_gone() -> None:
    """``report_runtime_probes`` was the only writer and had no caller.

    The key it fed is deliberately absent rather than always-empty — see
    ``_support_bundle_extra``. Setting the old attribute must not
    resurrect it.
    """
    service = _FakeBundleService()
    host = _host(service)
    host.runtime_probes = [{"name": "cef", "severity": "info"}]
    await host.capture_logs()
    assert "runtime_probes" not in (service.calls[0][1] or {})


async def test_bus_health_is_folded_in() -> None:
    """The retired ``get_bus_health`` route's payload now rides the bundle."""
    service = _FakeBundleService()
    host = _host(service)
    host.bus = SimpleNamespace(_handlers={"sync_complete": [1, 2], "boot": [3]})
    await host.capture_logs()
    health = (service.calls[0][1] or {})["bus_health"]
    assert health["bus"]["events_registered"] == 2
    assert health["bus"]["handler_counts"]["sync_complete"] == 2


async def test_security_snapshot_is_folded_in() -> None:
    """The five retired security routes' payloads now ride the bundle."""
    service = _FakeBundleService()
    security = SimpleNamespace(
        get_counters=lambda: {"SECURITY_AUTH_FLOW_FAILED": 2},
        get_bruteforce_status=lambda: {"escalated": False, "recent_failures": 1},
        get_audit_log=lambda limit: [{"event": "SECURITY_AUTH_FLOW_FAILED"}],
    )
    host = _host(service, security=security)
    await host.capture_logs()
    block = (service.calls[0][1] or {})["security"]
    assert block["counters"]["SECURITY_AUTH_FLOW_FAILED"] == 2
    assert block["bruteforce"]["escalated"] is False
    assert block["audit_log"][0]["event"] == "SECURITY_AUTH_FLOW_FAILED"


async def test_a_broken_security_service_does_not_cost_the_bundle() -> None:
    """Same contract as flags/metrics: optional context, never fatal."""
    def _explode() -> dict[str, Any]:
        raise RuntimeError("security service down")

    service = _FakeBundleService()
    host = _host(service, security=SimpleNamespace(
        get_counters=_explode, get_bruteforce_status=dict, get_audit_log=lambda limit: [],
    ))
    result = await host.capture_logs()
    assert result["archive_path"]
    assert "security" not in (service.calls[0][1] or {})


async def test_config_validation_never_carries_the_offending_value() -> None:
    """The bundle is pasted in public; jsonschema messages are not.

    ``ValidationError.message`` interpolates the user's own config value
    (``"'sk-abc' is not of type 'integer'"``), so only ``safe_message``
    — built from the schema side — may reach the bundle.
    """
    service = _FakeBundleService()
    secret = "sk-SECRET-abc123"  # noqa: S105 — test fixture, not a credential
    err = SimpleNamespace(
        source="user_overrides",
        path="stores.epic.client_id",
        message=f"'{secret}' is not of type 'integer'",
        safe_message="type != integer",
    )
    host = _host(service)
    host._config_validation_result = SimpleNamespace(errors=[err], warnings=[])
    host._config_degraded = True
    await host.capture_logs()
    block = (service.calls[0][1] or {})["config_validation"]

    assert block["degraded"] is True
    assert block["error_count"] == 1
    assert block["errors"][0]["path"] == "stores.epic.client_id"
    assert block["errors"][0]["safe_message"] == "type != integer"
    # The whole point: neither the raw message nor the value it quotes.
    assert secret not in str(block)
    assert "message" not in block["errors"][0]


async def test_config_validation_block_is_clean_when_config_is_valid() -> None:
    """A healthy boot still reports, so "no errors" is distinguishable
    from "never ran"."""
    service = _FakeBundleService()
    await _host(service).capture_logs()
    block = (service.calls[0][1] or {})["config_validation"]
    assert block == {
        "degraded": False, "error_count": 0, "warning_count": 0, "errors": [],
    }


async def test_plugin_metrics_reach_the_environment_report() -> None:
    """Counters/timers/gauges are in-memory only, so the collector can't
    read them off disk — the RPC layer has to hand them over."""
    service = _FakeBundleService()
    snapshot = {
        "counters": {"auth_attempts": 3},
        "timers_ms": {"sync_duration_ms": 812.4},
        "gauges": {"sync_games_total": 214.0},
        "uptime_s": 90,
    }
    host = _host(service, metrics=SimpleNamespace(get_plugin_metrics=lambda: snapshot))
    await host.capture_logs()
    extra = service.calls[0][1] or {}
    assert extra["plugin_metrics"]["timers_ms"]["sync_duration_ms"] == 812.4
    assert extra["plugin_metrics"]["counters"]["auth_attempts"] == 3


async def test_a_broken_metrics_service_does_not_cost_the_bundle() -> None:
    """Same contract as the flag service: optional context, never fatal."""
    def _explode() -> dict[str, Any]:
        raise RuntimeError("metrics service down")

    service = _FakeBundleService()
    host = _host(service, metrics=SimpleNamespace(get_plugin_metrics=_explode))
    result = await host.capture_logs()
    assert result["archive_path"]
    assert set(service.calls[0][1] or {}) == ALWAYS_PRESENT


async def test_extra_carries_only_the_unconditional_blocks_on_a_bare_host() -> None:
    service = _FakeBundleService()
    await _host(service).capture_logs()
    assert set(service.calls[0][1] or {}) == ALWAYS_PRESENT


async def test_a_broken_flag_service_does_not_cost_the_bundle() -> None:
    """Optional context must never be able to fail the capture."""
    def _explode() -> dict[str, Any]:
        raise RuntimeError("flag service down")

    service = _FakeBundleService()
    host = _host(service, feature_flags=SimpleNamespace(get_flags=_explode))
    result = await host.capture_logs()
    assert result["archive_path"]
    assert set(service.calls[0][1] or {}) == ALWAYS_PRESENT


# ── service facade ────────────────────────────────────────────────
async def test_concurrent_capture_is_refused_not_duplicated() -> None:
    """Two taps must not build two archives or race on the filename."""
    service = SupportBundleService(config=None, paths=None)
    await service._lock.acquire()
    try:
        result = await service.capture()
    finally:
        service._lock.release()
    assert result["in_progress"] is True
    assert result["archive_path"] is None


async def test_capture_runs_the_collector_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Blocking file I/O must be thread-hopped, not inline."""
    seen: dict[str, Any] = {}

    def _fake_capture(
        dest_path: str, config: Any, paths: Any, extra: Any,
    ) -> dict[str, Any]:
        seen["dest_path"] = dest_path
        seen["extra"] = extra
        return {"archive_path": "/x/b.zip"}

    monkeypatch.setattr(
        "unifideck.services.support_bundle.collect.capture_bundle", _fake_capture,
    )
    service = SupportBundleService(config=None, paths=None)
    result = await service.capture("/tmp/dest", extra={"a": 1})
    assert result["archive_path"] == "/x/b.zip"
    assert seen["dest_path"] == "/tmp/dest"
    assert seen["extra"] == {"a": 1}


async def test_lock_is_released_after_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed capture must not wedge the button forever."""
    def _explode(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise OSError("nowhere writable")

    monkeypatch.setattr(
        "unifideck.services.support_bundle.collect.capture_bundle", _explode,
    )
    service = SupportBundleService(config=None, paths=None)
    with pytest.raises(OSError, match="nowhere writable"):
        await service.capture()
    assert not service._lock.locked()
