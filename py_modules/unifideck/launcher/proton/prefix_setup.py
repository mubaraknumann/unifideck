"""launcher/proton/prefix_setup.py — the ONE canonical prefix setup.

Historically the "create the Wine prefix + install redistributables" process
had two divergent implementations that could disagree on which Proton to use:

* install-time **warmup** (``services/download/prefix_warmup.py``) ran
  createprefix + compat and, on a runtime hang, retried once with the managed
  GE-Proton — but recorded nothing about the Proton it succeeded with; and
* first **launch** (``services/launcher/orchestrator.py``) ran createprefix
  (Phase 1.5) then compat (inside ``proton.dispatch``) exactly once, with NO
  hang recovery and NO record.

So warmup could recover to GE-Proton while launch independently re-picked the
user's (hanging) global-default Proton, saw a "Proton family change", wiped the
just-warmed prefix, and re-ran the whole setup at Play time — throwing warmup's
work away (observed live: Rise of the Tomb Raider, 2026-07-22).

:func:`setup_prefix` is the single source of truth both paths now call. It runs
the identical, self-healing setup and — crucially — **pins the Proton it
actually succeeded with** (``proton_settings.json`` tier-1 + the prefix's
``.unifideck_proton_version`` marker) so the next launch resolves the SAME
Proton and does a fast no-op instead of a full redo. Whichever path runs first
wins and pins; the other becomes a genuine no-op.

Lives in ``launcher/`` (not ``services/``) because it runs under the system
``/usr/bin/python3`` out-of-process: stdlib-only at import time. The one write
into the aiohttp-heavy ``compatibility`` package (``save_proton_setting``) is
imported lazily inside the function, exactly as ``compat/ge_fallback.py`` does.
"""
from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from unifideck.launcher.wrapper_stores import prefix_owns_game_install

if TYPE_CHECKING:
    from unifideck.launcher.types.context import LaunchContext, RuntimeState

logger = logging.getLogger(__name__)


def _build_plan(
    ctx: LaunchContext,
    state: RuntimeState,
    python_bin: Any,
    proton: tuple[Path, str],
    session_env: dict[str, str] | None,
) -> Any:
    """A ``ProtonLaunchPlan`` for one Proton, with the session env grafted."""
    from unifideck.launcher.proton import proton_prepare

    proton_path, proton_tool_id = proton
    plan = proton_prepare(
        ctx, state, python_bin=python_bin,
        proton_path=proton_path, proton_tool_id=proton_tool_id,
    )
    # Graft any caller-supplied session env (install-time warmup borrows the
    # user session from the running Steam client; at launch Steam already
    # provides it so this is None). ``setdefault`` never clobbers a value the
    # plan already carries.
    if session_env:
        for env_key, env_val in session_env.items():
            plan.env.setdefault(env_key, env_val)
    return plan


async def _run_one(
    ctx: LaunchContext,
    state: RuntimeState,
    python_bin: Any,
    proton: tuple[Path, str],
    session_env: dict[str, str] | None,
    vcreg_proton: tuple[Path, str] | None = None,
) -> bool:
    """createprefix + generic compat under one Proton; True if a step hung.

    ``proton`` is a ``(path, tool_id)`` pair. ``vcreg_proton``, when given,
    runs only the VC++ registry step under that Proton instead — see
    ``compat.apply_prefix_compat``. Best-effort: any failure is logged and
    swallowed so the caller can still fall through to the GE retry (and the
    launch-time path remains a last-resort fallback).
    """
    from unifideck.launcher.proton.compat import apply_prefix_compat
    from unifideck.launcher.proton.compat.prefix_init import (
        ensure_prefix_initialized,
    )

    plan = _build_plan(ctx, state, python_bin, proton, session_env)
    try:
        await ensure_prefix_initialized(plan)
        vcreg_plan = (
            _build_plan(ctx, state, python_bin, vcreg_proton, session_env)
            if vcreg_proton is not None
            else None
        )
        return await apply_prefix_compat(plan, vcreg_plan=vcreg_plan)
    except Exception:
        logger.exception(
            "[prefix_setup] prefix init/compat failed for %s (continuing)",
            ctx.game_key,
        )
        return False


