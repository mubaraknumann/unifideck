"""
Registry of installed Ubisoft games — tracks active installs across reboots.

``UbisoftInstallerRegistry`` maintains a persistent JSON registry of
every Ubisoft game installed via Unifideck. Each entry records:

* the UPC ``space_id`` + Unifideck ``install_id`` (cross-ref via id_map);
* the install path inside the Wine prefix;
* the prefix path (for multi-prefix installs);
* the install timestamp + last-known launch timestamp.

The registry is read on store boot to rebuild the library state, and
written after every successful install/uninstall. It complements (does
not replace) the UPC-side game catalog: the registry is the authoritative
source for "which games does Unifideck know about", while UPC's catalog
is authoritative for "which games are licensed".
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from unifideck.stores.ubisoft.config import UbisoftConfig

logger = logging.getLogger(__name__)
_INSTALLS_REG_SECTION_FMT = (
    "[Software\\\\WOW6432Node\\\\Ubisoft\\\\Launcher\\\\Installs\\\\{install_id}]"
)
_STEAM_COMPAT_ENV_VARS = (
    "SteamAppId",
    "SteamGameId",
    "STEAM_COMPAT_APP_ID",
)

def resolve_active_prefix_dir(
    prefix_path: str,
) -> str | None:
    """Resolve active prefix dir."""
    prefix = Path(prefix_path)
    pfx = prefix / "pfx"
    if (pfx / "system.reg").is_file():
        return str(pfx)
    if (prefix / "system.reg").is_file():
        return str(prefix)
    return None

def read_system_reg(
    active_prefix: str,
) -> tuple[str, str] | None:
    """Read system reg."""
    system_reg = str(Path(active_prefix) / "system.reg")
    try:
        content = Path(system_reg).read_text(
            encoding="utf-8",
            errors="replace",
        )
    except OSError:
        return None
    return system_reg, content

def find_install_registry_section_bounds(
    content: str,
    section: str,
) -> tuple[int, int] | None:
    """Find install registry section bounds."""
    if section not in content:
        return None
    sec_start = content.index(section)
    tail = content[sec_start + len(section) :]
    next_sec = re.search(r"\n\[", tail)
    sec_end = sec_start + len(section) + next_sec.start() if next_sec else len(content)
    return sec_start, sec_end

def _update_or_append_install_section(
    content: str,
    section: str,
    values: list[str],
) -> str:
    """Update or append install section."""
    bounds = find_install_registry_section_bounds(
        content,
        section,
    )
    if bounds is None:
        return content + f"\n{section}\n" + "\n".join(values) + "\n"
    sec_start, sec_end = bounds
    sec_body = content[sec_start + len(section) : sec_end]
    for val in values:
        key = val.split("=")[0]
        pattern = rf'^{re.escape(key)}="[^"]*"'
        new_body, count = re.subn(
            pattern,
            val,
            sec_body,
            flags=re.MULTILINE,
        )
        sec_body = new_body if count else sec_body.rstrip("\n") + "\n" + val + "\n"
    return content[: sec_start + len(section)] + sec_body + content[sec_end:]

def inject_install_registry(
    prefix_path: str,
    install_id: str,
    install_dir: str,
) -> None:
    """Inject install registry."""
    try:
        active_prefix = resolve_active_prefix_dir(prefix_path)
        if active_prefix is None:
            return
        loaded = read_system_reg(active_prefix)
        if loaded is None:
            return
        system_reg, content = loaded
        wine_path = install_dir
        if install_dir.startswith("/"):
            wine_path = "Z:" + install_dir.replace("/", "\\\\")
        section = _INSTALLS_REG_SECTION_FMT.format(
            install_id=install_id,
        )
        values = [f'"InstallDir"="{wine_path}"']
        content = _update_or_append_install_section(
            content,
            section,
            values,
        )
        Path(system_reg).write_text(
            content,
            encoding="utf-8",
        )
        logger.info(
            "[UbisoftInstaller] install registry injected for %s",
            install_id,
        )
    except Exception as e:
        logger.warning(
            "[UbisoftInstaller] registry injection failed: %s",
            e,
        )

def clean_install_registry(
    prefix_path: str,
    install_id: str,
) -> None:
    """Clean install registry."""
    if not install_id:
        return
    try:
        active_prefix = resolve_active_prefix_dir(prefix_path)
        if active_prefix is None:
            return
        loaded = read_system_reg(active_prefix)
        if loaded is None:
            return
        system_reg, content = loaded
        section = _INSTALLS_REG_SECTION_FMT.format(
            install_id=install_id,
        )
        bounds = find_install_registry_section_bounds(
            content,
            section,
        )
        if bounds is None:
            return
        sec_start, sec_end = bounds
        content = content[:sec_start] + content[sec_end:]
        Path(system_reg).write_text(
            content,
            encoding="utf-8",
        )
        logger.info(
            "[UbisoftInstaller] cleaned registry for %s",
            install_id,
        )
    except Exception as e:
        logger.warning(
            "[UbisoftInstaller] registry cleanup failed: %s",
            e,
        )

def get_directory_size(path: str) -> int:
    """Get directory size."""
    total = 0
    with contextlib.suppress(OSError):
        for dirpath, _dirs, filenames in os.walk(path):
            for f in filenames:
                try:
                    total += (Path(dirpath) / f).stat().st_size
                except OSError:
                    continue
    return total

def prefix_has_game_files(prefix_path: str) -> bool:
    """Return True if the prefix's UPC ``games/`` dir holds a game folder.

    A direct filesystem check used as a hard safety guard before deleting an
    abandoned prefix — independent of the (fallible) snapshot-based install
    detector, so real game files are never removed.
    """
    rel = (
        Path("drive_c") / "Program Files (x86)" / "Ubisoft"
        / "Ubisoft Game Launcher" / "games"
    )
    for base in (Path(prefix_path) / rel, Path(prefix_path) / "pfx" / rel):
        with contextlib.suppress(OSError):
            if any(entry.is_dir() for entry in base.iterdir()):
                return True
    return False

def parse_positive_int(value: Any) -> int | None:
    """Parse positive int."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None

