"""Tests for the canonical prefix setup's timeout → managed-GE retry + pin.

The "create prefix + install redistributables" process is now a single
:func:`unifideck.launcher.proton.setup_prefix`, reused by install-time warmup
AND first launch. A structurally-complete but runtime-hanging Proton wedges
setup; ``apply_prefix_compat`` reports when a step was force-killed for timing
out, and ``setup_prefix`` retries the setup ONCE with the plugin-managed
GE-Proton, then PINS whichever Proton succeeded so the next launch reuses it
directly (no prefix reset, no dependency reinstall).

These tests pin: retry-with-GE on timeout; no retry when the hung Proton WAS
already GE (no loop); no retry on a clean run; and the pin (marker + saved
setting) only on a recovery. They target ``setup_prefix`` directly — the same
unit both the warmup adapter and the launch orchestrator call.

(An earlier revision inserted a repair-in-place rung here — official Protons via
SteamClient.Apps.VerifyApp, GE via re-install — between the default attempt and
the GE switch. Removed: across every hang it fired on live, VerifyApp reported
success but the same-Proton retry hung again regardless, so it never changed the
outcome. The actual install-warmup hang was a missing user-session env for
install-time umu runs, fixed in ``_user_session_env`` — see
test_warmup_session_env.py and memory install-hang-orphaned-wineserver-lock.md.)
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from unifideck.launcher import proton as proton_pkg
from unifideck.launcher.proton import prefix_setup as setup_mod
from unifideck.launcher.proton.compat import prefix_init as prefix_init_mod


def _ctx(tmp_path):
    """A minimal LaunchContext-shaped stub the setup unit reads from."""
    return SimpleNamespace(
        store="gog", game_id="123", game_key="gog:123", steam_app_id=None,
    )


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """Patch the lazy-imported launcher surface ``setup_prefix`` pulls in.

    Returns the spies so each test can assert selection + retry behaviour.
    """
    monkeypatch.setattr(proton_pkg, "find_python_3_10_plus", lambda: "/usr/bin/python3")
    # ``context``/``prefix_path`` are what ``compat.compat_work_pending`` reads
    # for the "is any setup still outstanding?" check that now gates the ladder.
    # The prefix has no ``system.reg``, so it correctly reports pending and the
    # ladder runs — these tests are about the timeout path, not the skip path.
    monkeypatch.setattr(
        proton_pkg, "proton_prepare",
        lambda ctx, state, **kw: SimpleNamespace(
            tool_id=kw["proton_tool_id"], env={},
            context=ctx, prefix_path=tmp_path / "prefix",
        ),
    )
    ensure = AsyncMock()
    monkeypatch.setattr(prefix_init_mod, "ensure_prefix_initialized", ensure)
    # Never touch disk or Steam in the ladder tests — the pin is asserted
    # separately in test_prefix_setup_pin.py.
    pin = MagicMock()
    monkeypatch.setattr(setup_mod, "_pin_final_tool", pin)
    return SimpleNamespace(ensure=ensure, pin=pin)


def _patch_compat(monkeypatch, timed_out_sequence):
    """apply_prefix_compat returns each bool in the sequence, in order."""
    from unifideck.launcher.proton import compat as compat_pkg

    seq = iter(timed_out_sequence)
    calls = []

    async def _compat(plan):
        val = next(seq)
        calls.append(plan.tool_id)
        return val

    monkeypatch.setattr(compat_pkg, "apply_prefix_compat", _compat)
    return calls


def _capable_proton(tmp_path, name):
    """A Proton dir that CAN run umu's winetricks verb.

    The winetricks step skips a Proton with no ``protonfixes/`` (see
    test_prefix_setup_winetricks_capable.py). These tests exercise the
    *timeout* ladder, which only applies to a Proton that genuinely could
    have worked, so the fakes carry the payload.
    """
    root = tmp_path / name
    (root / "protonfixes").mkdir(parents=True, exist_ok=True)
    (root / "protonfixes" / "winetricks").write_text("#!/bin/sh\n")
    return root


def _patch_selectors(monkeypatch, tmp_path, *, default_tool, ge_tool):
    default_path = _capable_proton(tmp_path, default_tool)
    # The "steam" tier: the default is the game's own setting, so setup
    # keeps it and only the hang ladder can move the game to GE.
    monkeypatch.setattr(
        proton_pkg, "select_proton_with_source",
        lambda steam_app_id, store_game_id: (str(default_path), default_tool, "steam"),
    )
    ge = MagicMock(return_value=(str(_capable_proton(tmp_path, ge_tool)), ge_tool))
    monkeypatch.setattr(proton_pkg, "select_managed_ge_proton", ge)
    return ge


async def test_retry_with_ge_on_compat_timeout(tmp_path, wired, monkeypatch):
    # default Proton hangs (timed_out=True), then GE retry succeeds (False).
    calls = _patch_compat(monkeypatch, [True, False])
    ge = _patch_selectors(
        monkeypatch, tmp_path, default_tool="proton_experimental", ge_tool="GE-Proton11-1",
    )

    tool, recovered = await setup_mod.setup_prefix(_ctx(tmp_path), SimpleNamespace())

    ge.assert_called_once()
    # createprefix runs twice (default, then GE); compat runs against both.
    assert wired.ensure.await_count == 2
    assert calls == ["proton_experimental", "GE-Proton11-1"]
    assert (tool, recovered) == ("GE-Proton11-1", True)
    # A recovery pins the winning Proton so the next launch reuses it.
    wired.pin.assert_called_once()
    # (ctx, state, tool) — state carries the resolved prefix the marker goes in.
    assert wired.pin.call_args.args[2] == "GE-Proton11-1"


async def test_no_retry_when_hung_proton_was_already_ge(tmp_path, wired, monkeypatch):
    # GE itself hung — retrying with GE again would loop, so we must NOT.
    calls = _patch_compat(monkeypatch, [True])
    ge = _patch_selectors(
        monkeypatch, tmp_path, default_tool="GE-Proton11-1", ge_tool="GE-Proton11-1",
    )

    tool, recovered = await setup_mod.setup_prefix(_ctx(tmp_path), SimpleNamespace())

    # select_managed_ge_proton is consulted to compare tool ids, but no
    # second createprefix/compat runs — and nothing is pinned (no recovery).
    ge.assert_called_once()
    assert wired.ensure.await_count == 1
    assert calls == ["GE-Proton11-1"]
    assert (tool, recovered) == ("GE-Proton11-1", False)
    wired.pin.assert_not_called()


async def test_no_retry_on_clean_run(tmp_path, wired, monkeypatch):
    calls = _patch_compat(monkeypatch, [False])
    ge = _patch_selectors(
        monkeypatch, tmp_path, default_tool="proton_experimental", ge_tool="GE-Proton11-1",
    )

    tool, recovered = await setup_mod.setup_prefix(_ctx(tmp_path), SimpleNamespace())

    ge.assert_not_called()
    assert wired.ensure.await_count == 1
    assert calls == ["proton_experimental"]
    # A clean run keeps the resolved default and must NOT pin (which would
    # freeze the user's global-default choice against their will).
    assert (tool, recovered) == ("proton_experimental", False)
    wired.pin.assert_not_called()
