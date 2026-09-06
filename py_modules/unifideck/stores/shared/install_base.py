"""Removable-media install-base detection, shared across stores.

py_modules/unifideck/stores/shared/install_base.py

SteamOS mounts the Deck's internal microSD at ``/run/media/mmcblk0p1`` — a
device node that does **not** exist on desktops, Bazzite, CachyOS or other
handhelds — so a hardcoded path is wrong everywhere but one machine. This
picks the first writable *mounted* directory under ``/run/media`` instead,
handling both layouts seen in the field:

  * SteamOS flat:   ``/run/media/<label>``
  * udisks2 nested: ``/run/media/<user>/<label>``

Moved here from ``stores/ubisoft/config.py`` and generalised — the trailing
store-specific directory is a parameter rather than a hardcoded
``Games/Ubisoft``. **Ubisoft is still the only caller**
(``ubisoft/config.py``); an earlier version of this docstring claimed
Battle.net was a second consumer, which was never true. The generalisation
is worth keeping regardless, but do not read it as evidence of two callers.

This seeds a *default* only. Live install detection re-scans removable
media at scan time, so a stale value is harmless — the path simply will not
exist.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

# The historical Deck path, used only when nothing is mounted.
_FALLBACK_MEDIA_ROOT = Path("/run/media/mmcblk0p1")
_DEFAULT_MEDIA_BASE = Path("/run/media")


def _first_writable_mount(parent: Path) -> Path | None:
    """First writable mountpoint directly under ``parent``, or None."""
    with contextlib.suppress(OSError):
        for sub in sorted(parent.iterdir()):
            if (
                not sub.is_symlink()
                and sub.is_dir()
                and os.path.ismount(sub)
                and os.access(sub, os.W_OK)
            ):
                return sub
    return None


def detect_media_root(media_base: Path | None = None) -> Path | None:
    """The first writable removable-media mountpoint, or None if none."""
    base = _DEFAULT_MEDIA_BASE if media_base is None else media_base
    with contextlib.suppress(OSError):
        for entry in sorted(base.iterdir()):
            if entry.is_symlink() or not entry.is_dir():
                continue
            # Flat layout: /run/media/<label> is itself the mountpoint.
            if os.path.ismount(entry) and os.access(entry, os.W_OK):
                return entry
            nested = _first_writable_mount(entry)
            if nested is not None:
                return nested
    return None


def detect_sdcard_install_base(
    store_dir: str,
    media_base: Path | None = None,
) -> str:
    """Default SD / removable-media install base for one store.

    ``store_dir`` is the vendor folder appended under ``Games`` — e.g.
    ``"Ubisoft"`` or ``"Battlenet"``. ``media_base`` is injectable for
    tests; production uses ``/run/media``.
    """
    root = detect_media_root(media_base) or _FALLBACK_MEDIA_ROOT
    return str(root / "Games" / store_dir)