def _bridge_into_compatdata(plan: Any) -> None:
    """Expose this prefix to external Wine tooling (Protontricks).

    Protontricks resolves a non-Steam shortcut's prefix only at
    ``steamapps/compatdata/<appid>``, which is nowhere near where we keep
    ours, so without this link it reports "does not have a prefix" and skips
    the game entirely. Doing it here rather than at install time also repairs
    prefixes that predate the bridge, and covers Ubisoft, whose prefix path is
    only known once resolved at launch.

    ``ctx.steam_app_id`` comes straight from the games.map v3 row — never
    recompute it: ``generate_app_id`` is anchored on the launcher exe path, so
    a derived id does not match the stored one.
    """
    app_id = getattr(getattr(plan, "context", None), "steam_app_id", None)
    if not app_id:
        return
    try:
        from unifideck.core.compat_bridge import link_prefix
        from unifideck.utils.vdf_compat import resolve_live_steam_root

        link_prefix(plan.prefix_path, app_id, resolve_live_steam_root())
    except Exception:
        logger.exception("[prefix_setup] compatdata bridge failed (non-fatal)")


def _pin_final_tool(ctx: LaunchContext, state: RuntimeState, tool: str) -> None:
    """Persist ``tool`` as this game's Proton so the next launch reuses it.

    Called only after a GE recovery, when the tool that succeeded differs from
    what ``select_proton_version`` would resolve again (the user's hanging
    global-default). Without this the next launch re-picks the hanging Proton,
    sees a "Proton family change" against the GE-built prefix, and wipes +
    rebuilds it — exactly the redo-at-Play this module exists to kill.

    The prefix root comes from ``state.prefix_path``, which ``proton_prepare``
    resolved for this launch. It must NOT be rebuilt as
    ``~/.local/share/unifideck/prefixes/<game_id>``: that layout is right for
    every store except Ubisoft, whose path is read from ``ubisoft_id_map.json``
    and can live under any storage base the user picked. Reconstructing it
    stamped ``prefixes/80`` — a directory no launch ever opens — while the real
    prefix at ``~/Games/prefixes/ubisoft/80`` kept its stale marker, so the
    family-change reset fired again on the very next launch (2026-08-01).

    Mirrors ``compat/ge_fallback.py``: re-stamp the prefix marker AND write the
    per-game pin. ``save_proton_setting`` lives in the aiohttp-heavy
    ``compatibility`` package, so import it lazily to keep this launcher module
    stdlib-safe at import time. Best-effort — a failed pin must never break
    setup (the prefix is already built; worst case is a redo next launch).
    """
    from unifideck.launcher.proton.compat.prefix_init import _MARKER_NAME
    from unifideck.launcher.proton.infrastructure.prefix_layout import (
        normalize_prefix_root,
    )

    resolved = getattr(state, "prefix_path", None)
    if not resolved:
        logger.warning(
            "[prefix_setup] no resolved prefix for %s — not stamping the "
            "marker (the per-game pin below still applies)", ctx.game_key,
        )
        _save_pin(ctx, tool)
        return
    prefix_root = normalize_prefix_root(resolved)
    with contextlib.suppress(OSError):
        prefix_root.mkdir(parents=True, exist_ok=True)
        (prefix_root / _MARKER_NAME).write_text(tool, encoding="utf-8")
    _save_pin(ctx, tool)


def _save_pin(ctx: LaunchContext, tool: str) -> None:
    """Write the per-game Force-Compat pin (tier 1 of ``select_proton_version``)."""
    try:
        from unifideck.compatibility.proton_helpers import save_proton_setting

        save_proton_setting(ctx.game_key, tool)
        logger.info(
            "[prefix_setup] pinned %s for %s (survives next launch)",
            tool, ctx.game_key,
        )
    except Exception:
        logger.exception(
            "[prefix_setup] failed to pin %s for %s (non-fatal)",
            tool, ctx.game_key,
        )