class _ShortcutRegistry:
    """Shortcut registry."""

    def __init__(self, config: UbisoftConfig) -> None:
        """Initialize the instance."""
        self._config = config

    def load(self) -> dict[str, Any]:
        """Load."""
        path = Path(self._config.data_dir_expanded) / "shortcuts_registry.json"
        if not path.is_file():
            return {}
        try:
            data = json.loads(
                path.read_text(encoding="utf-8"),
            )
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(
                "[UbisoftInstaller] shortcuts registry load failed: %s",
                e,
            )
            return {}
        if isinstance(data, dict):
            return data
        return {}

    def scan_for_ubisoft_appid(
        self,
        registry: dict[str, Any],
    ) -> int | None:
        """Scan for UBISOFT appid."""
        for key, entry in registry.items():
            if not isinstance(key, str) or not key.startswith("ubisoft:"):
                continue
            if not isinstance(entry, dict):
                continue
            appid = parse_positive_int(
                entry.get("appid_unsigned"),
            )
            if appid:
                return appid
        return None

    def resolve_shortcut_appid(
        self,
        store_game_id: str | None,
    ) -> int | None:
        """Resolve shortcut appid."""
        registry = self.load()
        if store_game_id:
            entry = registry.get(store_game_id, {})
            appid = parse_positive_int(
                entry.get("appid_unsigned"),
            )
            if appid:
                return appid
        appid = self.scan_for_ubisoft_appid(registry)
        if appid:
            return appid
        for env_var in _STEAM_COMPAT_ENV_VARS:
            appid = parse_positive_int(
                os.environ.get(env_var),
            )
            if appid:
                return appid
        if store_game_id:
            appid = self.scan_for_ubisoft_appid(registry)
            if appid:
                return appid
        return None
