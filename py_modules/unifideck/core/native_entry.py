"""The launch entry point of a GOG Linux-native install.

GOG's Linux installers put a ``start.sh`` at the install root. It is the
entry point GOG ships and the one upstream gogdl (and so Heroic) launches:
it changes into ``game/`` and sets up whatever the build needs before it
runs the real binary. The same install also carries a
``game/goggame-<id>.info`` whose play task names that binary directly, and
launching the binary that way skips the script.

Every caller that asks "is this a native GOG install, and what do we
launch" uses :func:`gog_linux_start_script`, so the resolver, the launcher,
the install-time prefix warmup and cloud saves agree on one rule.

Stdlib-only: the out-of-process launcher imports it under the SYSTEM
python (3.10-3.14).
"""

from __future__ import annotations

from pathlib import Path

START_SCRIPT = "start.sh"


def gog_linux_start_script(install_path: str | Path) -> Path | None:
    """The root ``start.sh`` of a GOG Linux-native install, or None."""
    if not install_path:
        return None
    candidate = Path(install_path) / START_SCRIPT
    return candidate if candidate.is_file() else None