def _can_run_winetricks_verb(proton_path: Path | str | None) -> bool:
    """Whether ``umu-run winetricks`` can work with this Proton.

    umu execs ``<PROTONPATH>/protonfixes/winetricks``. GE-Proton and
    UMU-Proton bundle that; official Valve Protons do not ship a
    ``protonfixes`` directory at all, so the verb dies with a
    FileNotFoundError from inside umu rather than reporting anything useful.

    Returns True when there is no path to judge (``None``) or the check
    itself errors, so this gate can only ever skip an attempt that was
    certain to fail — it never becomes a new way to reject a Proton that
    might have worked. A path that exists but has no ``protonfixes/``
    returns False, which includes the "selector handed us something that
    isn't there" case: routing that to managed GE is the same outcome the
    timeout ladder would have reached, just sooner.
    """
    if not proton_path:
        return True
    try:
        root = Path(proton_path)
        if root.is_file():  # the `proton` script itself was passed
            root = root.parent
        return (root / "protonfixes").is_dir()
    except OSError:
        return True


def _bridge_now(
    ctx: LaunchContext,
    state: RuntimeState,
    python_bin: Path | str,
    default: tuple[Path | str | None, str],
) -> None:
    """Bridge this game's prefix into ``compatdata``, best-effort.

    Wraps :func:`_bridge_into_compatdata` with the cheap plan build it needs
    (a mkdir plus env assembly, no subprocess). Separate from the setup ladder
    so it can run before every early return — the bridge must not depend on
    whether there is setup work left to do.
    """
    default_path, default_tool = default
    if not default_path:
        return
    try:
        _bridge_into_compatdata(
            _build_plan(
                ctx, state, python_bin, (Path(default_path), default_tool), None,
            ),
        )
    except Exception:
        logger.exception(
            "[prefix_setup] compatdata bridge skipped for %s (non-fatal)",
            ctx.game_key,
        )
    _bridge_compat_tool(default_path, default_tool)


def _bridge_compat_tool(proton_path: Path | str, tool_id: str) -> None:
    """Expose the Proton we are about to use to Protontricks, best-effort.

    Companion to the compatdata bridge: that one gets Protontricks to the
    prefix, this one gets it to the Proton. Without it a distro-packaged tool
    (CachyOS ``proton-cachyos`` under ``/usr/share/steam``) is invisible to the
    Protontricks Flatpak and it aborts with "Active Proton installation could
    not be found automatically".

    Done here, at launch, for the same reasons the prefix bridge is: this is
    the only point where the tool actually in use is known, and it also
    repairs games that predate the bridge. No-ops for the common case — a
    GE-Proton under ``~/.steam`` is already visible to the sandbox.

    ``proton_path`` is the ``proton`` *script*; the tool directory is its
    parent, matching how ``core`` derives ``PROTONPATH``.
    """
    try:
        from unifideck.core.compat_tool_bridge import link_tool

        link_tool(Path(proton_path).parent, tool_id)
    except Exception:
        logger.exception(
            "[prefix_setup] compat-tool bridge failed (non-fatal)",
        )


def _compat_pending(
    ctx: LaunchContext,
    state: RuntimeState,
    python_bin: Path | str,
    default: tuple[Path | str | None, str],
) -> bool:
    """Whether this prefix still needs any setup work under ``default``.

    Cheap: builds a plan (a mkdir plus env assembly, no subprocess) purely so
    the compat steps' own guards can be consulted. Fails OPEN — if anything
    goes wrong deciding, report pending and let the real steps re-check.
    """
    from unifideck.launcher.proton.compat import compat_work_pending

    default_path, default_tool = default
    if not default_path:
        return True
    try:
        plan = _build_plan(
            ctx, state, python_bin, (Path(default_path), default_tool), None,
        )
        return compat_work_pending(plan)
    except Exception:
        logger.exception(
            "[prefix_setup] pending-check failed for %s (assuming pending)",
            ctx.game_key,
        )
        return True


