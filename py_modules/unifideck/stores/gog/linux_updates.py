"""Update tracking for GOG Linux-native installs.

GOG serves Linux builds only as installers, never through the
content-system builds API (``os/linux/builds`` answers 404 "Unsupported
OS"). The update checker used to compare every install against the
**Windows** build id and run ``gogdl update --platform windows``, so a
Linux install always showed "update available" and the update wrote the
Windows build into the same folder, next to the Linux one.

A Linux install is tracked by its installer *version* instead, the
``version`` GOG's products API lists for the Linux installer and the
``versionName`` gogdl reports. We record it in the ``.unifideck-id``
marker at install and after each update. ``gameinfo`` (line 2) is only a
fallback: it matches the installer version for some games
(``1.0.2.1``) and uses a different scheme for others (``2.0.0.3``
against ``(gog-3)``), so a mismatch there proves nothing.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .http import fetch_json_get

logger = logging.getLogger(__name__)

MARKER_KEY = "linux_installer_version"
_MARKER_NAME = ".unifideck-id"


def pick_linux_installer_version(
    product: dict[str, Any], language: str | None,
) -> str | None:
    """The version of the Linux installer gogdl would pick for ``language``.

    Mirrors gogdl's ``find_matching_installer``: the only installer, else
    the one in the install language, else English.
    """
    downloads = product.get("downloads")
    installers = downloads.get("installers") if isinstance(downloads, dict) else None
    linux = [
        i for i in (installers or [])
        if isinstance(i, dict) and i.get("os") == "linux"
    ]
    if not linux:
        return None
    lang = (language or "").split("-")[0].lower()
    # A lone installer falls through to ``linux[0]`` whatever its language.
    chosen = next((i for i in linux if i.get("language") == lang), None)
    if chosen is None:
        chosen = next((i for i in linux if i.get("language") == "en"), linux[0])
    version = chosen.get("version")
    return str(version) if version else None


async def fetch_linux_installer_version(
    game_id: str, language: str | None, *,
    api_url: str, user_agent: str, bearer: str | None = None,
) -> str | None:
    """The current Linux installer version for ``game_id``, or None."""
    product = await fetch_json_get(
        f"{api_url}/products/{game_id}?expand=downloads",
        bearer=bearer,
        user_agent=user_agent,
        timeout=10.0,
        log_prefix=f"[GOGLinuxUpdates] {game_id}",
    )
    if not isinstance(product, dict):
        return None
    return pick_linux_installer_version(product, language)


def read_marker(install_path: str) -> dict[str, Any]:
    """The install's ``.unifideck-id`` marker, or ``{}``."""
    try:
        data = json.loads((Path(install_path) / _MARKER_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def gameinfo_version(install_path: str) -> str | None:
    """Line 2 of GOG's ``gameinfo`` file, or None."""
    try:
        lines = (Path(install_path) / "gameinfo").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    if len(lines) < 2:
        return None
    return lines[1].strip() or None


def record_version(install_path: str, version: str) -> bool:
    """Store the installed Linux installer version in the marker."""
    marker = Path(install_path) / _MARKER_NAME
    data = read_marker(install_path)
    data[MARKER_KEY] = version
    try:
        marker.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        logger.warning("[GOGLinuxUpdates] could not record version in %s", marker)
        return False
    return True


def linux_update_state(install_path: str, remote: str | None) -> bool | None:
    """True when an update exists, False when current, None when unknown.

    Unknown covers installs made before the version was recorded whose
    ``gameinfo`` uses a different scheme: offering an update we cannot
    justify is how the Windows build ended up in Linux installs.
    """
    if remote is None:
        return None
    recorded = read_marker(install_path).get(MARKER_KEY)
    if recorded:
        return str(recorded) != remote
    if gameinfo_version(install_path) == remote:
        return False
    return None
