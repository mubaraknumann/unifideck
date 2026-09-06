from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)
PathLike = str | Path
def normalize_prefix_root(prefix_path: PathLike) -> Path:
    """Normalize prefix root."""
    p = Path(prefix_path).resolve() if isinstance(prefix_path, str) \
        else prefix_path.resolve()
    while p.name == "pfx":
        p = p.parent
    return p
def resolve_registry_prefix(prefix_root: PathLike) -> Path:
    """Resolve registry prefix."""
    root = Path(prefix_root) if isinstance(prefix_root, str) \
        else prefix_root
    direct = root / "user.reg"
    pfx = root / "pfx"
    pfx_reg = pfx / "user.reg"
    if direct.exists():
        return root
    if pfx_reg.exists():
        return pfx
    if pfx.is_dir():
        return pfx
    return root
def resolve_drive_c(prefix_root: PathLike) -> Path | None:
    """Resolve a prefix's ``drive_c`` across both layouts, or ``None``.

    umu creates ``pfx -> .`` as a self-symlink, so ``<prefix>/drive_c`` and
    ``<prefix>/pfx/drive_c`` are the same directory — and both spellings
    occur in the wild. The naive combine is what made Ubisoft's recovery path
    fail to find a ``upc.exe`` that was genuinely present.

    (That explanation came from a copy of this function in
    ``launcher/wrapper_session_specs.py``; the copy is gone, the reasoning
    is not. Audit register item 47.)
    """
    root = Path(prefix_root) if isinstance(prefix_root, str) \
        else prefix_root
    modern = root / "pfx" / "drive_c"
    if modern.is_dir():
        return modern
    legacy = root / "drive_c"
    if legacy.is_dir():
        return legacy
    return None
