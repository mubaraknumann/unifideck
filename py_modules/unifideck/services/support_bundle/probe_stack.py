"""support_bundle/probe_stack.py — The software stack we run on.

Steam, Decky, our own install, the Proton/umu runtime, and the caches
that decide whether a sync does any work.

Two fields here exist because of specific past investigations:

* ``compat_tool_mapping`` includes the **appid-0 global default**, not
  just our own AppIDs. A distro shipping a global default Proton that
  leaked onto our shortcuts was its own bug, and it is invisible if you
  only look at per-game entries.
* every Proton install gets a ``complete`` verdict, because an install
  that is structurally present but internally incomplete hangs the
  first-run setup in a way that looks like our code failing.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from . import procscan

logger = logging.getLogger(__name__)

# Env vars Decky documents for plugins. Recorded as present/absent so
# a missing one explains which log-directory fallback rung had to win.
_DECKY_VARS = (
    "DECKY_VERSION", "DECKY_USER", "DECKY_USER_HOME", "DECKY_HOME",
    "DECKY_PLUGIN_SETTINGS_DIR", "DECKY_PLUGIN_RUNTIME_DIR",
    "DECKY_PLUGIN_LOG_DIR", "DECKY_PLUGIN_LOG", "DECKY_PLUGIN_DIR",
    "DECKY_PLUGIN_NAME", "DECKY_PLUGIN_VERSION", "DECKY_PLUGIN_AUTHOR",
)
# Non-secret environment we are willing to ship. Never dict(os.environ):
# a dev machine holds GITHUB_TOKEN and friends.
_ENV_ALLOWLIST = (
    "HOME", "USER", "LOGNAME", "SHELL", "LANG", "PATH",
    "XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP", "XDG_DATA_HOME",
    "XDG_CONFIG_HOME", "SteamDeck", "SteamGamepadUI", "SteamOS",
    # Whether a bundled CLI can start at all now depends on these. legendary
    # and gogdl ship as zipapps run by the system python3, so PYTHONHOME /
    # PYTHONPATH decide which interpreter and which packages they get, and
    # XDG_CACHE_HOME decides where they extract their native modules. The
    # loader pair is the long-running "umu exits 127 / curl picks the wrong
    # libssl" culprit. All are non-secret paths, and without them a bundle
    # from a CLI that refused to launch shows nothing about why.
    "XDG_CACHE_HOME", "PYTHONPATH", "PYTHONHOME",
    "LD_LIBRARY_PATH", "LD_PRELOAD",
)
# Presence only: a proxy can break store sync, but its URL may embed
# credentials, so the value never leaves the device.
_PROXY_VARS = ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "no_proxy")
_BUNDLED_BINARIES = ("legendary", "gogdl", "nile", "comet", "umu-run", "winetricks")
_OFFICIAL_PROTON_APPIDS = {
    "1493710": "Proton Experimental",
    "2805730": "Proton 9.0",
    "2348590": "Proton 8.0",
    "1887720": "Proton 7.0",
    "1580130": "Proton 6.3",
    "2180100": "Proton Hotfix",
}
# A Proton directory missing any of these is present but unusable.
_PROTON_REQUIRED = ("proton", "toolmanifest.vdf")
_UMU_ENTRY_POINTS = ("umu", "_v2-entry-point")


def _read_json(path: Path) -> dict[str, Any] | None:
    """Parse a small JSON file, returning None on any failure."""
    try:
        parsed = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _mode_of(path: Path) -> dict[str, Any]:
    """Size, mode and executability of one file."""
    try:
        info = path.stat()
    except OSError:
        return {"present": False}
    return {
        "present": True,
        "size": info.st_size,
        "mode": oct(info.st_mode & 0o7777),
        "executable": os.access(path, os.X_OK),
    }


def identity_block() -> dict[str, Any]:
    """Who this process runs as, and its non-secret environment.

    The uid fields are the running proof that the backend is demoted to
    the desktop user rather than running as root, which a stale comment
    elsewhere in the tree still claims.
    """
    env = {name: os.environ.get(name, "") for name in _ENV_ALLOWLIST}
    proxies = {name: bool(os.environ.get(name)) for name in _PROXY_VARS}
    ids = procscan.own_ids()
    return {
        "ids": ids,
        "running_as_root": ids["euid"] == 0,
        "home": str(Path.home()),
        "cwd": os.getcwd(),
        "env_allowlisted": env,
        "proxy_vars_set": proxies,
    }


def decky_block() -> dict[str, Any]:
    """Decky's environment contract, including what was missing."""
    present = {name: os.environ[name] for name in _DECKY_VARS if name in os.environ}
    absent = [name for name in _DECKY_VARS if name not in os.environ]
    return {
        "version": os.environ.get("DECKY_VERSION", "unknown"),
        "present": present,
        "absent": absent,
        "plugins_dir": str(Path.home() / "homebrew" / "plugins"),
    }


