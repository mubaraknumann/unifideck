"""support_bundle/probe_proton.py — Is the plugin-managed GE-Proton usable?

The GE-Proton the plugin installs is what every game and the Ubisoft login
launch with when no Proton override is set, so one broken copy fails all of
them at once. ``runtime.ge_builds`` lists directory names only, and a
tester's GE-Proton11-7 with no usable ``toolmanifest.vdf`` looked normal
there. This probe runs the launcher's own completeness check against the
build the default tier would pick, so the verdict matches what a launch does.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


def managed_ge_block() -> dict[str, Any]:
    """The cached latest GE tag and, per copy on disk, why it is unusable.

    ``tag`` is None when the plugin has never recorded one. ``dirs`` lists
    every compat root holding a ``<tag>`` directory, with ``problem`` set
    to the completeness check's reason, or None when that copy is usable.
    """
    from unifideck.launcher.proton.infrastructure import ge_installer, ge_marker
    from unifideck.utils import vdf_compat

    tag = ge_marker.read_cached_latest_tag()
    if not tag:
        return {"tag": None, "dirs": []}
    dirs: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for root in vdf_compat.STEAM_COMPAT_ROOTS:
        tool_dir = Path(root).expanduser() / tag
        # The roots are usually symlinks to one directory; report it once.
        if tool_dir.is_dir() and tool_dir.resolve() not in seen:
            seen.add(tool_dir.resolve())
            dirs.append({
                "path": str(tool_dir),
                "problem": ge_installer.proton_install_problem(tool_dir / "proton"),
            })
    return {"tag": tag, "dirs": dirs}
