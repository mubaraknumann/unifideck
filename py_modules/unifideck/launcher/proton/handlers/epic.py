from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from unifideck.launcher.frontend_bridge import launcher_toast
from unifideck.launcher.proton.compat.epic_cleanup import cleanup_epic_artifacts
from unifideck.launcher.proton.infrastructure.core import ProtonLaunchPlan
from unifideck.launcher.proton.infrastructure.umu_runtime import run_umu_with_retry
from unifideck.launcher.types.errors import GameFailedError, UmuRuntimeError

logger = logging.getLogger(__name__)

# Last resort for ``-epiclocale`` when the user's language can't be read.
# Matches legendary's own default (``language_code, country_code = 'en','US'``).
_FALLBACK_EPIC_LANG = "en"


def _rockstar_play_exe_rel(plan: ProtonLaunchPlan) -> str | None:
    """The ``--override-exe`` target (relative) for RDR2/GTA5, else None.

    Prefers the launch shim ``compat.rockstar_egs`` just wrote, which starts
    the game **through** the fake ``EpicGamesLauncher.exe``. That indirection
    is the whole point: launching ``PlayGTAV.exe``/``PlayRDR2.exe`` directly
    is the reported failure (Rockstar launcher finds the game once, refuses
    to start it, then stops finding it) because the Epic entitlement is never
    verified.

    Falls back to the bare Play exe if the shim isn't on disk — same
    behaviour as before, so a shim-write failure degrades instead of
    breaking the launch outright. A user's explicit "Change executable"
    still wins (checked first in ``_resolve_exe_override``), which is what
    lets a hand-written ``fix.bat`` keep working.
    """
    from unifideck.launcher.proton.compat.rockstar_egs import LAUNCH_SHIM_NAME
    from unifideck.launcher.proton.fixes.game_fixes import (
        resolve_rockstar_play_exe,
    )
    work_dir = plan.context.work_dir
    play_exe = resolve_rockstar_play_exe(
        plan.context.game_id, plan.state.umu_id, plan.context.exe_path.name,
        work_dir,
    )
    if not play_exe:
        return None
    if work_dir and (Path(work_dir) / LAUNCH_SHIM_NAME).is_file():
        return LAUNCH_SHIM_NAME
    logger.warning(
        "[launcher.proton.epic] Rockstar launch shim absent — falling back to "
        "direct %s launch (Rockstar launcher may not detect the install)",
        play_exe,
    )
    return play_exe


def _resolve_exe_override(plan: ProtonLaunchPlan) -> Path | None:
    """Resolve exe override."""
    from unifideck.launcher.proton.fixes.game_fixes import get_exe_override
    # User "Change executable" / curated MANUAL_FIXES wins; otherwise the
    # Rockstar Play exe for RDR2/GTA5 (None for every other Epic game).
    rel = get_exe_override(plan.context.game_id) or _rockstar_play_exe_rel(plan)
    if not rel:
        return None
    installed = Path(
        "~/.config/legendary/installed.json",
    ).expanduser()
    if not installed.is_file():
        return None
    try:
        with installed.open() as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    install_path = (
        data.get(plan.context.game_id, {}).get("install_path")
    )
    if not install_path:
        return None
    full = Path(install_path) / rel
    return full if full.is_file() else None

async def _run_epic_prerequisites(plan: ProtonLaunchPlan) -> None:
    """Run epic prerequisites."""
    from unifideck.launcher.proton.fixes.epic_prerequisites import (
        apply_epic_prerequisites,
    )
    try:
        await apply_epic_prerequisites(plan)
    except Exception:
        logger.exception(
            "[launcher.proton.epic] prerequisites step crashed "
            "(non-fatal)",
        )


async def epic_launch(plan: ProtonLaunchPlan) -> int:

    """Epic launch."""
    logger.info("[launcher.proton.epic] launching %s", plan.context.game_key)
    launcher_toast(
        "toasts.launcher.startingEpicGame",
        i18n_title_key="toasts.launcher.launchingGame",
        game_title=plan.context.game_key,
    )
    cleanup_epic_artifacts(plan)
    await _run_epic_prerequisites(plan)
    # Rockstar-on-Epic (RDR2/GTA5) only: fake EpicGamesLauncher.exe + the
    # com.epicgames.launcher protocol handler. No-op for every other Epic
    # title (gated on the umu id), so the standard flow is unchanged.
    from unifideck.launcher.proton.compat.rockstar_egs import (
        apply_rockstar_egs_setup,
    )
    apply_rockstar_egs_setup(plan)
    legendary_bin, env = await _prepare_epic_env(plan)
    rc = await _run_epic_game(plan, legendary_bin, env)
    return _finish_epic_launch(plan, rc)