def _nothing_to_do(
    ctx: LaunchContext,
    state: RuntimeState,
    python_bin: Path | str,
    default: tuple[Path | str | None, str],
) -> bool:
    """Whether :func:`setup_prefix` should return without touching anything.

    Two reasons, both of which must short-circuit BEFORE the GE reroute:

    **Ubisoft.** ``apply_prefix_compat`` skips the store outright (UPC installs
    its own redistributables), so the only step left would be
    ``ensure_prefix_initialized`` — and running that is what DELETED a user's
    Rayman Origins on 2026-08-01. Ubisoft games live inside the prefix, so its
    Proton-family reset is data loss. Naming the store here keeps the
    destructive path unreachable rather than relying on the pending-check
    below happening to say "nothing pending".

    **An already-warmed prefix.** Skipping keeps a Proton switch cheap: the
    reroute would otherwise borrow GE on EVERY launch of a fully set-up game,
    and each Proton change makes Proton re-run ``wineboot -u`` and rewrite
    ``system.reg``.
    """
    if prefix_owns_game_install(ctx.store):
        logger.info(
            "[prefix_setup] skipping setup for %s — the %s client owns this "
            "prefix (compat is skipped and a reset would delete the game)",
            ctx.game_key, ctx.store,
        )
        return True
    if not _compat_pending(ctx, state, python_bin, default):
        logger.debug(
            "[prefix_setup] nothing pending for %s under proton=%s",
            ctx.game_key, default[1],
        )
        return True
    return False


async def _preempt_incapable_proton(
    ctx: LaunchContext,
    state: RuntimeState,
    python_bin: Path | str,
    default: tuple[Path | str | None, str],
    session_env: dict[str, str] | None,
) -> bool:
    """Run setup under managed GE when the default can't do compat at all.

    Returns ``True`` if it took over and setup is done, else ``False`` (caller
    proceeds with the default as usual). Note it reports only *whether* it
    ran — the final Proton stays the caller's default, because borrowing GE
    for one umu verb must not change which Proton the game launches under.

    umu's winetricks verb execs ``<PROTONPATH>/protonfixes/winetricks``,
    which only GE-Proton and UMU-Proton ship — umu's own ``--help`` says
    "requires UMU-Proton or GE-Proton". Under an official Valve Proton
    (Experimental, Proton 9, proton-cachyos…) that path does not exist, so
    the step cannot succeed no matter how long it is given.

    Attempting it anyway is not merely futile, it is expensive: the missing
    directory surfaces as a FileNotFoundError *inside* umu, the wine child is
    left holding the prefix, and each step burns its full timeout before
    being killed — twice per install — after which the caller's ladder
    switches to GE and RESETS the prefix, discarding everything it just
    built. The end state was always GE, so checking first costs one ``stat``
    and saves minutes plus a wasted prefix build on every fresh install.
    """
    default_path, default_tool = default
    if _can_run_winetricks_verb(default_path):
        return False

    from unifideck.launcher.proton import select_managed_ge_proton

    ge_path, ge_tool = select_managed_ge_proton()
    if ge_tool == default_tool:
        # Nothing better to switch to; let the normal path report whatever
        # actually happens rather than silently doing nothing.
        return False

    logger.info(
        "[prefix_setup] proton=%s cannot run umu's winetricks verb (no "
        "protonfixes/ — official Valve Protons don't ship it); using managed "
        "GE-Proton %s for %s's redistributables (the game still runs under %s)",
        default_tool, ge_tool, ctx.game_key, default_tool,
    )
    # Borrow GE for the winetricks verb ONLY, and run the VC++ registry step
    # under the Proton the game will actually use. Do NOT pin GE and do NOT
    # report it as the final tool: this reroute is about one umu verb, not
    # about whether the game can run, and claiming GE here is what used to
    # make the launch and the prefix disagree — the launcher logged
    # "proton=GE-Proton11-3" while umu ran the user's Proton-Experimental,
    # which then re-stamped the prefix on every single launch.
    await _run_one(
        ctx, state, python_bin, (ge_path, ge_tool), session_env,
        vcreg_proton=(Path(default_path), default_tool) if default_path else None,
    )
    return True


def _chosen_or_selected(ctx: LaunchContext, select: Any) -> tuple[Path, str]:
    """``ctx.proton_tool`` when set and installed (a fresh install's GE), else the selector's pick."""
    chosen = getattr(ctx, "proton_tool", None)
    if chosen:
        from unifideck.launcher.proton import resolve_proton_path

        path = resolve_proton_path(chosen)
        if path is not None:
            return path, chosen
        logger.warning("[prefix_setup] %s not found for %s; using the selected Proton", chosen, ctx.game_key)
    picked: tuple[Path, str] = select(steam_app_id=ctx.steam_app_id, store_game_id=ctx.game_key)
    return picked


