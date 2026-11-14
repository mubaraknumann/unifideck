"""``maxima-cli`` as both processes see it: where it is, its data folder, its env.

Shared by the plugin (``stores/ea/``) and the out-of-process launcher
(``launcher/ea/``), so neither keeps its own copy. Standard library only: the
launcher runs on the system Python.

``maxima-cli`` is our build of Maxima (the ``unifideck-maxima`` repo). Every run
gets our data folder and starts in an empty private folder: upstream cloud sync
globs save patterns against the working directory (that repo's ISSUES.md E-003).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

RUN_DIR_NAME = "run"


def data_dir() -> Path:
    """``~/.local/share/unifideck/ea/maxima``, resolved at call time."""
    return Path.home() / ".local/share/unifideck/ea/maxima"
#: Config key naming a development build instead of the bundled binary.
CLI_OVERRIDE_KEY = "stores.ea.cli_path"
BUNDLED_CLI = Path("bin/maxima-cli")


def find_cli(plugin_dir: str | Path | None, override: str = "") -> Path | None:
    """The ``maxima-cli`` to run: ``override`` when executable, else the bundled one."""
    if override:
        path = Path(override).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            logger.warning("[EA] using the development maxima-cli at %s", path)
            return path
        logger.warning("[EA] %s %s is not an executable file; ignoring it", CLI_OVERRIDE_KEY, path)
    if plugin_dir:
        bundled = Path(plugin_dir) / BUNDLED_CLI
        if bundled.is_file() and os.access(bundled, os.X_OK):
            return bundled
    logger.warning("[EA] maxima-cli not found at %s, so EA is unavailable", BUNDLED_CLI)
    return None


def prepare_run_dir(data: Path | None = None) -> Path:
    """Create the data and run folders (0700) and return the run folder."""
    data = data or data_dir()
    run_dir = data / RUN_DIR_NAME
    for folder in (data, run_dir):
        folder.mkdir(parents=True, exist_ok=True)
        os.chmod(folder, 0o700)
    return run_dir


def base_env(data: Path | None = None) -> dict[str, str]:
    """Variables every ``maxima-cli`` run needs."""
    return {
        "MAXIMA_DATA_DIR": str(data or data_dir()),
        "MAXIMA_PACKAGED": "1",
        "MAXIMA_DISABLE_QRC": "1",
    }


def prefix_env(prefix: Path, proton_script: Path, umu_run: Path) -> dict[str, str]:
    """Variables pointing Maxima at one game's prefix, Proton and umu."""
    return {
        "MAXIMA_WINE_PREFIX": str(prefix),
        "MAXIMA_PROTON": str(proton_script.parent),
        "MAXIMA_UMU": str(umu_run),
    }
