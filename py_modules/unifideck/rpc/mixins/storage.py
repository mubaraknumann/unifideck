"""
StorageRPCMixin — install-location enumeration + mutation RPCs.

* ``get_storage_locations``     — list of `(id, label, path, free_space_gb)`
* ``get_browseable_devices``    — device mount-points for the file picker
* ``set_default_storage_location`` — persist user pick
* ``set_custom_install_path``   — persist a custom path

No path is hardcoded: device roots come from ``/proc/mounts``
(real mount points), storage classification uses ``st_dev``
comparison instead of string-prefix heuristics, and ``$HOME``
is resolved at runtime via ``Path.home()``.

Mount enumeration itself is delegated to ``utils/mounts.py``
(shared with ``rpc/mixins/download.py`` and ``utils/paths.py``),
which also handles FUSE-mounted external media (NTFS via ntfs-3g,
some exFAT setups) that are invisible to this backend's root
process without a demoted subprocess — see that module's docstring.

The ``/proc/mounts`` scan and per-device directory creation are
blocking filesystem work, so the async RPCs delegate to the
module-level sync builders (``_build_storage_locations`` /
``_build_browseable_devices``) through a single
``asyncio.to_thread`` hop rather than touching disk on the event
loop.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from unifideck.rpc import RpcError
from unifideck.utils import mount_naming, mounts

logger = logging.getLogger(__name__)

class StorageRPCMixin:
    """Install-location RPC : enumerate + mutate config."""

    config: Any

    async def get_storage_locations(self) -> Any:
        """Return install locations — one entry per physical device.

        Device-level enumeration: reads ``/proc/mounts`` to
        discover unique writable devices (by ``st_dev``), creates
        a ``Games/`` subdirectory on each, and returns one entry
        per device plus an optional custom-path override.

        No per-store subdirectory iteration — the frontend
        ``PickStorageModal`` shows exactly one row per device.
        """
        config = getattr(self, "config", None)
        custom_path = _read_config_str(config, "download.custom_path")
        default = (
            _read_config_str(config, "download.default_location", "internal")
            or "internal"
        )
        locations = await asyncio.to_thread(_build_storage_locations, custom_path)
        default = _remap_legacy_default(default, locations)
        return {"locations": locations, "default": default}

    async def get_browseable_devices(self) -> Any:
        """Return mount-points of every writable storage device.

        Reads ``/proc/mounts`` to discover real mount points with
        no hardcoded paths.  Internal = ``$HOME``; external = every
        other writable, non-system mount that sits on a different
        device.
        """
        devices = await asyncio.to_thread(_build_browseable_devices)
        return {"devices": devices}

    async def set_default_storage_location(self, loc_id: str) -> Any:
        """Persist the user's preferred default storage location."""
        if loc_id not in ("internal", "sdcard", "custom") and not loc_id.startswith("ext:"):
            raise RpcError("invalid_location", loc_id=loc_id)
        config = getattr(self, "config", None)
        if config is None:
            raise RpcError("service_unavailable", service="config")
        config.set("download.default_location", loc_id)
        return {"success": True, "default": loc_id}

    async def set_custom_install_path(self, path: str) -> Any:
        """Persist a user-picked custom install root.

        Validates that the path exists and is writable before
        saving so the download service can rely on it.
        """
        config = getattr(self, "config", None)
        if config is None:
            raise RpcError("service_unavailable", service="config")
        resolved = await asyncio.to_thread(
            lambda: str(Path(path or "").expanduser().resolve()),
        )
        is_dir = await asyncio.to_thread(Path(resolved).is_dir)
        if not resolved or not is_dir:
            return {
                "success": False,
                "error": "path_not_a_directory",
                "path": resolved,
            }
        writable = await asyncio.to_thread(os.access, resolved, os.W_OK)
        if not writable:
            return {
                "success": False,
                "error": "path_not_writable",
                "path": resolved,
            }
        config.set("download.custom_path", resolved)
        return {"success": True, "path": resolved}

# ─── Storage builders (blocking — run via asyncio.to_thread) ──────