async def _run_epic_game(
    plan: ProtonLaunchPlan, legendary_bin: str, env: dict[str, str],
) -> int:
    """Resolve legendary's launch recipe, then run umu-run ourselves.

    UD-126: ``legendary launch`` fire-and-forgets the ``--wrapper``
    command (``subprocess.Popen`` with no ``wait()``), so awaiting
    legendary returned ~2s after the game was forked and this process
    exited while the game was still starting. Steam ended the session
    there — and in Gaming Mode gamescope will not raise a window whose
    app Steam no longer considers running, which is why a slow-starting
    title came up as audio with no window while Desktop Mode looked fine.

    ``--json`` hands us the same parameters legendary was about to spawn,
    so we spawn them ourselves and await the real umu-run: Epic now
    behaves exactly like ``handlers/generic.py`` does for every other
    store, and ``rc`` is the game's own exit code.

    One consequence worth knowing: ``run_umu_with_retry`` may retry the
    same argv, and ``egl_parameters`` carries a single-use Epic exchange
    code. That is fine for the failure this retry exists for — a umu
    bootstrap failure (rc 2/74/127) means the game never reached EGS, so
    the code is still unused — and the long-session guard in
    ``umu_runtime._is_recoverable`` keeps the window small for the rest.
    """
    from unifideck.launcher.proton.compat.epic_launch_params import (
        build_umu_argv,
        maybe_run_pre_launch,
        merge_environment,
        resolve_cwd,
        resolve_launch_params,
    )
    params = await resolve_launch_params(
        _build_legendary_argv(plan, legendary_bin, json_mode=True), env,
    )
    game_env = merge_environment(env, params)
    await maybe_run_pre_launch(params, game_env)
    return await run_umu_with_retry(
        build_umu_argv(plan, params),
        env=game_env,
        cwd=resolve_cwd(params),
        on_start=plan.on_process_start,
    )


async def _prepare_epic_env(
    plan: ProtonLaunchPlan,
) -> tuple[str, dict[str, str]]:
    """Resolve the legendary binary + env, applying the EOS overlay once.

    The EOS/EGS overlay (needed by some titles, e.g. Football Manager)
    is best-effort and never blocks the launch.
    """
    from unifideck.launcher.proton.compat.epic import (
        apply_eos_overlay,
        build_legendary_env,
        resolve_legendary_bin,
        resolve_legendary_config_path,
    )
    config_path = resolve_legendary_config_path()
    legendary_bin = resolve_legendary_bin(plan.context.plugin_dir)
    try:
        await apply_eos_overlay(plan, legendary_bin, config_path)
    except Exception:
        logger.exception(
            "[launcher.proton.epic] EOS overlay step failed (non-fatal)",
        )
    return legendary_bin, build_legendary_env(plan, config_path)


def _resolve_epic_language(plan: ProtonLaunchPlan) -> str:
    """Two-letter code for legendary's ``--language`` → ``-epiclocale=``.

    ``-epiclocale`` is how the Epic Games Launcher itself tells a game
    which language to run in, and legendary reproduces it in the
    ``egl_parameters`` we forward. This used to be hardcoded ``"en"``
    (behind an ``EPIC_LANG`` env var nothing ever set), so **every** Epic
    game launched in English no matter what the user had chosen — the
    UD-101 / UD-041 reports, and why a title installed with an Italian
    audio pack still came up in English.

    legendary resolves the final value as
    ``config.get(app_name, 'language', fallback=<this>)``, so a per-game
    choice recorded at install time (``legendary.write_app_language``)
    deliberately outranks the value returned here — this is the default
    for games with no recorded preference, i.e. the user's Unifideck
    language. ``EPIC_LANG`` is kept as an explicit escape hatch.
    """
    override = os.environ.get("EPIC_LANG")
    if override:
        logger.info(
            "[launcher.proton.epic] EPIC_LANG override: %s", override,
        )
        return override
    try:
        from unifideck.config.config_manager import ConfigManager
        from unifideck.config.defaults_path import resolve_defaults_config_path
        from unifideck.config.user_config_path import resolve_user_config_path
        from unifideck.launcher.proton.language_setup import (
            get_unifideck_language,
        )
        # Both arguments matter, and neither was here before.
        # ``resolve_defaults_config_path`` finds the bundled config on a
        # Decky CLI build, which flattens ``defaults/`` to the install
        # root; the hardcoded nested path found nothing there.
        # ``user_path`` is what makes this see the language the user
        # actually PICKED. Without them the merged view was the hardcoded
        # _FALLBACK alone, so the setting had no effect on Epic titles —
        # and it still looked right on any machine already in that
        # language, which is why it survived casual testing.
        config = ConfigManager(
            resolve_defaults_config_path(plan.context.plugin_dir),
            user_path=resolve_user_config_path(),
        )
        locale_tag = get_unifideck_language(config)
    except Exception as err:
        logger.warning(
            "[launcher.proton.epic] language resolution failed (%s); "
            "falling back to %s", err, _FALLBACK_EPIC_LANG,
        )
        return _FALLBACK_EPIC_LANG
    # legendary documents --language as a two-letter code and its own
    # default is the pre-dash half of the locale, so match that.
    code = locale_tag.split("-", maxsplit=1)[0].strip().lower()
    logger.info(
        "[launcher.proton.epic] language %s → -epiclocale=%s",
        locale_tag, code or _FALLBACK_EPIC_LANG,
    )
    return code or _FALLBACK_EPIC_LANG