def plugin_block(plugin_dir: str | None) -> dict[str, Any]:
    """Our own install: version, build flavour, bundled binaries."""
    if not plugin_dir:
        return {"resolved": False}
    base = Path(plugin_dir)
    package = _read_json(base / "package.json") or {}
    binaries = {
        name: _mode_of(base / "bin" / name) for name in _BUNDLED_BINARIES
    }
    binaries["unifideck-launcher"] = _mode_of(base / "bin" / "unifideck-launcher")
    # butler ships as a directory with the 7-zip libraries it dlopen()s.
    binaries["butler"] = _mode_of(base / "bin" / "butler" / "butler")
    binaries["butler-7z.so"] = _mode_of(base / "bin" / "butler" / "7z.so")
    return {
        "resolved": True,
        "path": plugin_dir,
        "version": str(package.get("version", "unknown")),
        "dev_build": (base / "dev_build.json").is_file(),
        "defaults_config": (base / "defaults" / "config.json").is_file(),
        "flattened_config": (base / "config.json").is_file(),
        "binaries": binaries,
        "umu_version": _bundled_umu_version(base),
    }


def _bundled_umu_version(base: Path) -> str:
    """Version of the bundled umu zipapp, or ``"unknown"``.

    umu is committed to the repo rather than downloaded, so it has no entry
    in package.json's ``remote_binary`` manifest and nothing else in a
    bundle records which one shipped. Since umu chooses the Steam Linux
    Runtime and how it is fetched (the <=1.4.1 URL is permanently 403'd),
    that is the first thing worth knowing about any launch failure.
    """
    try:
        return (base / "bin" / "umu" / "VERSION").read_text(
            encoding="utf-8",
        ).strip() or "unknown"
    except OSError:
        return "unknown"


def steam_block(steam_root: str | None, root_source: str) -> dict[str, Any]:
    """Steam layout, branch, active user, and compat-tool mapping."""
    processes = procscan.iter_processes()
    block: dict[str, Any] = {
        "root": steam_root or "",
        "root_source": root_source,
        "flatpak_steam": (
            Path.home() / ".var" / "app" / "com.valvesoftware.Steam"
        ).is_dir(),
        "steam_running": any(p.name == "steam" for p in processes),
        "steam_started_at": procscan.newest_start(processes, "steam"),
    }
    if not steam_root:
        return block
    root = Path(steam_root)
    block["layout"] = _steam_layout(root)
    block["branch"] = _steam_branch(root)
    block["compat_tool_mapping"] = _compat_mapping(root)
    block["active_user"] = _active_user(root)
    return block


def _steam_layout(root: Path) -> str:
    """Describe which install layout this root represents."""
    name = str(root)
    if ".var/app/com.valvesoftware.Steam" in name:
        return "flatpak"
    if name.endswith(".steam/steam"):
        return "dot_steam"
    return "local_share_steam"


def _steam_branch(root: Path) -> str:
    """Read the opted-in client branch from ``steam.cfg``."""
    for candidate in (root / "steam.cfg", root.parent / "steam.cfg"):
        if not candidate.is_file():
            continue
        for line in _safe_text(candidate).splitlines():
            if line.lower().startswith("betaname"):
                return line.partition("=")[2].strip() or "unknown"
    return "stable_or_unset"