def _build_storage_locations(custom_path: str | None) -> list[dict[str, Any]]:
    """Enumerate one location per writable device + optional custom path.

    Internal storage (``~/Games``) is always first; each distinct
    external device contributes one ``Games/`` entry; a configured
    custom path is appended last.
    """
    home_dev = mounts.stat_dev(os.path.expanduser("~"))
    games_root = str(Path("~/Games").expanduser())
    _ensure_dir(games_root)
    locations: list[dict[str, Any]] = [
        _location_entry("internal", "Internal storage", games_root, games_root),
    ]
    externals = mounts.dedupe_by_device(
        mounts.scan_mounts(home_dev, require_writable=True),
    )
    for loc_id, m in mounts.assign_unique_ids(externals):
        label = _external_label(m)
        games_path = mounts.ensure_games_subdir(
            m.mount_point, m.effective_uid, m.effective_gid,
        )
        locations.append(_location_entry(loc_id, label, games_path, m.mount_point))
    if custom_path:
        locations.append(
            _location_entry("custom", custom_path, custom_path, custom_path),
        )
    return locations

def _build_browseable_devices() -> list[dict[str, Any]]:
    """List every writable device's mount point for the file picker."""
    home = str(Path.home())
    devices: list[dict[str, Any]] = [
        {
            "id": "internal",
            "label": "Internal Storage",
            "path": home,
            "free_space_gb": _free_gb(home),
        },
    ]
    externals = mounts.dedupe_by_device(
        mounts.scan_mounts(mounts.stat_dev(home), require_writable=True),
    )
    for loc_id, m in mounts.assign_unique_ids(externals):
        devices.append({
            "id": loc_id,
            "label": _external_label(m),
            "path": m.mount_point,
            "free_space_gb": _free_gb(m.mount_point),
        })
    return devices

def _external_label(m: mounts.MountInfo) -> str:
    """SD-card-looking source device → "SD Card"; else the mount's name."""
    if mounts.is_sdcard_source(m.device):
        return "SD Card"
    return f"External Drive ({mount_naming.display_name(m.mount_point)})"

def _remap_name_derived_id(
    default: str, locations: list[dict[str, Any]],
) -> str | None:
    """Match an ``ext:<name>`` id by re-deriving it from ``device_path``."""
    for loc in locations:
        device_path = loc.get("device_path")
        if not isinstance(device_path, str) or not device_path:
            continue
        if mount_naming.legacy_mount_id(device_path) == default:
            return str(loc["id"])
    return None

def _first_external_id(locations: list[dict[str, Any]]) -> str | None:
    """First external location, standing in for the old shared ``sdcard`` id."""
    for loc in locations:
        loc_id = loc["id"]
        if isinstance(loc_id, str) and loc_id.startswith("ext:"):
            return loc_id
    return None

def _remap_legacy_default(default: str, locations: list[dict[str, Any]]) -> str:
    """Remap a persisted default saved under an older id scheme.

    Two generations of stale values can be sitting in a user's config,
    and either would match nothing in *locations* and silently
    pre-select no row in the picker:

    * ``"sdcard"`` — from when every external mount shared one
      hardcoded id.
    * ``"ext:<name>"`` — derived from the mount-point basename, before
      ids moved to the filesystem UUID. Recognised by re-deriving the
      old id from each location's ``device_path``, so the user's chosen
      drive stays chosen across the upgrade.
    """
    ids = {loc["id"] for loc in locations}
    if default in ids:
        return default
    remapped: str | None = None
    if default.startswith("ext:"):
        remapped = _remap_name_derived_id(default, locations)
    elif default == "sdcard":
        remapped = _first_external_id(locations)
    return remapped or "internal"

# ─── Module-level helpers ─────────────────────────────────────

def _location_entry(
    loc_id: str, label: str, path: str, free_basis: str,
) -> dict[str, Any]:
    """Build a storage-location dict; free space is measured on *free_basis*.

    ``device_path`` is the root this location lives on (the mount point
    for an external, the directory itself otherwise). It is what lets
    ``_remap_legacy_default`` recognise an id saved under the old
    name-derived scheme without a second mount scan.
    """
    return {
        "id": loc_id,
        "label": label,
        "path": path,
        "device_path": free_basis,
        "available": True,
        "free_space_gb": _free_gb(free_basis),
    }

def _read_config_str(
    config: Any, key: str, default: str | None = None,
) -> str | None:
    """Read a string config value defensively; *default* on any failure."""
    if config is None:
        return default
    try:
        value = config.get(key, default)
    except Exception as e:
        logger.debug("[storage] reading %s failed: %s", key, e)
        return default
    return value if isinstance(value, str) else default

def _free_gb(path: str) -> float:
    """Free space in GB for the filesystem containing *path*."""
    try:
        st = os.statvfs(path)
        return round((st.f_frsize * st.f_bavail) / (1024 ** 3), 1)
    except OSError:
        return 0.0

def _ensure_dir(path: str) -> None:
    """Create a directory if it doesn't exist. Idempotent."""
    Path(path).mkdir(parents=True, exist_ok=True)
