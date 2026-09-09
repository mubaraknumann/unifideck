from __future__ import annotations

import logging
from pathlib import Path

from unifideck.launcher.frontend_bridge import launcher_toast
from unifideck.launcher.proton.infrastructure.core import ProtonLaunchPlan
from unifideck.launcher.proton.infrastructure.umu_runtime import run_umu_with_retry
from unifideck.launcher.types.errors import GameFailedError, UmuRuntimeError

logger = logging.getLogger(__name__)
def _read_amazon_fuel_args(work_dir: Path) -> list[str]:
    """Parse ``Main.Args`` from an Amazon ``fuel.json`` (launch params).

    Amazon ships a ``fuel.json`` in the install dir describing the
    launch command + args. Mirrors staging: strip ``//`` comments
    (fuel.json sometimes has them), read ``Main.Args``. Returns ``[]``
    on any problem — the exe still launches, just without extra args.
    """
    fuel = Path(work_dir) / "fuel.json"
    if not fuel.is_file():
        return []
    try:
        import json
        import re
        raw = fuel.read_text(encoding="utf-8", errors="replace")
        content = re.sub(r"//.*$", "", raw, flags=re.MULTILINE)
        data = json.loads(content)
        args = (data.get("Main") or {}).get("Args") or []
        return [str(a) for a in args]
    except Exception:
        logger.warning(
            "[launcher.proton.generic] fuel.json parse failed at %s", fuel,
        )
        return []
async def _gog_launch(plan: ProtonLaunchPlan) -> int:
    """GOG Windows launch — delegated to the GOG compat orchestrator.

    The orchestrator handles language, the Galaxy stub, the GOG
    redistributable/script setup (``gog_setup``), Comet, NVAPI, and the
    broken-launcher-stub fallback. GOG *native* games (start.sh) never
    reach here — they go through ``launch_native``.
    """
    from unifideck.launcher.proton.compat.gog import run_gog_launch
    return await run_gog_launch(plan)

async def _amazon_launch(plan: ProtonLaunchPlan) -> int:

    """Amazon launch."""
    # The prefix's Windows locale used to be applied here. It is store-
    # agnostic, so it moved to ``proton.dispatch`` and now runs for every
    # store instead of the three that happened to have grown a copy.
    # Amazon has no game-level language setting of its own — nile takes no
    # ``--lang`` — so the prefix locale is the whole of it for this store.
    #
    # Amazon games launch by running the resolved exe directly through
    # umu — matching staging. ``nile launch`` was the wrong port: nile
    # manages its own wine binary + install manifest and exits rc=1
    # here. fuel.json's ``Main.Args`` carry any required launch params.
    work_dir = plan.context.work_dir or plan.context.exe_path.parent
    fuel_args = _read_amazon_fuel_args(work_dir)
    cwd: Path | None = (
        plan.context.exe_path.parent
        if plan.context.exe_path.parent.is_dir()
        else None
    )
    argv: list[str] = []
    argv.extend([
        str(plan.python_bin),
        str(plan.umu_wrapper),
        str(plan.context.exe_path),
    ])
    argv.extend(fuel_args)
    argv.extend(plan.state.game_args)
    logger.info(
        "[launcher.proton.generic] Amazon direct exe launch: %s "
        "(fuel_args=%d)",
        plan.context.exe_path, len(fuel_args),
    )
    return await run_umu_with_retry(
        argv, env=plan.env, cwd=cwd, on_start=plan.on_process_start,
    )
async def _raw_exe_launch(plan: ProtonLaunchPlan) -> int:
    """Raw exe launch."""
    logger.info(
        "[launcher.proton.generic] raw exe launch: %s", plan.context.exe_path,
    )
    cwd: Path | None = None
    if plan.context.exe_path.parent.is_dir():
        cwd = plan.context.exe_path.parent
    argv: list[str] = []
    argv.extend([
        str(plan.python_bin),
        str(plan.umu_wrapper),
        str(plan.context.exe_path),
    ])
    argv.extend(plan.state.game_args)
    return await run_umu_with_retry(argv, env=plan.env, cwd=cwd, on_start=plan.on_process_start)
async def generic_launch(plan: ProtonLaunchPlan) -> int:
    """Generic launch."""
    store = plan.context.store
    if store == "gog":
        launcher_toast(
            "toasts.launcher.startingGogGame",
            i18n_title_key="toasts.launcher.launchingGame",
            game_title=plan.context.game_key,
        )
        rc = await _gog_launch(plan)
    elif store == "amazon":
        launcher_toast(
            "toasts.launcher.startingAmazonGame",
            i18n_title_key="toasts.launcher.launchingGame",
            game_title=plan.context.game_key,
        )
        rc = await _amazon_launch(plan)
    else:
        rc = await _raw_exe_launch(plan)
    plan.state.game_exit_code = rc
    if rc == 0:
        return 0
    if rc in {2, 74}:
        raise UmuRuntimeError(
            f"umu-run failed with unrecoverable code {rc}",
            context={"subprocess_rc": rc, "store": store},
        )
    raise GameFailedError(
        f"{store} game exited with code {rc}",
        subprocess_rc=rc,
        context={"store": store, "game_id": plan.context.game_id},
    )
