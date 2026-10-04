"""Which file in an itch.io install to launch.

itch.io gives no launch target for most games: the developer uploads a zip,
and only a minority add an ``.itch.toml`` manifest. So, in order:

1. the manifest's ``play`` action for this OS, when there is one;
2. the shared scorer (``stores/shared/launch_target``), native pool first for
   a Linux upload;
3. for a *Windows* upload, a native launcher still wins when it is named by
   convention or after the game. Ren'Py "PC" zips ship ``lib/linux-x86_64``
   and ``<Game>.sh`` beside the ``.exe`` (measured on Hampton Court), and the
   user asked for native first. Any other script in a Windows zip does not.

butler's own ``configure`` verdict is deliberately not the answer: on an NW.js
build it listed ``nacl_helper`` before the real ``nw`` binary.
"""
from __future__ import annotations

import logging
import re
import tomllib
from pathlib import Path
from typing import Any

from unifideck.stores.shared.launch_target import find_executable, is_named_native_entry

from .uploads import upload_platforms

logger = logging.getLogger(__name__)

MANIFEST_NAME = ".itch.toml"
_EXT_TOKEN = "{{EXT}}"  # noqa: S105 - butler manifest placeholder, not a secret
_EXT_FOR = {"linux": "", "windows": ".exe"}


def upload_platform(upload: dict[str, Any]) -> str:
    """``"linux"`` or ``"windows"`` for an upload; Linux wins when both are set.

    Untagged uploads use the filename guess from ``uploads.upload_platforms``.
    """
    platforms = upload_platforms(upload)
    return "linux" if platforms.get("linux") else "windows"


def resolve_launch_target(install_dir: str, platform: str, title: str) -> str | None:
    """Absolute path of the file to launch, or None when nothing is runnable."""
    manifest = manifest_play_target(install_dir, platform)
    if manifest:
        return manifest
    if platform == "linux":
        return find_executable(install_dir, prefer_native=True, title=title)
    native = find_executable(install_dir, prefer_native=True, title=title)
    if native and not native.lower().endswith(".exe") and is_named_native_entry(native, title):
        logger.info("[itch] %s: Windows upload carries a native launcher (%s); "
                    "running it natively", title, Path(native).name)
        return native
    return find_executable(install_dir, prefer_native=False, title=title)


def manifest_play_target(install_dir: str, platform: str) -> str | None:
    """The ``.itch.toml`` ``play`` action for *platform*, if the file names one.

    Schema (butler ``hush/manifest``): ``[[actions]]`` with ``name``, ``path``
    (relative, may contain ``{{EXT}}``, or a URL), optional ``platform``.
    URL actions and actions for another OS are skipped. An unreadable
    manifest is logged and ignored, and the heuristic still runs.
    """
    path = Path(install_dir) / MANIFEST_NAME
    if not path.is_file():
        return None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning("[itch] unreadable %s: %s", path, e)
        return None
    for action in data.get("actions") or []:
        target = _action_target(action, install_dir, platform)
        if target:
            logger.info("[itch] launch target from %s: %s", MANIFEST_NAME, target)
            return target
    return None


def _action_target(action: Any, install_dir: str, platform: str) -> str | None:
    if not isinstance(action, dict) or str(action.get("name", "")).lower() != "play":
        return None
    if action.get("platform") not in (None, platform):
        return None
    rel = str(action.get("path") or "")
    if not rel or re.match(r"^[a-z]+://", rel):
        return None
    rel = rel.replace(_EXT_TOKEN, _EXT_FOR.get(platform, ""))
    target = (Path(install_dir) / rel).resolve()
    root = Path(install_dir).resolve()
    if root not in target.parents or not target.is_file():
        return None
    return str(target)