async def setup_prefix(
    ctx: LaunchContext,
    state: RuntimeState,
    *,
    session_env: dict[str, str] | None = None,
) -> tuple[str, bool]:
    """The canonical prefix setup, reused by install warmup AND first launch.

    createprefix + generic compat under the normally-resolved Proton; on a
    runtime **hang** (a step force-killed for exceeding its timeout — a
    structurally-complete but broken Proton the static check can't catch),
    retry ONCE with the plugin-managed GE-Proton, then **pin** whichever Proton
    succeeded so the next launch reuses it directly (no prefix reset, no
    dependency reinstall).

    Recovery ladder, gated so it never loops:
      0. Nothing left to do (prefix built, both compat steps already terminal)
         → return immediately, touching neither the prefix nor the Proton.
      1. Setup under ``select_proton_version`` (the default a launch would pick).
      2. On hang → switch to ``select_managed_ge_proton`` and retry; pin the
         result.

    Returns ``(final_tool_id, did_recover)``. The returned tool is the one the
    GAME should run under, so a caller must launch with it — only step 2
    changes it, because only step 2 means the default Proton is genuinely
    broken. All best-effort: if every attempt still hangs, the prefix finishes
    at first launch (the launch path re-runs these same steps). ``session_env``
    is grafted into the umu env for the headless install-time caller; ``None``
    at launch (Steam provides a session).
    """
    from unifideck.launcher.proton import (
        find_python_3_10_plus,
        select_managed_ge_proton,
        select_proton_version,
    )

    python_bin = find_python_3_10_plus()
    # No per-game Force-Compat choice / steam_app_id is meaningful at install
    # time, and at launch ``ctx.steam_app_id`` is honoured — this resolves the
    # same default the first launch would pick.
    default_path, default_tool = _chosen_or_selected(ctx, select_proton_version)

    # Bridge FIRST, before any early return: it is a cheap idempotent symlink
    # (``link_prefix`` returns "noop" when already correct) and it is the only
    # thing that makes the prefix reachable from Protontricks. Gating it behind
    # the work-pending checks below would mean an already-warmed prefix — the
    # common case on every relaunch — never gets bridged or repaired.
    _bridge_now(ctx, state, python_bin, (default_path, default_tool))

    if _nothing_to_do(ctx, state, python_bin, (default_path, default_tool)):
        return default_tool, False

    if await _preempt_incapable_proton(
        ctx, state, python_bin, (default_path, default_tool), session_env,
    ):
        return default_tool, False

    if not await _run_one(
        ctx, state, python_bin, (default_path, default_tool), session_env,
    ):
        return default_tool, False

    return await _recover_from_hang(
        ctx, state, python_bin, default_tool, session_env,
        select_managed_ge_proton,
    )


async def _recover_from_hang(
    ctx: LaunchContext,
    state: RuntimeState,
    python_bin: Path | str,
    default_tool: str,
    session_env: dict[str, str] | None,
    select_managed_ge_proton: Any,
) -> tuple[str, bool]:
    """Step 2 of the ladder: setup hung, retry once under managed GE and pin.

    Unlike the winetricks-capability reroute, a hang means the default Proton
    is genuinely broken, so GE becomes the tool the GAME runs under too — hence
    the pin, and hence this returns it as the final tool.
    """
    ge_path, ge_tool = select_managed_ge_proton()
    if ge_tool == default_tool:
        logger.warning(
            "[prefix_setup] compat timed out for %s under managed GE-Proton "
            "%s — not retrying (prefix finishes at launch)",
            ctx.game_key, ge_tool,
        )
        return ge_tool, False

    logger.warning(
        "[prefix_setup] compat still timing out for %s under proton=%s — "
        "retrying setup with managed GE-Proton %s",
        ctx.game_key, default_tool, ge_tool,
    )
    await _run_one(ctx, state, python_bin, (ge_path, ge_tool), session_env)
    _pin_final_tool(ctx, state, ge_tool)
    return ge_tool, True
