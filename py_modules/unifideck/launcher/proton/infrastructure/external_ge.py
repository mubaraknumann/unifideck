"""infrastructure/external_ge.py — detection and management of external GE-Proton.

Handles third-party GE-Proton tools (such as "Proton-GE Latest" managed
by ProtonPlus, ProtonUp-Qt, or distro packages). These tools update
in-place, frequently modify ``compatibilitytool.vdf`` manifests, and may
break directory naming conventions.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from unifideck.launcher.proton.infrastructure import ge_installer
from unifideck.utils import vdf_compat

logger = logging.getLogger(__name__)

EXTERNAL_GE_ALIASES: tuple[str, ...] = (
    "Proton-GE Latest",
    "Proton-GE-Latest",
    "GE-Proton-Latest",
)

EXTERNAL_COMPAT_ROOTS: tuple[str, ...] = (
    *ge_installer._SCAN_ROOTS,
    *vdf_compat.SYSTEM_COMPAT_DIRS,
)


def get_external_compat_roots() -> list[Path]:
    """Return all roots searched for externally managed compatibility tools."""
    roots: list[Path] = []
    for r in EXTERNAL_COMPAT_ROOTS:
        p = Path(r).expanduser()
        if p not in roots:
            roots.append(p)
    return roots


def parse_ge_version(tag: str) -> tuple[int, int] | None:
    """Parse major and minor version numbers from a GE-Proton tag (e.g. GE-Proton11-5 -> (11, 5))."""
    if not tag:
        return None
    m = re.search(r"GE-Proton(\d+)[-_](\d+)", tag, re.IGNORECASE)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


def is_ge_outdated(current_version: str, latest_version: str) -> bool:
    """True iff current_version is older than latest_version."""
    curr = parse_ge_version(current_version)
    latest = parse_ge_version(latest_version)
    if curr and latest:
        return curr < latest
    return False


EXTERNAL_GE_MAX_MINOR_LAG = 5


def is_ge_sufficiently_fresh(
    current_version: str,
    latest_version: str,
    max_minor_lag: int = EXTERNAL_GE_MAX_MINOR_LAG,
) -> bool:
    """True iff current_version is considered sufficiently fresh compared to latest_version.

    Returns True if:
    - current_version is newer than or equal to latest_version; OR
    - current_version and latest_version share the same major version and
      latest_version's minor version is at most `max_minor_lag` ahead.
    """
    curr = parse_ge_version(current_version)
    latest = parse_ge_version(latest_version)
    if not (curr and latest):
        return False
    major_diff = latest[0] - curr[0]
    minor_diff = latest[1] - curr[1]
    if major_diff < 0:
        return True
    if major_diff == 0 and minor_diff <= max_minor_lag:
        return True
    return False


def read_tool_internal_version(tool_dir: Path) -> str:
    """Read the actual version string from a Proton tool's version file."""
    for candidate_dir in (tool_dir, tool_dir.parent):
        version_file = candidate_dir / "version"
        if version_file.is_file():
            try:
                content = version_file.read_text(encoding="utf-8", errors="ignore").strip()
                if content:
                    return content.split()[-1]
            except OSError:
                pass
    if parse_ge_version(tool_dir.name):
        return tool_dir.name
    return ""


def find_external_ge_proton(
    roots: list[Path] | tuple[Path, ...] | None = None,
) -> tuple[Path, str, str] | None:
    """Find an externally managed GE-Proton installation (e.g. via ProtonPlus).

    Builds on ``vdf_compat.iter_compat_tools`` to accurately discover tools
    whose display names or internal keys match ``EXTERNAL_GE_ALIASES`` regardless
    of directory name.

    Returns (proton_script_path, tool_id, real_version_tag) or None.
    """
    if roots is None:
        scan_roots = get_external_compat_roots()
    else:
        scan_roots = list(roots)

    tools = vdf_compat.iter_compat_tools(scan_roots)
    if not tools:
        return None

    for alias in EXTERNAL_GE_ALIASES:
        proton = tools.get(alias)
        matched_name = alias
        if proton is None:
            lowered = alias.lower()
            for name, path in tools.items():
                if name.lower() == lowered:
                    proton = path
                    matched_name = name
                    break
        if proton and ge_installer.is_proton_install_complete(proton):
            real_version = read_tool_internal_version(proton.parent)
            return proton, matched_name, real_version
    return None
