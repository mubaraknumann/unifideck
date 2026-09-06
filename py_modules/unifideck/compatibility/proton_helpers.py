from __future__ import annotations

import contextlib
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

# Re-exported for backward-compat: the pure config.vdf KeyValues parsers now
# live in the launcher-safe ``utils.vdf_compat`` (stdlib-only) so the
# out-of-process launcher can share them without importing this aiohttp-heavy
# package. Importing them here keeps ``proton_helpers.parse_*`` call sites working.
from unifideck.utils.vdf_compat import (
    parse_compat_tool,
    parse_global_default_compat_tool,
)

if TYPE_CHECKING:
    from unifideck.config import ConfigManager
logger = logging.getLogger(__name__)
LINUX_RUNTIME_PREFIXES = (
    "steamlinuxruntime",
    "scout",
    "soldier",
    "sniper",
    "medic",
)
DEFAULT_CONFIG_VDF_RELATIVE = "config/config.vdf"
DEFAULT_PROTON_SETTINGS_RELATIVE = (
    ".local/share/unifideck/proton_settings.json"
)
DEFAULT_SHORTCUTS_REGISTRY_RELATIVE = (
    ".local/share/unifideck/shortcuts_registry.json"
)
@dataclass
class CompatToolResult:
    """Compat tool result."""
    success: bool
    appid: int
    tool_name: str
    previous: str | None = None
    error: str | None = None
    def to_dict(self) -> dict[str, Any]:
        """To dict."""
        return {
            "success": self.success,
            "appid": self.appid,
            "tool_name": self.tool_name,
            "previous": self.previous,
            "error": self.error,
        }
def is_linux_runtime(tool_name: str) -> bool:
    """Check whether linux runtime."""
    if not tool_name:
        return False
    lower = tool_name.lower()
    return any(
        lower.startswith(prefix) or f"_{prefix}" in lower
        for prefix in LINUX_RUNTIME_PREFIXES
    )

def inject_compat_tool(
    content: str, appid: int, tool_name: str,
) -> str:
    """Inject compat tool."""
    if tool_name and not re.match(
        r"^[A-Za-z0-9._\-]*$", tool_name,
    ):
        raise ValueError(
            f"invalid compat tool name: {tool_name!r} "
            f"(must match [A-Za-z0-9._-])",
        )
    if not content:
        return content
    appid_str = str(appid)
    pattern = re.compile(
        rf'("{appid_str}"\s*\{{[^}}]*"name"\s+)"[^"]*"',
        re.DOTALL,
    )
    new_content, count = pattern.subn(
        rf'\1"{tool_name}"', content, count=1,
    )
    if count > 0:
        return new_content
    marker = '"CompatToolMapping"'
    marker_pos = content.find(marker)
    if marker_pos < 0:
        return content
    open_brace = content.find("{", marker_pos)
    if open_brace < 0:
        return content
    insertion = (
        f'\n\t\t"{appid_str}"\n'
        f'\t\t{{\n'
        f'\t\t\t"name"\t\t"{tool_name}"\n'
        f'\t\t\t"config"\t\t""\n'
        f'\t\t\t"priority"\t\t"250"\n'
        f'\t\t}}'
    )
    return (
        content[:open_brace + 1]
        + insertion
        + content[open_brace + 1:]
    )
