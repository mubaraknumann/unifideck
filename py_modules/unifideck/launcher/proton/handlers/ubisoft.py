from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

from unifideck.launcher.frontend_bridge import launcher_toast
from unifideck.launcher.game_title import resolve_title
from unifideck.launcher.proton.handlers import wrapper_clients
from unifideck.launcher.proton.handlers.ubisoft_handoff import (
    capture_after_exit,
    seed_before_launch,
    vault_fingerprint,
)
from unifideck.launcher.proton.handlers.ubisoft_recovery import (
    ID_MAP_FILE,
    clone_template_into,
    find_recovered_prefix,
    find_upc_in,
)
from unifideck.launcher.proton.infrastructure.core import ProtonLaunchPlan
from unifideck.launcher.proton.infrastructure.umu_runtime import run_umu_with_retry
from unifideck.launcher.types.errors import GameFailedError, UmuRuntimeError

logger = logging.getLogger(__name__)


def _uplay_id_from_id_map(space_id: str) -> str | None:
    """Resolve the ``uplay://`` launch id for ``space_id`` from the id_map.

    Steam can't hand env vars to the launcher (it only forwards launch
    options as argv, and the dispatcher promotes ``UNIFIDECK_*`` tokens
    only), so ``UPLAY_ID`` is almost never set. Fall back to the id_map
    the backend persists, preferring the leveldb-sourced
    ``ubisoftconnect_game_id`` — the value ``uplay://launch/{id}/0``
    actually expects — then ``launch_id`` / ``install_id``. Returns
    ``None`` (caller drops to the Legendary path) on any read error.
    """
    try:
        data = json.loads(ID_MAP_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entry = data.get(space_id) if isinstance(data, dict) else None
    if not isinstance(entry, dict):
        return None
    for key in ("ubisoftconnect_game_id", "launch_id", "install_id"):
        value = entry.get(key)
        # UUID-only titles record "0" (no numeric uplay id resolved yet);
        # ``uplay://launch/0/0`` opens UPC's home, not the game, so treat
        # "0" as missing and fall through to the next candidate.
        if value and str(value) != "0":
            return str(value)
    return None
async def _apply_epic_wrapper_fix(plan: ProtonLaunchPlan) -> None:
    """Apply EPIC wrapper fix."""
    from unifideck.launcher.proton.fixes.epic_prefix_fix import apply_epic_launcher_fix
    bundled_wrapper = (
        plan.context.plugin_dir / "bin" / "EpicGamesLauncher.exe"
    )
    if not bundled_wrapper.is_file():
        logger.warning(
            "[launcher.proton.ubisoft] EpicGamesLauncher.exe "
            "wrapper missing at %s",
            bundled_wrapper,
        )
        return
    try:
        await apply_epic_launcher_fix(
            plan=plan,
            prefix_path=plan.prefix_path,
            bundled_wrapper=bundled_wrapper,
        )
        logger.info(
            "[launcher.proton.ubisoft] Epic launcher wrapper applied",
        )
    except Exception:
        logger.exception(
            "[launcher.proton.ubisoft] Epic launcher wrapper fix failed",
        )
async def _inject_registry_keys(plan: ProtonLaunchPlan) -> bool:
    """Inject registry keys."""
    from unifideck.launcher.proton.fixes.epic_registry import setup_registry
    legendary_config = await asyncio.to_thread(lambda: Path("~/.config/legendary").expanduser())
    try:
        result = await setup_registry(
            plan=plan,
            game_id=plan.context.game_id,
            legendary_config=legendary_config,
        )
        return result.success
    except Exception:
        logger.exception(
            "[launcher.proton.ubisoft] registry injection crashed",
        )
        return False



def _find_upc_exe(plan: ProtonLaunchPlan) -> Path | None:
    """Find UPC exe."""
    active_prefix = os.environ.get("ACTIVE_WINEPREFIX")
    if active_prefix:
        found = find_upc_in(Path(active_prefix))
        if found is not None:
            return found
    return find_upc_in(plan.prefix_path)
async def _run_upc_with_session_handoff(
    plan: ProtonLaunchPlan,
    argv: list[str],
) -> int:
    """Run UPC with the session baton passed in before and out after.

    Ubisoft retires the previous refresh token whenever a UPC instance signs
    in, so with a prefix per game only the prefix that ran last holds a live
    token. Seeding first means the game runs on the live one instead of
    whatever it kept from its own last run; capturing after means the *next*
    game gets a live one. Without both, signing into one title signed the user
    out of the others (GH #435).

    The capture is in a ``finally`` because a crashed or non-zero run still
    rotated the token — UPC signs in before the game starts — and leaving it
    stranded in this prefix is the exact failure being prevented. It is gated
    on the vault fingerprint taken here, *after* the seed and before UPC runs,
    so a run that never wrote the vault captures nothing: without that gate a
    failed launch fans the prefix's existing bytes out to every other prefix.

    All three moves are best-effort and run off the event loop: they touch the
    filesystem and wait for UPC to exit, and none may fail a launch.
    """
    await asyncio.to_thread(seed_before_launch, plan.prefix_path)
    before = await asyncio.to_thread(vault_fingerprint, plan.prefix_path)
    try:
        return await run_umu_with_retry(
            argv, env=plan.env, on_start=plan.on_process_start,
        )
    finally:
        await asyncio.to_thread(capture_after_exit, plan.prefix_path, before)


async def ubisoft_launch(plan: ProtonLaunchPlan) -> int:
    """Ubisoft launch."""
    logger.info(
        "[launcher.proton.ubisoft] launching %s",
        plan.context.game_key,
    )
    launcher_toast(
        "toasts.launcher.startingUbisoftGame",
        i18n_title_key="toasts.launcher.launchingGame",
        game_title=resolve_title(plan.context.game_key),
    )
    await _apply_epic_wrapper_fix(plan)
    if not await _inject_registry_keys(plan):
        logger.warning(
            "[launcher.proton.ubisoft] registry injection "
            "failed or skipped",
        )
    _apply_language_setup(plan)
    # Recover an empty/lost-pointer prefix before giving up (same routine the
    # install path uses) so a missing upc.exe doesn't just black-flash.
    upc_exe = await _resolve_or_recover_upc_exe(plan)
    if upc_exe is None:
        launcher_toast(
            "toasts.launcher.ubisoftPrefixNotReadyMessage",
            i18n_title_key="toasts.launcher.ubisoftPrefixNotReady",
            game_title=resolve_title(plan.context.game_key),
            severity="error",
        )
        raise GameFailedError(
            "upc.exe not found in the Ubisoft prefix — the per-game "
            "prefix may not be fully set up yet",
            subprocess_rc=127,
            context={"store": "ubisoft", "prefix": str(plan.prefix_path)},
        )
    uplay_id = os.environ.get("UPLAY_ID") or _uplay_id_from_id_map(
        plan.context.game_id,
    )
    argv = [str(plan.python_bin), str(plan.umu_wrapper), str(upc_exe)]
    if uplay_id:
        logger.info(
            "[launcher.proton.ubisoft] direct launch: "
            "uplay://launch/%s/0",
            uplay_id,
        )
        argv.append(f"uplay://launch/{uplay_id}/0")
    else:
        # No resolvable uplay id (id_map not seeded, or UUID-only "0"). The
        # old behaviour fell back to ``legendary launch`` — an Epic-only
        # codepath that can never launch a Ubisoft title. Open UPC bare into
        # the game's prefix instead so the user lands in their installed
        # library and can start the game, and surface why direct launch
        # didn't work. (Robust post-install id seeding should make this rare.)
        logger.warning(
            "[launcher.proton.ubisoft] no uplay id for %s — opening UPC "
            "bare (user can launch from their library)",
            plan.context.game_id,
        )
        launcher_toast(
            "toasts.launcher.ubisoftLaunchIdMissingMessage",
            i18n_title_key="toasts.launcher.ubisoftLaunchIdMissing",
            game_title=resolve_title(plan.context.game_key),
            severity="warning",
        )
    rc = await _run_upc_with_session_handoff(plan, argv)
    plan.state.game_exit_code = rc
    if rc == 0:
        return 0
    _raise_for_umu_rc(rc, plan)
    return rc

async def ubisoft_auth_launch(plan: ProtonLaunchPlan) -> int:
    """Open Ubisoft Connect (UPC) in the auth prefix so the user signs in.

    The auth shortcut is *not* a game launch: unlike :func:`ubisoft_launch`
    (which opens a specific title via ``uplay://launch/{id}/0``), this
    just runs UPC bare in the ``.upc-auth`` prefix and keeps the process
    alive until the user closes it. That's what stops the Steam shortcut
    from exiting immediately — the bug where "something happened but the
    shortcut closed". The plugin's session monitor captures the
    credentials UPC writes once sign-in completes.

    Run directly (not via the normal launch pipeline) on purpose: that
    pipeline runs ``ensure_prefix_initialized`` (now in
    ``orchestrator.launch_windows``, before the cloud sync-down) which can
    *reset* the prefix on a Proton family change — that would wipe the UPC
    install the plugin already built into the auth prefix.
    """
    logger.info(
        "[launcher.proton.ubisoft] auth launch — opening UPC in %s",
        plan.prefix_path,
    )
    # NOT a game launch — the generic "Launching Game" confused users.
    # Shared with Battle.net so the two clients say the same thing, and
    # worded by action so a cart press doesn't say "sign in".
    wrapper_clients.announce_client_open("ubisoft")
    upc_exe = _find_upc_exe(plan)
    if upc_exe is None:
        launcher_toast(
            "toasts.launcher.ubisoftAuthPrefixNotReadyMessage",
            i18n_title_key="toasts.launcher.ubisoftAuthPrefixNotReady",
            game_title="Ubisoft Connect",
            severity="error",
        )
        raise GameFailedError(
            "upc.exe not found in the Ubisoft auth prefix — the auth "
            "prefix may not be fully set up yet",
            subprocess_rc=127,
            context={"store": "ubisoft", "prefix": str(plan.prefix_path)},
        )
    argv = [str(plan.python_bin), str(plan.umu_wrapper), str(upc_exe)]
    logger.info("[launcher.proton.ubisoft] auth argv: %s", argv)
    rc = await run_umu_with_retry(
        argv, env=plan.env, on_start=plan.on_process_start,
    )
    plan.state.game_exit_code = rc
    logger.info("[launcher.proton.ubisoft] UPC auth session exited rc=%d", rc)
    return rc



async def _resolve_or_recover_upc_exe(plan: ProtonLaunchPlan) -> Path | None:
    """The install prefix's upc.exe, recovering an empty prefix first.

    When the resolved prefix has no upc.exe, recover before giving up so the
    user doesn't just see a black flash: first look for a populated prefix for
    this game under another storage base (the recorded pointer can be lost),
    then clone the prebuilt ``.template`` on demand and retarget the run env to
    it. Returns None only when both recovery routes fail.
    """
    upc_exe = _find_upc_exe(plan)
    if upc_exe is not None:
        return upc_exe
    recovered = find_recovered_prefix(plan.context.game_id)
    if recovered is not None:
        logger.info(
            "[launcher.proton.ubisoft] recovered populated prefix for "
            "%s at %s (resolved %s was empty)",
            plan.context.game_id,
            recovered,
            plan.prefix_path,
        )
        # The plan is frozen; retarget the run via its env (mutable dict)
        # so umu opens UPC in the recovered prefix.
        plan.env["WINEPREFIX"] = str(recovered)
        plan.env["STEAM_COMPAT_DATA_PATH"] = str(recovered)
        return find_upc_in(recovered)
    if await asyncio.to_thread(clone_template_into, plan.prefix_path):
        logger.info(
            "[launcher.proton.ubisoft] cloned .template into %s for %s",
            plan.prefix_path,
            plan.context.game_id,
        )
        return find_upc_in(plan.prefix_path)
    return None


async def ubisoft_install_launch(plan: ProtonLaunchPlan) -> int:
    """Open Ubisoft Connect (UPC) to install a game, via RunGame.

    The install shortcut is *not* a game launch: like
    :func:`ubisoft_auth_launch` it opens UPC and keeps the process alive
    until the user closes it, but it points UPC at the title's
    ``uplay://install/{id}`` deeplink (when the id resolves) so the
    install page opens directly; otherwise it opens UPC bare and the user
    picks the game. Because Steam launches this through ``RunGame``, UPC
    runs inside its own gamescope/XWayland session — so the window
    actually renders in Gaming Mode, which the old backend-subprocess
    spawn could not do (no session → invisible window).

    Run directly (NOT via the normal launch pipeline) on purpose — that
    pipeline runs ``ensure_prefix_initialized`` (in
    ``orchestrator.launch_windows``) which can *reset* the per-game prefix
    the plugin just bootstrapped UPC into. The plugin's download worker
    watches the prefix for the installed files and finalises the queue
    item; this handler does not report install success itself.
    """
    logger.info(
        "[launcher.proton.ubisoft] install launch — opening UPC in %s",
        plan.prefix_path,
    )
    launcher_toast(
        "toasts.launcher.installingUbisoftMessage",
        i18n_title_key="toasts.launcher.installingUbisoft",
        game_title="Ubisoft Connect",
    )
    # _resolve_or_recover_upc_exe returns a verified-existing path or None
    # (every recovery branch gates on is_file), so a None means the resolved
    # prefix was empty and both recovery routes failed.
    upc_exe = await _resolve_or_recover_upc_exe(plan)
    if upc_exe is None:
        launcher_toast(
            "toasts.launcher.ubisoftPrefixNotReadyMessage",
            i18n_title_key="toasts.launcher.ubisoftPrefixNotReady",
            game_title="Ubisoft Connect",
            severity="error",
        )
        raise GameFailedError(
            "upc.exe not found in the Ubisoft prefix — the per-game "
            "prefix may not be fully set up yet",
            subprocess_rc=127,
            context={"store": "ubisoft", "prefix": str(plan.prefix_path)},
        )
    uplay_id = os.environ.get("UPLAY_ID") or _uplay_id_from_id_map(
        plan.context.game_id,
    )
    argv = [str(plan.python_bin), str(plan.umu_wrapper), str(upc_exe)]
    if uplay_id:
        argv.append(f"uplay://install/{uplay_id}")
        logger.info(
            "[launcher.proton.ubisoft] install deeplink: uplay://install/%s",
            uplay_id,
        )
    else:
        logger.info(
            "[launcher.proton.ubisoft] no uplay id for %s — "
            "opening UPC bare for install",
            plan.context.game_id,
        )
    rc = await run_umu_with_retry(
        argv, env=plan.env, on_start=plan.on_process_start,
    )
    plan.state.game_exit_code = rc
    logger.info(
        "[launcher.proton.ubisoft] UPC install session exited rc=%d", rc,
    )
    return rc


def _apply_language_setup(plan: ProtonLaunchPlan) -> None:

    """Apply language setup."""
    try:
        from unifideck.config.config_manager import ConfigManager
        from unifideck.config.defaults_path import (
            resolve_defaults_config_path,
        )
        from unifideck.config.user_config_path import (
            resolve_user_config_path,
        )
        from unifideck.launcher.proton.language_setup import apply_ubisoft_language
        _cfg = ConfigManager(
            resolve_defaults_config_path(plan.context.plugin_dir),
            user_path=resolve_user_config_path(),
        )
        apply_ubisoft_language(
            str(plan.prefix_path),
            space_id=plan.context.game_id,
            config=_cfg,
        )
    except Exception as err:
        logger.warning(
            "[launcher.proton.ubisoft] language setup failed: %s",
            err,
        )
def _raise_for_umu_rc(rc: int, plan: ProtonLaunchPlan) -> None:
    """Raise for UMU rc."""
    if rc in {2, 74}:
        raise UmuRuntimeError(
            f"umu-run failed with unrecoverable code {rc}",
            context={"subprocess_rc": rc, "store": "ubisoft"},
        ) from None
    raise GameFailedError(
        f"Ubisoft game exited with code {rc}",
        subprocess_rc=rc,
        context={
            "store": "ubisoft",
            "game_id": plan.context.game_id,
        },
    ) from None