def _safe_text(path: Path, limit: int = 262144) -> str:
    """Read a text file, returning "" on any failure."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return handle.read(limit)
    except OSError:
        return ""


def _compat_mapping(root: Path) -> dict[str, Any]:
    """Extract CompatToolMapping, including the appid-0 default.

    Only the mapping is extracted, never the surrounding document:
    ``config.vdf`` also holds account data. The appid-0 entry is kept
    deliberately — a distro-wide default Proton leaking onto our
    shortcuts is invisible if you only look at per-game rows.

    A parse failure is reported rather than raised: this block is
    diagnostic, and the rest of the report is still worth having.
    """
    config = root / "config" / "config.vdf"
    if not config.is_file():
        return {"available": False}
    try:
        import vdf

        # The vendored vdf library ships no type stubs.
        parsed = vdf.loads(_safe_text(config))  # type: ignore[no-untyped-call]
    except Exception as err:
        logger.debug("[support_bundle] config.vdf parse failed: %s", err)
        return {"available": False, "error": repr(err)}
    node = parsed
    for key in ("InstallConfigStore", "Software", "Valve", "Steam"):
        node = node.get(key, {}) if isinstance(node, dict) else {}
    mapping = node.get("CompatToolMapping", {}) if isinstance(node, dict) else {}
    return {
        "available": True,
        "global_default": mapping.get("0", {}),
        "entry_count": len(mapping) if isinstance(mapping, dict) else 0,
        "non_steam_entries": _non_steam_entries(mapping),
    }


def _non_steam_entries(mapping: Any) -> dict[str, Any]:
    """Keep only the mapping rows for non-Steam shortcut AppIDs."""
    if not isinstance(mapping, dict):
        return {}
    return {
        appid: entry for appid, entry in mapping.items()
        if appid.isdigit() and int(appid) > 2000000000
    }


def _active_user(root: Path) -> dict[str, Any]:
    """Resolve the logged-in Steam user id."""
    try:
        from unifideck.steam.current_user import resolve

        resolved = resolve(root)
    except Exception as err:
        return {"resolved": None, "error": repr(err)}
    userdata = root / "userdata"
    return {
        "resolved": resolved,
        "userdata_dirs": _child_names(userdata),
    }


def _child_names(root: Path) -> list[str]:
    """Immediate child names of a directory, tolerating errors."""
    try:
        return sorted(child.name for child in root.iterdir())
    except OSError:
        return []


def runtime_block(steam_root: str | None, data_dir: str | None) -> dict[str, Any]:
    """Proton and umu inventory, with completeness verdicts."""
    return {
        "umu_run": _umu_version(),
        "umu_variants": _umu_variants(),
        "protons": _protons(steam_root),
        "ge_builds": _ge_builds(steam_root, data_dir),
        "xdg_user_dir": shutil.which("xdg-user-dir") or "",
        "flatpak": shutil.which("flatpak") or "",
    }


def _umu_version() -> str:
    """Version string of whichever umu-run is on PATH."""
    binary = shutil.which("umu-run")
    if binary is None:
        return "not on PATH"
    try:
        done = subprocess.run(
            [binary, "--version"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError) as err:
        return f"probe failed: {err}"
    return (done.stdout or done.stderr).strip()[:200] or "unknown"


def _umu_variants() -> list[dict[str, Any]]:
    """Per-variant umu runtime state, including the entry point.

    A variant whose entry-point symlink is missing logs "up to date"
    and then fails with a file-not-found, exiting with a code outside
    the recoverable set, so the self-heal path never fires. That makes
    this symlink worth naming explicitly.
    """
    base = Path.home() / ".local" / "share" / "umu"
    if not base.is_dir():
        return []
    found: list[dict[str, Any]] = []
    for entry in sorted(base.iterdir()):
        if not entry.is_dir() or not entry.name.startswith("steamrt"):
            continue
        entries = {
            name: (entry / name).exists() for name in _UMU_ENTRY_POINTS
        }
        found.append({
            "variant": entry.name,
            "entry_points": entries,
            "complete": any(entries.values()),
        })
    return found


def _protons(steam_root: str | None) -> list[dict[str, Any]]:
    """Official Proton installs with a completeness verdict."""
    if not steam_root:
        return []
    common = Path(steam_root) / "steamapps" / "common"
    if not common.is_dir():
        return []
    found: list[dict[str, Any]] = []
    for entry in sorted(common.iterdir()):
        if not entry.is_dir() or not entry.name.startswith("Proton"):
            continue
        found.append({
            "name": entry.name,
            "complete": all((entry / f).exists() for f in _PROTON_REQUIRED),
            "manifests": _proton_manifests(Path(steam_root), entry.name),
        })
    return found


def _proton_manifests(steam_root: Path, name: str) -> dict[str, Any]:
    """State of the Steam app manifest backing a Proton install."""
    for appid, label in _OFFICIAL_PROTON_APPIDS.items():
        if label.lower() not in name.lower():
            continue
        manifest = steam_root / "steamapps" / f"appmanifest_{appid}.acf"
        return {"appid": appid, "manifest_present": manifest.is_file()}
    return {}


def _ge_builds(steam_root: str | None, data_dir: str | None) -> list[str]:
    """Names of GE builds visible to Steam and to the plugin."""
    roots: list[Path] = []
    if steam_root:
        roots.append(Path(steam_root) / "compatibilitytools.d")
    if data_dir:
        roots.append(Path(data_dir) / "compat-tools")
    names: list[str] = []
    for root in roots:
        if root.is_dir():
            names.extend(f"{root.name}/{child}" for child in _child_names(root))
    return names


def caches_block(data_dir: str | None, runtime_dir: str | None) -> dict[str, Any]:
    """Cache files with their age.

    Sync uses long TTLs and a rate-limit gate, so a cache that is stale
    but unexpired is indistinguishable from a broken sync unless the
    age is written down.
    """
    roots = [
        Path(runtime_dir) / "cache" if runtime_dir else None,
        Path(data_dir) / "cache" if data_dir else None,
        Path.home() / ".cache" / "unifideck",
    ]
    entries: list[dict[str, Any]] = []
    for root in roots:
        if root is None or not root.is_dir():
            continue
        entries.extend(_cache_entries(root))
    return {"entries": entries}


def _cache_entries(root: Path) -> list[dict[str, Any]]:
    """Age and size for every cache file under ``root``."""
    now = time.time()
    found: list[dict[str, Any]] = []
    try:
        children = sorted(root.iterdir())
    except OSError:
        return found
    for child in children:
        if not child.is_file():
            continue
        try:
            info = child.stat()
        except OSError:
            continue
        found.append({
            "path": str(child),
            "size": info.st_size,
            "age_hours": round((now - info.st_mtime) / 3600, 2),
        })
    return found


def artwork_block(grid_dir: str | None) -> dict[str, Any]:
    """Artwork coverage for our shortcuts.

    Counts by suffix rather than by app: Steam encodes the artwork kind
    in the filename suffix, so the distribution alone shows whether a
    particular kind failed to download across the board.
    """
    if not grid_dir:
        return {"resolved": False}
    root = Path(grid_dir)
    if not root.is_dir():
        return {"resolved": True, "exists": False}
    kinds: dict[str, int] = {}
    total = 0
    size = 0
    try:
        children = list(root.iterdir())
    except OSError as err:
        return {"resolved": True, "exists": True, "error": repr(err)}
    for child in children:
        if not child.is_file():
            continue
        total += 1
        size += _size_or_zero(child)
        kinds[_artwork_kind(child.stem)] = kinds.get(_artwork_kind(child.stem), 0) + 1
    return {
        "resolved": True, "exists": True, "path": grid_dir,
        "files": total, "bytes": size, "by_kind": kinds,
    }


def _size_or_zero(path: Path) -> int:
    """File size, or 0 when it cannot be read."""
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _artwork_kind(stem: str) -> str:
    """Classify a grid filename by its Steam artwork suffix."""
    for suffix, kind in (
        ("_hero", "hero"), ("_logo", "logo"), ("p", "portrait"),
        ("_icon", "icon"),
    ):
        if stem.endswith(suffix):
            return kind
    return "header"


def playtime_block(db_path: str | None) -> dict[str, Any]:
    """Row counts read from the live playtime database.

    The archive ships the ``.db`` file but not its write-ahead log, so
    that copy can lag behind reality. This block opens the live
    database read-only (``mode=ro``, which does honour the WAL) purely
    to count rows, so an empty table in the shipped snapshot can be
    told apart from genuinely absent playtime data.

    Read-only by construction: the URI forbids writes, so this can
    never create or upgrade a schema as a side effect.
    """
    if not db_path or not Path(db_path).is_file():
        return {"present": False}
    import sqlite3

    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'",
            ).fetchall()
            counts = {
                str(row[0]): _row_count(conn, str(row[0])) for row in tables
            }
    except sqlite3.Error as err:
        return {"present": True, "error": repr(err)}
    return {"present": True, "tables": counts}


def _row_count(conn: Any, table: str) -> int | None:
    """Count rows in one table, tolerating an odd schema."""
    if not table.replace("_", "").isalnum():
        return None
    try:
        cursor = conn.execute(f"SELECT COUNT(*) FROM {table}")  # noqa: S608
        return int(cursor.fetchone()[0])
    except Exception:
        return None
