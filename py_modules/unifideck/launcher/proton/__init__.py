"""launcher.proton — Proton-based launch orchestration.

Public surface used by the dispatcher: handler functions per
store, the ``ProtonLaunchPlan`` dataclass, selector helpers for
finding the right Python / Proton version, and UMU-runtime
cache management.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from unifideck.launcher.proton.handlers.battlenet import battlenet_launch
from unifideck.launcher.types.errors import UmuRuntimeError

from .handlers.epic import epic_launch
from .handlers.generic import generic_launch
from .handlers.ubisoft import ubisoft_launch
from .infrastructure.core import ProtonLaunchPlan, proton_prepare
from .infrastructure.selector import (
    find_python_3_10_plus,
    resolve_proton_path,
    select_managed_ge_proton,
    select_proton_version,
)
from .infrastructure.umu_runtime import (
    UMU_CACHE_DIR,
    cleanup_umu_runtime_cache,
    ensure_umu_runtime_ready,
    repair_incomplete_umu_runtime,
    run_umu_with_retry,
    unrecoverable_runtime_variants,
)
from .prefix_setup import setup_prefix

logger = logging.getLogger(__name__)


def _apply_prefix_language(plan: ProtonLaunchPlan) -> None:
    """Write the user's language into the prefix's Windows locale.

    Swallows everything. A game in the wrong language is a bad day; a game
    that will not start is a worse one, and this is the only step in
    :func:`dispatch` whose failure costs nothing but a preference.

    Imports are local for the same reason the store handlers' were: this
    module is imported at launcher start-up under the system python, and the
    config stack it pulls in is not needed until a launch actually happens.
    """
    try:
        from unifideck.config.config_manager import ConfigManager
        from unifideck.config.defaults_path import resolve_defaults_config_path
        from unifideck.config.user_config_path import resolve_user_config_path
        from unifideck.launcher.proton.language_setup import (
            apply_prefix_language,
        )

        config = ConfigManager(
            resolve_defaults_config_path(plan.context.plugin_dir),
            user_path=resolve_user_config_path(),
        )
        apply_prefix_language(str(plan.prefix_path), config=config)
    except Exception as err:
        logger.warning("[launcher.proton] prefix language setup failed: %s", err)


# Stores with a launch handler of their own; everything else runs through
# ``generic_launch``. A map rather than an if-chain so a test can assert the
# coverage: for a *wrapper* store an absent row is not a graceful fallback but
# a silent failure — ``generic_launch`` expects an exe path the vendor client
# owns, so the game never starts. See
# ``tests/unit/test_wrapper_store_dispatch_coverage.py``.
_STORE_LAUNCHERS: dict[str, Callable[[ProtonLaunchPlan], Awaitable[int]]] = {
    "battlenet": battlenet_launch,
    "ubisoft": ubisoft_launch,
    "epic": epic_launch,
}


async def dispatch(plan: ProtonLaunchPlan) -> int:
    """Dispatch.

    Routes the prepared plan to the per-store handler, which adds any
    store-specific compatibility (Epic EOS overlay, GOG galaxy stub, Amazon
    fuel args) and runs the game through umu-run.

    Prefix creation AND generic compat (createprefix + winetricks + VC++
    registry fix, with the managed-GE recovery ladder + pin) are NOT done
    here: the orchestrator runs the canonical :func:`setup_prefix` earlier
    (Phase 1.5), before the cloud sync-down, so the save dir resolves out of
    ``drive_c`` on the first launch and the exact same self-healing setup runs
    at launch as at install-time warmup. Running compat here too would
    double-run it (and its proton-change toast), so it lives in the single
    ``setup_prefix`` call in ``orchestrator.launch_windows``.
    """
    # Self-heal a half-downloaded umu runtime (payload present but the
    # umu/_v2-entry-point link missing) before anything spawns umu-run this
    # launch (UD-084). Store-agnostic, and a cheap no-op stat when healthy.
    repair_incomplete_umu_runtime()

    # If a variant is STILL broken after that repair, umu could not download
    # it and every umu-run this launch will die in build_command with
    # "Runtime Platform missing or download incomplete". umu exits 0 on that
    # path, so without this check the launcher reported SUCCESS for a game
    # that never started — the user saw a "Retrying UMU" toast, then nothing,
    # with no error at all. Fail loudly instead.
    #
    # The historical cause: umu <=1.3.0 installed from
    # ``images/latest-public-beta``, a symlink dir the repo now answers 403.
    # umu 1.4.x reads ``images/latest-public-beta.txt`` and fetches from the
    # numbered dir it names, which serves fine — so the bundled umu bump is
    # the actual fix. This check remains as the guard that a runtime we
    # cannot use is reported rather than silently "succeeding".
    broken = unrecoverable_runtime_variants()
    if broken:
        raise UmuRuntimeError(
            "Steam Linux Runtime "
            f"({', '.join(broken)}) is incomplete and umu could not "
            "re-download it — the game cannot start. See game.log for umu's "
            "error.",
            context={"variants": broken, "umu_cache": str(UMU_CACHE_DIR)},
        )

    # The prefix's Windows locale, for every store. Before the branch on
    # purpose: a prefix's ``Control Panel\International`` is prefix state, not
    # store state, and running here means it lands before any handler starts
    # something that would take ownership of the registry — which is what
    # Battle.net's phase A does. Refuses rather than lies when the prefix is
    # already busy; see ``language_setup.registry_io``.
    _apply_prefix_language(plan)

    handler = _STORE_LAUNCHERS.get(plan.context.store, generic_launch)
    return await handler(plan)


__all__ = [
    "UMU_CACHE_DIR",
    "ProtonLaunchPlan",
    "battlenet_launch",
    "cleanup_umu_runtime_cache",
    "dispatch",
    "ensure_umu_runtime_ready",
    "epic_launch",
    "find_python_3_10_plus",
    "generic_launch",
    "proton_prepare",
    "repair_incomplete_umu_runtime",
    "resolve_proton_path",
    "run_umu_with_retry",
    "select_managed_ge_proton",
    "select_proton_version",
    "setup_prefix",
    "ubisoft_launch",
    "unrecoverable_runtime_variants",
]
