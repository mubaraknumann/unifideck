"""Well-known path helpers — locate the plugin's install directory.

The plugin can be installed in various places depending on
the environment (Decky's default location, a custom dev
install, a CI checkout). This module centralises the lookup
logic so every caller agrees on where the plugin lives.

Resolution order (first match wins):

1. ``$UNIFIDECK_PLUGIN_DIR`` env var (highest priority,
   for dev workflows);
2. ``$DECKY_PLUGIN_DIR``     env var (set by Decky at
   runtime);
3. ``~/homebrew/plugins/unifideck`` (Decky's default
   install location);
4. Walk up from ``__file__`` looking for a ``plugin.json``
   marker (works for ad-hoc checkouts).

If everything fails, the function logs at WARN and returns
the Decky default as a last-resort fallback rather than
raising — most callers can survive a stale path lookup
with degraded behaviour better than a hard crash.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)
_DECKY_DEFAULT_PATH = Path.home() / "homebrew" / "plugins" / "unifideck"

def resolve_plugin_dir(start: Path | None = None) -> Path:
    """Return the plugin's root directory using a 4-step fallback chain.

    See module docstring for the full resolution order.
    An invalid value at any step (env var pointing to a
    non-directory) logs at WARN and falls through to the
    next candidate.

    Args:
        start: optional search-start for the
            walk-up-looking-for-plugin.json fallback.
            Defaults to this file's location.

    Returns:
        Resolved directory ``Path``. Always returns
        something — the final fallback is the Decky default,
        even if it doesn't exist.
    """
    explicit = os.environ.get("UNIFIDECK_PLUGIN_DIR")
    if explicit:
        p = Path(explicit).expanduser()
        if p.is_dir():
            return p
        logger.warning(
            "[paths] UNIFIDECK_PLUGIN_DIR=%s is not a directory, ignoring",
            explicit,
        )
    decky = os.environ.get("DECKY_PLUGIN_DIR")
    if decky:
        p = Path(decky).expanduser()
        if p.is_dir():
            return p
    if _DECKY_DEFAULT_PATH.is_dir():
        return _DECKY_DEFAULT_PATH
    here = (start or Path(__file__)).resolve()
    for parent in here.parents:
        if (parent / "plugin.json").is_file():
            return parent
    logger.warning(
        "[paths] plugin directory could not be resolved, falling back to %s",
        _DECKY_DEFAULT_PATH,
    )
    return _DECKY_DEFAULT_PATH

def resolve_py_modules_dir() -> Path:
    """Return the ``py_modules/`` directory inside the plugin root.

    Trivial composition over ``resolve_plugin_dir`` — kept
    separate because callers reading vendored Python
    packages (aiohttp, websockets, …) want this path more
    often than the plugin root itself.

    Returns:
        ``<plugin_root>/py_modules`` path. May not exist
        on disk if the plugin root resolution itself
        failed; callers should ``.is_dir()`` before use.
    """
    return resolve_plugin_dir() / "py_modules"