class ProtonToolsManager:
    """Proton tools manager."""
    def __init__(self, config: ConfigManager | None = None) -> None:
        """Initialize the instance."""
        self._config = config
        self._config_vdf_path = self._resolve_config_vdf()
        self._proton_settings_path = (
            self._resolve_proton_settings()
        )
        self._shortcuts_registry_path = (
            self._resolve_shortcuts_registry()
        )

    def _resolve_config_vdf(self) -> Path:

        """Resolve config VDF."""
        from unifideck.steam.library import find_steam_path
        steam = find_steam_path(self._config)
        if steam is None:
            return (
                Path.home()
                / ".local/share/Steam"
                / DEFAULT_CONFIG_VDF_RELATIVE
            )
        return Path(steam) / DEFAULT_CONFIG_VDF_RELATIVE
    def _resolve_proton_settings(self) -> Path:
        """Resolve PROTON settings."""
        return Path(
            self._cfg(
                "proton.settings_path",
                "~/" + DEFAULT_PROTON_SETTINGS_RELATIVE,
            ),
        ).expanduser()
    def _resolve_shortcuts_registry(self) -> Path:
        """Resolve shortcuts registry."""
        return Path(
            self._cfg(
                "proton.shortcuts_registry_path",
                "~/" + DEFAULT_SHORTCUTS_REGISTRY_RELATIVE,
            ),
        ).expanduser()
    def _cfg(self, key: str, default: Any) -> Any:
        """Cfg."""
        if self._config is None:
            return default
        try:
            return self._config.get(key, default)
        except Exception:
            return default
    def get_for_app(self, appid: int) -> CompatToolResult:
        """Get for app."""
        content = self._read_config_vdf()
        tool = parse_compat_tool(content, appid)
        return CompatToolResult(
            success=True, appid=appid, tool_name=tool,
        )
    def get_global_default(self) -> str:
        """Return the global-default compat tool (``CompatToolMapping["0"]``)."""
        return parse_global_default_compat_tool(self._read_config_vdf())
    def set_for_app(
        self, appid: int, tool_name: str,
    ) -> CompatToolResult:
        """Set for app."""
        content = self._read_config_vdf()
        if not content:
            return CompatToolResult(
                success=False, appid=appid,
                tool_name=tool_name,
                error="config.vdf not readable",
            )
        previous = parse_compat_tool(content, appid)
        new_content = inject_compat_tool(
            content, appid, tool_name,
        )
        if not self._write_config_vdf(new_content):
            return CompatToolResult(
                success=False, appid=appid,
                tool_name=tool_name,
                error="config.vdf write failed",
            )
        return CompatToolResult(
            success=True, appid=appid, tool_name=tool_name,
            previous=previous,
        )
    def clear_for_app(self, appid: int) -> CompatToolResult:
        """Clear for app."""
        return self.set_for_app(appid, "")
    def list_known_tools(self) -> list[str]:
        """List known tools."""
        tools: list[str] = []
        steam_root = self._config_vdf_path.parent.parent
        for sub in ("compatibilitytools.d", "steamapps/common"):
            d = steam_root / sub
            if not d.is_dir():
                continue
            try:
                for child in sorted(d.iterdir()):
                    if child.is_dir():
                        tools.append(child.name)
            except OSError:
                continue
        return tools

    def _read_config_vdf(self) -> str:

        """Read config VDF."""
        try:
            return self._config_vdf_path.read_text(
                encoding="utf-8", errors="ignore",
            )
        except OSError:
            logger.exception("[proton_helpers] read %s failed", self._config_vdf_path)
            return ""
    def _write_config_vdf(self, content: str) -> bool:
        """Write config VDF."""
        tmp = self._config_vdf_path.with_suffix(".vdf.tmp")
        try:
            tmp.parent.mkdir(parents=True, exist_ok=True)
            with tmp.open("w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            Path(tmp).replace(self._config_vdf_path)
            return True
        except OSError:
            logger.exception("[proton_helpers] write failed")
            with contextlib.suppress(OSError):
                tmp.unlink()
            return False
    def load_proton_settings(self) -> dict[str, Any]:
        """Load PROTON settings."""
        try:
            return cast(
                "dict[str, Any]",
                json.loads(self._proton_settings_path.read_text()),
            )
        except (OSError, json.JSONDecodeError):
            return {"games": {}}
    def save_proton_settings(
        self, data: dict[str, Any],
    ) -> bool:
        """Save PROTON settings."""
        path = self._proton_settings_path
        tmp = path.with_suffix(".json.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data, indent=2))
            Path(tmp).replace(path)
            return True
        except OSError:
            logger.exception("[proton_helpers] save settings failed")
            return False
_singleton_pt_mgr: ProtonToolsManager | None = None


def _pt_mgr() -> ProtonToolsManager:
    """Return the singleton ProtonToolsManager, creating it on first call."""
    global _singleton_pt_mgr
    if _singleton_pt_mgr is None:
        _singleton_pt_mgr = ProtonToolsManager()
    return _singleton_pt_mgr


def get_compat_tool_for_app(appid_unsigned: int) -> str:
    """Return the compat tool name registered for ``appid_unsigned``."""
    return (
        _pt_mgr()
        .get_for_app(int(appid_unsigned))
        .tool_name
    )


def get_global_default_compat_tool() -> str:
    """Return Steam's global-default compat tool name (or empty string).

    This is ``CompatToolMapping["0"]`` — the tool applied to every title
    that lacks an explicit per-game override. Used to tell a genuine
    per-game Force-Compat choice apart from a distro/system default (e.g.
    Bazzite's ``Proton-CachyOS Latest``) that should NOT be adopted as a
    per-game override.
    """
    return _pt_mgr().get_global_default()


def temporarily_clear_compat_tool(appid_unsigned: int) -> dict[str, Any]:
    """Clear the compat tool for ``appid_unsigned``, returning previous state."""
    result = _pt_mgr().clear_for_app(int(appid_unsigned))
    return {
        "success": result.success,
        "previous": result.previous,
    }


def restore_compat_tool(appid_unsigned: int, tool_name: str) -> dict[str, bool]:
    """Restore the compat tool ``tool_name`` for ``appid_unsigned``."""
    result = _pt_mgr().set_for_app(
        int(appid_unsigned), tool_name,
    )
    return {"success": result.success}


def save_proton_setting(
    store_game_id: str, tool_name: str,
) -> dict[str, bool]:
    """Persist the chosen ``tool_name`` for ``store_game_id``."""
    settings = _pt_mgr().load_proton_settings()
    settings.setdefault("games", {})[store_game_id] = tool_name
    return {
        "success": _pt_mgr().save_proton_settings(settings),
    }


def get_saved_proton_tool(store_game_id: str) -> str:
    """Return the saved Proton tool for ``store_game_id`` (or empty string)."""
    return str(
        _pt_mgr()
        .load_proton_settings()
        .get("games", {})
        .get(store_game_id, ""),
    )
