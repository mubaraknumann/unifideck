"""launcher/proton/setup_proton.py — which Proton a game's prefix is set up and run on.

One prefix, one Proton. A prefix set up by one Proton and then run by another
keeps symlinks into the first one's shared install for every file the second
does not ship, and the second Proton's Wine writes through them into the
first one's tree (measured: Proton Experimental rewrote a DLL inside the
shared GE-Proton, which every GE game then flagged as damaged).

So the Proton is decided once, here, before any setup step runs, and the
setup steps and the launch all use it:

* a choice wins: the game's Steam Compatibility setting, an older per-game
  pin, or the Unifideck-wide default;
* a fresh install uses the GE it was installed with (``ctx.proton_tool``);
* otherwise nobody chose. A game whose prefix another Proton already set up
  keeps that Proton. A game with no prefix yet, or one GE set up, runs on
  GE, and the launcher writes that GE into the game's Steam Compatibility
  setting so it shows there and the next launch reads it.

Ubisoft and Battle.net never switch: their client and games share a prefix.
Stdlib-only at import: this runs under the system python out-of-process.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from unifideck.launcher.wrapper_stores import is_wrapper_store

if TYPE_CHECKING:
    from unifideck.launcher.types.context import LaunchContext

logger = logging.getLogger(__name__)

_CONFIG_INFO = "config_info"


def is_ge_tool(name: str | None) -> bool:
    """Whether a tool id or directory name is a GE-Proton build.

    Same test as the fresh-install pick: ``GE-Proton11-12`` and an external
    manager's ``Proton-GE Latest`` both match, Proton Experimental does not.
    """
    flat = "".join(ch for ch in (name or "").lower() if ch.isalnum())
    return "geproton" in flat or "protonge" in flat


def prefix_set_up_by(prefix_root: Path) -> Path | None:
    """The Proton directory that last set up this prefix, or None if never.

    Proton writes ``config_info`` on every prefix setup; its lines after the
    version are paths inside the Proton that wrote it
    (``<tool>/files/share/fonts/``, ``<tool>/files/lib/``). That is what the
    game actually runs on, which ``.unifideck_proton_version`` is not: it
    records who *created* the prefix and is never updated when a launch
    under another Proton upgrades it.
    """
    try:
        lines = (prefix_root / _CONFIG_INFO).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in lines[1:]:
        path = Path(line.strip())
        parts = path.parts
        if "files" in parts:
            return Path(*parts[: parts.index("files")])
    return None


def _created_by(prefix_root: Path) -> str | None:
    """The tool ``prefix_init`` recorded when it created the prefix, or None."""
    from unifideck.launcher.proton.compat.prefix_init import _MARKER_NAME

    try:
        return (prefix_root / _MARKER_NAME).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def choose(
    ctx: LaunchContext,
    prefix_root: Path,
    selected: tuple[Path, str, str],
) -> tuple[Path, str, bool]:
    """``(path, tool, write_to_steam)`` for this game's setup and launch.

    ``selected`` is ``select_proton_with_source``'s answer. ``write_to_steam``
    is True when the launcher picked GE for a game nobody chose for, so the
    caller records it in Steam's setting.
    """
    from unifideck.launcher.proton.infrastructure.selector import CHOSEN_SOURCES

    path, tool, source = selected
    chosen = getattr(ctx, "proton_tool", None)
    if chosen:
        fresh = _resolve(chosen)
        if fresh is not None:
            return fresh, chosen, False  # the install path sets it in Steam
        logger.warning("[setup_proton] %s not found for %s; using the selected Proton", chosen, ctx.game_key)
    if source in CHOSEN_SOURCES or is_wrapper_store(ctx.store):
        return path, tool, False
    set_up_by = prefix_set_up_by(prefix_root)
    created_by = _created_by(prefix_root)
    other = next(
        (name for name in (set_up_by.name if set_up_by else None, created_by) if name and not is_ge_tool(name)),
        None,
    )
    if other is not None:
        # Another Proton made or last ran this prefix: the game keeps what it
        # runs on. Moving it to GE would also be a Proton-family change,
        # which resets the prefix.
        logger.info("[setup_proton] %s keeps %s: its prefix belongs to %s", ctx.game_key, tool, other)
        return path, tool, False
    ge = _ge_for(set_up_by)
    if ge is None:
        return path, tool, False
    logger.info(
        "[setup_proton] %s has no Proton set (%s); using %s",
        ctx.game_key, "no prefix yet" if set_up_by is None else f"prefix set up by {set_up_by.name}", ge[1],
    )
    return ge[0], ge[1], True


def _resolve(tool: str) -> Path | None:
    """The ``proton`` script for ``tool``, or None when it is not installed."""
    from unifideck.launcher.proton.infrastructure.selector import resolve_proton_path

    return resolve_proton_path(tool)


def _ge_for(set_up_by: Path | None) -> tuple[Path, str] | None:
    """The GE this game runs on: the one that set up its prefix, else the latest.

    A prefix GE already set up stays on that same build while it is
    installed, so a game never changes GE behind the user's back. None when
    no GE is available (offline, none installed): the caller then keeps the
    selected Proton.
    """
    if set_up_by is not None:
        script = _resolve(set_up_by.name)
        if script is not None:
            return script, set_up_by.name
    from unifideck.launcher.proton.infrastructure.selector import select_managed_ge_proton

    try:
        path, tool = select_managed_ge_proton()
    except Exception:
        logger.warning("[setup_proton] no GE-Proton available", exc_info=True)
        return None
    return (path, tool) if is_ge_tool(tool) else None
