from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)
PathLike = str | Path

#: Where every per-game prefix outside the wrapper stores lives (launcher
#: invariant 2). Expanded at call time so a test can point ``HOME`` elsewhere.
PREFIXES_DIR = "~/.local/share/unifideck/prefixes"


def prefixes_dir() -> Path:
    """The parent of every per-game prefix, expanded."""
    return Path(PREFIXES_DIR).expanduser()


def game_prefix_root(game_id: str) -> Path:
    """The prefix root of a game that is not a wrapper-store title.

    ``<prefixes>/<game_id>``, with any trailing ``pfx`` component dropped so
    the result is always the root and never umu's ``pfx`` self-link. Builds
    the path only and creates nothing.
    """
    path = prefixes_dir() / game_id
    while path.name == "pfx":
        path = path.parent
    return path


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
