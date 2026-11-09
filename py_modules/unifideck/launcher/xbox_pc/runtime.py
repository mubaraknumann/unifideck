"""launcher/xbox_pc/runtime.py — the installed Xodus runtime and backend.

``active.json`` names one runtime folder and one backend folder
(``layout.read_active``). This module checks that the runtime folder can
actually run a game and reads its feature list, and resolves the verified
backend binaries. It never looks at ``PATH``.

The runtime folder is the unpacked release: ``proton``, ``files/bin/wine``,
``files/share/default_pfx`` and the metadata file named by
``layout.RUNTIME_META_NAME``.

Stdlib only, Python 3.10 safe.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .backend import BackendPaths, check_backend
from .errors import XboxPcLaunchError
from .layout import RUNTIME_META_NAME, ActiveComponents

#: Runtime features that make cloud saves and in-game Xbox sign-in work.
CLOUD_SAVES_FEATURE = "cloud-saves-pr28"
BRIDGE_FEATURE = "xodus-tcp-bridge-v1"

_REQUIRED = ("proton", "files/bin/wine", "files/bin/wineserver", "files/share/default_pfx")


@dataclass
class Runtime:
    """One unpacked runtime release and what its metadata declares."""

    root: Path
    features: list[str] = field(default_factory=list)
    components: dict[str, str] = field(default_factory=dict)

    @property
    def files(self) -> Path:
        """The Wine tree."""
        return self.root / "files"

    @property
    def wine(self) -> Path:
        """The runtime's own ``wine``."""
        return self.files / "bin" / "wine"

    @property
    def wineserver(self) -> Path:
        """The runtime's own ``wineserver``."""
        return self.files / "bin" / "wineserver"

    @property
    def cloud_saves(self) -> bool:
        """Whether the runtime can sync saves through ``xodus-service``."""
        return CLOUD_SAVES_FEATURE in self.features and BRIDGE_FEATURE in self.features

    @property
    def unbridged_cloud_saves(self) -> bool:
        """Cloud saves without the bridge: an old runtime that cannot reach the service."""
        return CLOUD_SAVES_FEATURE in self.features and BRIDGE_FEATURE not in self.features


def _read_meta(path: Path) -> tuple[list[str], dict[str, str]]:
    """``(features, components)`` from the metadata file. Raises ``runtime_missing``."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        raise XboxPcLaunchError("runtime_missing", f"unreadable {path.name}: {err}") from err
    if not isinstance(data, dict):
        raise XboxPcLaunchError("runtime_missing", f"{path.name} is not an object")
    features = [f for f in data.get("features") or [] if isinstance(f, str)]
    raw = data.get("components")
    components = (
        {str(k): v for k, v in raw.items() if isinstance(v, str)}
        if isinstance(raw, dict) else {}
    )
    return features, components


def load_runtime(root: Path) -> Runtime:
    """Check the runtime at ``root`` and read its features.

    Raises ``runtime_missing`` when a required file is gone or ``wine`` is
    not executable. A runtime with no metadata file still loads, with no
    features, so the caller can report the missing cloud saves by name.
    """
    root = root.resolve()
    for rel in _REQUIRED:
        if not (root / rel).exists():
            raise XboxPcLaunchError("runtime_missing", f"{root.name} has no {rel}")
    runtime = Runtime(root)
    for binary in (runtime.wine, runtime.wineserver):
        if not os.access(binary, os.X_OK):
            raise XboxPcLaunchError("runtime_missing", f"{binary.name} is not executable")
    meta = root / RUNTIME_META_NAME
    if meta.is_file():
        runtime.features, runtime.components = _read_meta(meta)
    return runtime


def load_backend(active: ActiveComponents) -> BackendPaths:
    """The verified ``xodus-cli`` and ``xodus-service`` of ``active``.

    Raises ``backend_missing`` or ``backend_tampered``. Blocking (it hashes
    the binaries the first time): call it through ``asyncio.to_thread``.
    """
    paths, problem = check_backend(active)
    if paths is None:
        raise XboxPcLaunchError(problem or "backend_missing", str(active.backend_dir.name))
    return paths
