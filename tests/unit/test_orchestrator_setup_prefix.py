"""Regression: first launch runs the canonical ``setup_prefix`` in Phase 1.5.

The install-time warmup and the first-launch path used to run two different
prefix-setup implementations. Warmup had the managed-GE recovery ladder and (now)
pins the winning Proton; the launch path ran only ``ensure_prefix_initialized``
in the orchestrator plus a bare ``apply_prefix_compat`` inside ``proton.dispatch``
with NO recovery — so a warmup that recovered to GE was undone at Play time.

These tests pin the unification:
  1. ``launch_windows`` calls ``setup_prefix`` (the ONE shared process) in
     Phase 1.5, before the cloud sync-down; and
  2. ``proton.dispatch`` no longer calls ``apply_prefix_compat`` (it moved into
     ``setup_prefix``, so it must not run twice per launch).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from unifideck.launcher import proton as proton_pkg
from unifideck.services.launcher import orchestrator as orch


class _Ctx(SimpleNamespace):
    pass


def _ctx():
    return _Ctx(store="epic", game_id="g1", game_key="epic:g1", steam_app_id=None)


def _fake_service():
    svc = MagicMock()
    plan = SimpleNamespace()
    svc._prepare_windows_plan = AsyncMock(return_value=plan)
    svc._cloud_sync_phase = AsyncMock()
    svc._run_game_subprocess = AsyncMock(return_value=0)
    svc._resolve_exit_code = MagicMock(return_value=0)
    svc._bus = MagicMock(emit=AsyncMock())
    return svc


def _setup_returning(order: list[str], tool: str = "proton_experimental"):
    """A ``setup_prefix`` double recording its call and reporting ``tool``.

    It MUST return the ``(final_tool_id, did_recover)`` pair: the orchestrator
    rebuilds the launch plan from that tool, so a double returning ``None``
    would not exercise the real contract.
    """
    def _run(*_a, **_k):
        order.append("setup_prefix")
        return tool, False
    return AsyncMock(side_effect=_run)


async def test_launch_windows_calls_setup_prefix_before_sync_down(monkeypatch):
    order: list[str] = []
    setup = _setup_returning(order)
    monkeypatch.setattr(proton_pkg, "setup_prefix", setup)

    svc = _fake_service()
    svc._cloud_sync_phase = AsyncMock(
        side_effect=lambda _ctx, direction: order.append(f"sync_{direction}"),
    )

    ctx = _ctx()
    await orch.launch_windows(svc, ctx, SimpleNamespace(rc=0, proton_tool_id=None))

    setup.assert_awaited_once()
    # setup_prefix runs with the launch ctx/state and NO session_env
    # (Steam provides the user session at launch).
    assert setup.await_args.args[0] is ctx
    assert setup.await_args.kwargs.get("session_env") in (None, {})
    # and it must precede the cloud sync-down (drive_c must exist first).
    assert order[0] == "setup_prefix"
    assert "sync_down" in order
    assert order.index("setup_prefix") < order.index("sync_down")


async def test_launch_plan_is_rebuilt_from_the_tool_setup_settled_on(monkeypatch):
    """The game must launch under the Proton that actually built its prefix.

    ``setup_prefix`` mutates the shared ``state`` and its recovery ladder can
    settle on a different Proton than Phase 1 resolved. Launching the Phase-1
    plan regardless is what made umu run Proton-Experimental while the launcher
    logged (and had prepared the prefix with) GE-Proton11-3 — every launch then
    re-stamped the prefix and erased the imported VC++ keys.
    """
    monkeypatch.setattr(
        proton_pkg, "setup_prefix", _setup_returning([], tool="GE-Proton11-3"),
    )

    svc = _fake_service()
    launch_plan = SimpleNamespace(name="rebuilt")
    svc._prepare_windows_plan = AsyncMock(
        side_effect=[SimpleNamespace(name="phase1"), launch_plan],
    )

    await orch.launch_windows(svc, _ctx(), SimpleNamespace(rc=0, proton_tool_id="proton_11"))

    # Rebuilt with the tool setup_prefix reported, not the one Phase 1 picked.
    assert svc._prepare_windows_plan.await_args.kwargs["tool_id"] == "GE-Proton11-3"
    # ...and that rebuilt plan is what actually gets run.
    assert svc._run_game_subprocess.await_args.args[0] is launch_plan


async def test_dispatch_does_not_run_compat(monkeypatch):
    # dispatch must route to the store handler WITHOUT calling apply_prefix_compat
    # (that now lives once in setup_prefix). Patch the compat entry to a spy and
    # assert it's never touched.
    from unifideck.launcher.proton import compat as compat_pkg

    compat_spy = AsyncMock(return_value=False)
    monkeypatch.setattr(compat_pkg, "apply_prefix_compat", compat_spy)
    monkeypatch.setattr(proton_pkg, "repair_incomplete_umu_runtime", MagicMock())

    # Patch the routing table, not the module-level name: ``dispatch`` reads
    # ``_STORE_LAUNCHERS``, which binds the handlers at import. Patching
    # ``proton_pkg.epic_launch`` would leave the real handler in the map and
    # silently test nothing.
    epic_spy = AsyncMock(return_value=0)
    monkeypatch.setitem(proton_pkg._STORE_LAUNCHERS, "epic", epic_spy)

    plan = SimpleNamespace(context=SimpleNamespace(store="epic"))
    rc = await proton_pkg.dispatch(plan)

    assert rc == 0
    epic_spy.assert_awaited_once_with(plan)
    compat_spy.assert_not_called()
