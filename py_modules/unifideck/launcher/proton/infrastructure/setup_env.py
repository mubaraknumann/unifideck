"""launcher/proton/infrastructure/setup_env.py — env for prefix-setup helpers.

A prefix-setup step (umu ``createprefix``, winetricks, the vcruntime regedit,
the GOG redist/script helpers) runs inside the GAME's prefix but is NOT the
game. Every way its environment has to diverge from ``plan.env`` was found the
hard way, once per site, because the ``env = dict(plan.env)`` recipe was
copy-pasted at four call sites with no shared owner. This module is that owner:
add the next divergence here and all four inherit it.

Callers layer their own extras on top of :func:`build_setup_env` — ``STORE``
for the GOG helpers, ``WINEPREFIX`` / ``UMU_RUNTIME_UPDATE`` for winetricks.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from unifideck.launcher.proton.infrastructure.core import ProtonLaunchPlan

# Game-side sidecar hooks. Proton acts on these for whatever process it is
# about to start, so they follow the env rather than the game — which means a
# setup helper inherits them and Proton dutifully starts the sidecar for
# ``scriptinterpreter.exe`` or ``regedit`` instead.
#
# ``PROTON_REMOTE_DEBUG_CMD`` is how CheatDeck attaches a trainer: it is set in
# the shortcut's Steam launch options, so it arrives via ``os.environ`` and
# rides ``plan.env`` into every helper that copies it. Proton's ``Session.run``
# (GE-Proton11-5 ~L2651) unconditionally ``Popen``s ``[wine_bin] + cmd`` before
# the target exe, for the ``run`` verb too. Its cleanup ``kill()`` (~L2706)
# reaps only the wine launcher stub — the Windows-side process survives under
# the prefix's wineserver. So each setup step leaks a live trainer, and the
# game launch that follows blocks in the ``wineserver -w`` below, waiting on
# processes the user cannot see. Observed on GOG first launch, where the
# helpers run in a loop (per redist, per product, per registry key).
_GAME_ONLY_VARS = ("PROTON_REMOTE_DEBUG_CMD",)


def build_setup_env(plan: ProtonLaunchPlan) -> dict[str, str]:
    """Return a copy of ``plan.env`` adjusted for a prefix-setup helper.

    Three divergences, each pinned by a regression test:

    ``GAMEID=umu-0``
        Generic — a setup helper is not the game, so it must not pull the
        game's per-title umu protonfix.

    ``PROTON_VERB=run``, never the inherited ``waitforexitandrun``
        This is the canonical explanation; other sites point here. Proton's
        ``waitforexitandrun`` runs ``wineserver -w`` FIRST (proton script
        ~L2111), which blocks until every process in the prefix exits.
        Proton's persistent ``steam.exe`` stub keeps that wineserver
        resident, so a second ``waitforexitandrun`` step — or a retry —
        deadlocks on the wait. That was the install-warmup hang: createprefix
        and regedit each stalling 120s, stacking wineservers on one lock.
        ``run`` skips ``wineserver -w`` entirely; setup steps operate on the
        prefix directly and never need to wait for a prior session.

    Game-only vars dropped
        See :data:`_GAME_ONLY_VARS`.

    Everything else is preserved deliberately, including ``PROTONPATH`` /
    ``STEAM_COMPAT_DATA_PATH`` from ``proton_prepare`` and
    ``PRESSURE_VESSEL_FILESYSTEMS_RW`` (dropping that one would cost the
    helper its filesystem access across the container boundary).

    ``plan.env`` itself is never mutated — the real game launch still gets the
    sidecar and the inherited verb.
    """
    env = dict(plan.env)
    for var in _GAME_ONLY_VARS:
        env.pop(var, None)
    env["GAMEID"] = "umu-0"
    env["PROTON_VERB"] = "run"
    return env