def _build_legendary_argv(
    plan: ProtonLaunchPlan, legendary_bin: str, *, json_mode: bool = False,
) -> list[str]:
    """Assemble the ``legendary launch`` argv (offline, language, overrides).

    ``json_mode`` appends ``--json``, which makes legendary print the
    resolved launch parameters and exit instead of forking the game (see
    :func:`_run_epic_game`). User wrappers are dropped for that call —
    a Steam launch-option wrapper (gamemoderun, mangohud…) belongs on the
    game, not on a metadata query — and are re-applied by
    ``epic_launch_params.build_umu_argv``.
    """
    from unifideck.launcher.proton.compat.epic import detect_offline
    argv: list[str] = []
    argv.extend([
        legendary_bin,
        "launch",
        plan.context.game_id,
        "--no-wine",
        "--skip-version-check",
    ])
    if json_mode:
        argv.append("--json")
    if detect_offline():
        argv.append("--offline")
        logger.info("[launcher.proton.epic] offline mode — passing --offline")
    argv.extend([
        "--wrapper",
        # This string comes back verbatim as the JSON's ``launch_command``
        # and becomes the head of the argv we spawn ourselves.
        #
        # The ``env -u`` prefix predates that: legendary used to fork this
        # wrapper itself and could hand down its own bundled
        # LD_LIBRARY_PATH/LD_PRELOAD instead of the clean env it was
        # launched with, and that pollution rode umu-run into the
        # pressure-vessel container, breaking the container's own python3
        # ("libz.so.1", umu rc=127). We now own the spawn and
        # ``umu_runtime._strip_loader_env`` pops both there, so this is
        # belt-and-suspenders — kept because it costs nothing and keeps
        # the exec line byte-identical to the pre-UD-126 one.
        f"env -u LD_LIBRARY_PATH -u LD_PRELOAD {plan.python_bin} {plan.umu_wrapper}",
        "--language",
        _resolve_epic_language(plan),
    ])
    exe_override = _resolve_exe_override(plan)
    if exe_override:
        argv.extend(["--override-exe", str(exe_override)])
        logger.info(
            "[launcher.proton.epic] using EXE override: %s", exe_override,
        )
    if plan.state.game_args:
        argv.append("--")
        argv.extend(plan.state.game_args)
    return argv


def _finish_epic_launch(plan: ProtonLaunchPlan, rc: int) -> int:
    """Record the exit code; raise on unrecoverable failures.

    Since UD-126 ``rc`` is the **game's** exit code, not legendary's:
    :func:`_run_epic_game` awaits the umu-run it spawned itself, so this
    process lives exactly as long as the game and Steam tracks the
    session (window focus in Gaming Mode, Stop, playtime, cloud sync-up).
    Identical handling to ``generic_launch`` — Epic is no longer special.

    Historical note for anyone tempted to reintroduce a wait loop: an
    early 0.7 build "waited" by polling ``pgrep -f
    steam-runtime-launch-client``, which matches Steam's OWN container
    manager in Gaming Mode and hung the launcher forever. No process
    matching is involved here; we hold the pid.
    """
    plan.state.game_exit_code = rc
    if rc == 0:
        return 0
    if rc in {2, 74}:
        raise UmuRuntimeError(
            f"umu-run failed with unrecoverable code {rc}",
            context={"subprocess_rc": rc, "store": "epic"},
        )
    raise GameFailedError(
        f"Epic game exited with code {rc}",
        subprocess_rc=rc,
        context={"store": "epic", "game_id": plan.context.game_id},
    )
