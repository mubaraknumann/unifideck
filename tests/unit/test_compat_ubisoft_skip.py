"""Ubisoft launches skip the generic redistributables compat step.

Ubisoft games run through UPC, which installs its own redistributables,
so ``apply_prefix_compat`` must NOT run winetricks/vcredist for them
(that only re-installs what UPC provides + adds a first-launch delay).
Other stores run the game exe directly and still need it.

``setup_prefix`` skips the store outright for the same reason — and because
the only step left, ``ensure_prefix_initialized``, resets the prefix on a
Proton family change, which for Ubisoft means deleting the installed game.
The compatdata bridge must still run: it is what makes the prefix reachable
from Protontricks, and Ubisoft is the store its docstring exists to cover.
"""
from __future__ import annotations

import types
from unittest.mock import AsyncMock, MagicMock

import pytest

from unifideck.launcher import proton as proton_pkg
from unifideck.launcher.proton import compat
from unifideck.launcher.proton import prefix_setup as setup_mod
from unifideck.launcher.proton.compat import prefix_init as prefix_init_mod


def _plan(store: str, prefix_path):
    return types.SimpleNamespace(
        context=types.SimpleNamespace(store=store),
        prefix_path=prefix_path,
    )


@pytest.mark.asyncio
async def test_ubisoft_skips_generic_compat(monkeypatch, tmp_path):
    called: list[str] = []

    async def _wt(_plan):
        called.append("winetricks")

    async def _vc(_plan):
        called.append("vcruntime")

    monkeypatch.setattr(compat, "apply_winetricks", _wt)
    monkeypatch.setattr(compat, "apply_vcruntime_fix", _vc)

    await compat.apply_prefix_compat(_plan("ubisoft", tmp_path))
    assert called == []  # neither step ran


@pytest.mark.asyncio
async def test_other_store_runs_generic_compat(monkeypatch, tmp_path):
    (tmp_path / "system.reg").write_text("x")  # initialised prefix
    called: list[str] = []

    async def _wt(_plan):
        called.append("winetricks")

    async def _vc(_plan):
        called.append("vcruntime")

    monkeypatch.setattr(compat, "apply_winetricks", _wt)
    monkeypatch.setattr(compat, "apply_vcruntime_fix", _vc)

    await compat.apply_prefix_compat(_plan("epic", tmp_path))
    assert called == ["winetricks", "vcruntime"]


# ── setup_prefix: skip the store, but still bridge ────────────────


async def _run_setup_prefix(monkeypatch, tmp_path, store: str):
    """Drive ``setup_prefix`` for ``store``, recording what it reached."""
    seen: dict[str, object] = {"init": False, "bridged": None}

    async def _init(_plan):
        seen["init"] = True

    monkeypatch.setattr(proton_pkg, "find_python_3_10_plus", lambda: "/usr/bin/python3")
    monkeypatch.setattr(
        proton_pkg, "proton_prepare",
        lambda ctx, state, **kw: types.SimpleNamespace(
            tool_id=kw["proton_tool_id"], env={}, context=ctx,
            prefix_path=tmp_path / "prefix",
        ),
    )
    monkeypatch.setattr(
        proton_pkg, "select_proton_with_source",
        lambda steam_app_id, store_game_id: ("/protons/experimental", "proton_exp", "steam"),
    )
    monkeypatch.setattr(
        proton_pkg, "select_managed_ge_proton",
        MagicMock(return_value=("/protons/ge", "GE-Proton11-3")),
    )
    monkeypatch.setattr(prefix_init_mod, "ensure_prefix_initialized", _init)
    monkeypatch.setattr(setup_mod, "_pin_final_tool", MagicMock())
    monkeypatch.setattr(compat, "apply_prefix_compat", AsyncMock(return_value=False))
    monkeypatch.setattr(
        setup_mod, "_bridge_into_compatdata",
        lambda plan: seen.__setitem__("bridged", plan.prefix_path),
    )

    ctx = types.SimpleNamespace(
        store=store, game_id="80", game_key=f"{store}:80", steam_app_id=3124767362,
    )
    result = await setup_mod.setup_prefix(ctx, types.SimpleNamespace())
    return result, seen


async def test_setup_prefix_never_initialises_a_ubisoft_prefix(monkeypatch, tmp_path):
    """The destructive step must be unreachable for Ubisoft.

    ``ensure_prefix_initialized`` resets the prefix on a Proton family change.
    Ubisoft installs its games *inside* the prefix, so that reset deleted a
    user's Rayman Origins on 2026-08-01. Nothing here needs the prefix built —
    UPC already built it — so the whole ladder is skipped.
    """
    (tool, recovered), seen = await _run_setup_prefix(monkeypatch, tmp_path, "ubisoft")

    assert seen["init"] is False
    assert (tool, recovered) == ("proton_exp", False)


async def test_setup_prefix_still_bridges_ubisoft(monkeypatch, tmp_path):
    """Skipping the setup ladder must not skip the Protontricks bridge."""
    _result, seen = await _run_setup_prefix(monkeypatch, tmp_path, "ubisoft")

    assert seen["bridged"] == tmp_path / "prefix"


async def test_setup_prefix_bridges_other_stores_too(monkeypatch, tmp_path):
    """The bridge is not Ubisoft-specific — it runs before every early return."""
    _result, seen = await _run_setup_prefix(monkeypatch, tmp_path, "gog")

    assert seen["bridged"] == tmp_path / "prefix"
