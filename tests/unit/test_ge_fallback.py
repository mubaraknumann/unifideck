"""Unit tests for compat/ge_fallback — last-resort GE-Proton fallback.

Regression coverage for the "give the resolved tool a fair chance,
then fall back to bundled GE-Proton" policy: select_proton_version's
tier 4 honors the user's Steam-wide global-default compat tool even
when that specific build is broken (e.g. a Proton-Experimental
snapshot confirmed hanging live while testing the 0.6.1 -> 0.7.1
upgrade). GE-Proton succeeded in ~9s against the identical prefix that
build hung on indefinitely.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import unifideck.compatibility.proton_helpers as pth_mod
import unifideck.launcher.proton.infrastructure.ge_installer as ge_mod
import unifideck.launcher.proton.infrastructure.ge_marker as gm_mod
from unifideck.launcher.proton.compat import ge_fallback as gf
from unifideck.launcher.proton.compat import prefix_init as pi
from unifideck.launcher.proton.compat import save_migration as sm


def _plan(prefix_root: Path, tool: str):
    return SimpleNamespace(
        prefix_path=prefix_root,
        # Mirrors RuntimeState, which carries both fields; the fallback's
        # identity guard reads proton_path as well as the tool id.
        state=SimpleNamespace(proton_tool_id=tool, proton_path=None),
        context=SimpleNamespace(
            game_key="epic:Hazelnut", store="epic", game_id="Hazelnut",
        ),
        python_bin=Path("/usr/bin/python3"),
        on_process_start=None,
    )


async def test_fallback_skipped_when_already_ge_proton(tmp_path, monkeypatch):
    root = tmp_path / "prefix"
    root.mkdir()
    plan = _plan(root, "GE-Proton10-34")
    monkeypatch.setattr(gf, "_resolve_ge_proton", lambda: (Path("/opt/ge/proton"), "GE-Proton10-34"))
    retry = MagicMock()
    monkeypatch.setattr(pi, "_run_createprefix_with_retry", retry)

    await gf.fallback_to_ge_proton(plan, root)

    retry.assert_not_called()


async def test_fallback_succeeds_persists_choice_and_restamps_marker(
    tmp_path, monkeypatch,
):
    root = tmp_path / "prefix"
    root.mkdir()
    plan = _plan(root, "proton_experimental")

    monkeypatch.setattr(gm_mod, "read_cached_latest_tag", lambda: "GE-Proton11-1")
    monkeypatch.setattr(
        ge_mod, "installed_ge_proton_path", lambda tag: Path("/opt/ge/proton"),
    )
    ge_plan = _plan(root, "GE-Proton11-1")
    ge_plan.env = {}
    prepare_kwargs: dict = {}

    def _fake_prepare(ctx, state, **kw):
        prepare_kwargs.update(kw)
        return ge_plan

    monkeypatch.setattr(gf, "proton_prepare", _fake_prepare)

    async def _fake_retry(plan_arg, env_arg, prefix_root_arg):
        (prefix_root_arg / "pfx").mkdir(exist_ok=True)
        (prefix_root_arg / "pfx" / "system.reg").write_text("reg")
        return True

    monkeypatch.setattr(pi, "_run_createprefix_with_retry", _fake_retry)

    async def _fake_restore(*a, **k):
        pass

    monkeypatch.setattr(sm, "restore_or_migrate_saves", _fake_restore)
    saved: dict = {}

    def _fake_save(store_game_id, tool_name):
        saved["store_game_id"] = store_game_id
        saved["tool_name"] = tool_name
        return {"success": True}

    monkeypatch.setattr(pth_mod, "save_proton_setting", _fake_save)
    toast = MagicMock()
    monkeypatch.setattr(gf, "launcher_toast", toast)

    await gf.fallback_to_ge_proton(plan, root)

    assert prepare_kwargs["proton_tool_id"] == "GE-Proton11-1"
    assert prepare_kwargs["proton_path"] == Path("/opt/ge/proton")
    assert saved == {"store_game_id": "epic:Hazelnut", "tool_name": "GE-Proton11-1"}
    assert (root / pi._MARKER_NAME).read_text() == "GE-Proton11-1"
    assert toast.call_args.args[0] == "toasts.launcher.protonSwitchedTo"


async def test_fallback_warns_when_ge_proton_unavailable(tmp_path, monkeypatch):
    root = tmp_path / "prefix"
    root.mkdir()
    plan = _plan(root, "proton_experimental")

    monkeypatch.setattr(gm_mod, "read_cached_latest_tag", lambda: None)
    monkeypatch.setattr(ge_mod, "ensure_latest_ge", lambda: None)
    retry = MagicMock()
    monkeypatch.setattr(pi, "_run_createprefix_with_retry", retry)

    await gf.fallback_to_ge_proton(plan, root)

    retry.assert_not_called()
    assert not (root / pi._MARKER_NAME).exists()


async def test_fallback_when_ge_proton_retry_also_fails(tmp_path, monkeypatch):
    root = tmp_path / "prefix"
    root.mkdir()
    plan = _plan(root, "proton_experimental")

    monkeypatch.setattr(gm_mod, "read_cached_latest_tag", lambda: "GE-Proton11-1")
    monkeypatch.setattr(
        ge_mod, "installed_ge_proton_path", lambda tag: Path("/opt/ge/proton"),
    )
    ge_plan = _plan(root, "GE-Proton11-1")
    ge_plan.env = {}
    monkeypatch.setattr(gf, "proton_prepare", lambda *a, **k: ge_plan)

    async def _fake_retry_fail(*a, **k):
        return False

    monkeypatch.setattr(pi, "_run_createprefix_with_retry", _fake_retry_fail)
    save = MagicMock()
    monkeypatch.setattr(pth_mod, "save_proton_setting", save)

    await gf.fallback_to_ge_proton(plan, root)

    save.assert_not_called()
    assert not (root / pi._MARKER_NAME).exists()


async def test_fallback_recovers_from_external_ge_failure(tmp_path, monkeypatch):
    """An external GE that cannot build a prefix must still fall back.

    Restored after the #448 merge dropped it (register 62). The guard used
    to be a *family* test, and "Proton-GE Latest" normalises into the
    ge-proton family, so a failing external tool left the launch with no
    recovery at all.
    """
    root = tmp_path / "prefix"
    root.mkdir()
    plan = _plan(root, "Proton-GE Latest")
    plan.state.proton_path = tmp_path / "external-ge" / "proton"

    bundled = tmp_path / "bundled-ge" / "proton"
    monkeypatch.setattr(gf, "_resolve_ge_proton", lambda: (bundled, "GE-Proton11-5"))
    ge_plan = _plan(root, "GE-Proton11-5")
    ge_plan.env = {}
    monkeypatch.setattr(gf, "proton_prepare", lambda ctx, state, **kw: ge_plan)
    retry = AsyncMock(return_value=True)
    monkeypatch.setattr(pi, "_run_createprefix_with_retry", retry)
    monkeypatch.setattr(sm, "restore_or_migrate_saves", AsyncMock())
    monkeypatch.setattr(pth_mod, "save_proton_setting", MagicMock())

    await gf.fallback_to_ge_proton(plan, root)
    retry.assert_awaited_once()


async def test_fallback_skipped_when_alias_resolves_to_the_bundled_ge(tmp_path, monkeypatch):
    """Register 61: an alias symlinked at the fallback is the same Proton.

    Comparing raw paths made the guard miss, so the fallback re-ran
    createprefix against the identical build that had just failed.
    """
    root = tmp_path / "prefix"
    root.mkdir()
    real = tmp_path / "GE-Proton11-5"
    real.mkdir()
    (real / "proton").write_text("#!/bin/sh\n")
    alias = tmp_path / "Proton-GE Latest"
    alias.symlink_to(real)

    plan = _plan(root, "Proton-GE Latest")
    plan.state.proton_path = alias / "proton"

    monkeypatch.setattr(gf, "_resolve_ge_proton", lambda: (real / "proton", "GE-Proton11-5"))
    retry = AsyncMock(return_value=True)
    monkeypatch.setattr(pi, "_run_createprefix_with_retry", retry)

    await gf.fallback_to_ge_proton(plan, root)
    retry.assert_not_awaited()
