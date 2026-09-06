"""config/config_manager.py — Centralized configuration service.

Moved from core/ to the new unifideck.config subpackage as
part of the config consolidation. A shim at core/config_manager.py
preserves backward compatibility for any caller still importing from
the old path.

Replaces 49 scattered constants and hardcoded paths in main.py with a
single ConfigManager instance exposing typed, dot-notated access.
Three layers merged on read (highest priority first):
1. User overrides loaded from ~/.config/unifideck/config.json
2. Shipped defaults from defaults/config.json
3. Hardcoded fallback dict (`_FALLBACK`) in this module
Features:
- Dot-notation keys: config.get("stores.gog.client_secret")
- Hot-reload via reload()
- Typed getters: get_str / get_int / get_bool
- Atomic user-config writes
- Backward-compatible dict-like access via __getitem__ / __contains__
- Derived paths (data_dir/prefixes/, tokens/, sessions/) via helper
 properties
Reference: Technical Document v1.0 — Section 3.9 (Configuration
service), Figure 30.
"""
from __future__ import annotations

import contextlib
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
# Hardcoded last-resort fallback. Every key must have a value here so
# that the plugin boots even if no config files are present.
_FALLBACK: dict[str, Any] = {
    "data_dir": "~/.local/share/unifideck",
    "ui": {
        # ``locale``, matching defaults/config.json — NOT ``language``,
        # which nothing declares and nothing writes. That stray key made
        # every locale lookup return "en-US": ``get_unifideck_locale``
        # reads it as an explicit user preference, and a fallback value
        # is indistinguishable from a real choice, so resolution never
        # reached the Steam language or the POSIX locale below it.
        "locale": "auto",
    },
    "sync": {
        "interval_seconds": 300,
        "cache_ttl_seconds": 3600,
    },
    "download": {
        "timeout_seconds": 30,
        "max_retries": 3,
        "artwork_concurrent": 4,
    },
    "stores": {
        "epic": {},
        "gog": {"client_secret": ""},
        "amazon": {},
        "ubisoft": {},
        "battlenet": {},
        "microsoft": {},
    },
}


def _deep_merge(base: dict[str, Any],
                override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge override into a copy of base. Override wins."""
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def _get_by_path(d: dict[str, Any], dotted: str) -> Any:
    """Walk a nested dict with a dot-separated key. Raises KeyError."""
    node: Any = d
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            raise KeyError(dotted)
        node = node[part]
    return node


def _set_by_path(d: dict[str, Any], dotted: str, value: Any) -> None:
    """Set a nested dict value using a dot-separated key (creating path)."""
    parts = dotted.split(".")
    node = d
    for part in parts[:-1]:
        if part not in node or not isinstance(node[part], dict):
            node[part] = {}
        node = node[part]
    node[parts[-1]] = value


class ConfigManager:
    """3-layer configuration manager.
    Usage:
    config = ConfigManager(
    defaults_path="/path/to/defaults/config.json",
    user_path="~/.config/unifideck/config.json",
    )
    lang = config.get("ui.language", default="en-US")
    config.set("ui.language", "fr-FR") # persisted.
    """

    def __init__(
        self,
        defaults_path: str | None = None,
        user_path: str | None = None,
    ) -> None:
        """Initialize with 3-layer merge.

        Args:
        defaults_path: Path to shipped defaults/config.json.
        If None, only _FALLBACK is used as base.
        user_path: Path to user override file. Created on first
        successful set() call if missing.

        """
        self._defaults_path = Path(defaults_path) if defaults_path else None
        self._user_path = Path(user_path) if user_path else None
        self._merged: dict[str, Any] = {}
        self.reload()
        # -------- loading --------

    def reload(self) -> None:
        """Re-read defaults + user overrides from disk and rebuild the
        merged view. Safe to call at runtime for hot-reload.
        """
        merged = dict(_FALLBACK)
        if self._defaults_path and self._defaults_path.exists():
            try:
                data = json.loads(
                    self._defaults_path.read_text(
                        encoding="utf-8",
                    ),
                )
                # Drop meta keys (comments, schema, …)
                data = {
                    k: v for k, v in data.items()
                    if not k.startswith("_")
                }
                merged = _deep_merge(merged, data)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(
                    "[ConfigManager] defaults unreadable "
                    "(%s): %s", type(e).__name__, e,
                )
        if self._user_path and self._user_path.exists():
            try:
                data = json.loads(
                    self._user_path.read_text(
                        encoding="utf-8",
                    ),
                )
                merged = _deep_merge(merged, data)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(
                    "[ConfigManager] user config unreadable "
                    "(%s): %s", type(e).__name__, e,
                )
        self._merged = merged
        # Validate the i18n section if present. A malformed
        # i18n block means the build-time translator and the
        # frontend locale catalog are out of sync, which
        # produces cryptic runtime errors in the UI. Failing
        # loudly at plugin startup is much friendlier than
        # discovering the problem later when a user can't
        # switch languages.
        #
        # The scripts/ directory sits outside the unifideck
        # package, so we temporarily inject it into sys.path
        # to import the shared locale_config module. This
        # keeps the validation logic in ONE place.
        self._validate_i18n_schema()

    def _validate_i18n_schema(self) -> None:
        """Validate the i18n section of the merged config using
        the shared locale_config module. Logs a warning and
        proceeds if the section is absent (for legacy configs
        that pre-date the i18n feature), but raises on any
        actual schema violation so the plugin fails loudly at
        startup rather than silently at runtime.
        The validation is delegated to scripts/locale_config.py
        to avoid duplicating the schema definition. The scripts
        directory is discovered relative to the defaults file
        location: if defaults_path is defaults/config.json, then
        scripts/ is a sibling directory.
        """
        if "i18n" not in self._merged:
            # Legacy config without i18n — tolerable, the frontend
            # will fall back to whatever locales are actually in
            # src/i18n/locales.generated.ts
            return
        # Discover the scripts/ directory. In a normal install
        # the layout is <repo>/defaults/config.json and
        # <repo>/scripts/locale_config.py, so scripts/ sits at
        # defaults_path.parent.parent / "scripts".
        if not self._defaults_path:
            return
        scripts_dir = self._defaults_path.parent.parent / "scripts"
        if not (scripts_dir / "locale_config.py").is_file():
            # Scripts not shipped with this install — skip
            # validation rather than fail. This can happen in
            # development checkouts where the layout differs.
            logger.debug(
                "[ConfigManager] locale_config.py not found at %s — "
                "skipping i18n schema validation",
                scripts_dir,
            )
            return
        import sys
        # Temporarily add scripts/ to sys.path so we can import
        # locale_config. Use a try/finally to always restore the
        # original path even if import or validation raises.
        scripts_str = str(scripts_dir)
        added = False
        if scripts_str not in sys.path:
            sys.path.insert(0, scripts_str)
            added = True
        try:
            # Same dynamic import as in ``i18n_schema.py``; mypy
            # can't resolve ``scripts/locale_config.py`` because
            # ``scripts/`` is only injected on sys.path at runtime.
            from locale_config import (  # type: ignore[import-not-found]
                LocaleConfigError,
                load_from_dict,
            )
            try:
                load_from_dict(self._merged)
            except LocaleConfigError:
                logger.exception("[ConfigManager] i18n schema validation failed")
                raise
        finally:
            if added:
                sys.path.remove(scripts_str)

    # -------- typed access --------

    def get(self, key: str, default: Any = None) -> Any:
        """Return value at dotted key, or default if missing."""
        try:
            return _get_by_path(self._merged, key)
        except KeyError:
            return default

    def get_str(self, key: str, default: str = "") -> str:
        v = self.get(key, default)
        return str(v) if v is not None else default

    def get_int(self, key: str, default: int = 0) -> int:
        v = self.get(key, default)
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    def get_bool(self, key: str, default: bool = False) -> bool:
        v = self.get(key, default)
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.strip().lower() in (
                "true", "1", "yes", "on",
            )
        return bool(v)

    def set(self, key: str, value: Any) -> None:
        """Update in-memory config AND persist to user_path atomically.
        Only the user layer is written — defaults and fallback are
        never touched.
        """
        _set_by_path(self._merged, key, value)
        if not self._user_path:
            return
        # Load existing user file to preserve other overrides
        user_data: dict[str, Any] = {}
        if self._user_path.exists():
            try:
                user_data = json.loads(
                    self._user_path.read_text(encoding="utf-8"),
                )
            except (json.JSONDecodeError, OSError):
                user_data = {}
        _set_by_path(user_data, key, value)
        self._user_path.parent.mkdir(
            parents=True, exist_ok=True,
        )
        tmp = self._user_path.with_suffix(
            self._user_path.suffix + ".tmp",
        )
        try:
            tmp.write_text(
                json.dumps(
                    user_data,
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            tmp.replace(self._user_path)
        except OSError:
            logger.exception("[ConfigManager] failed to persist %s", key)
            if tmp.exists():
                with contextlib.suppress(OSError):
                    tmp.unlink()

    @property
    def data_dir(self) -> str:
        """Base data directory (with ~ expansion).
        Reads `paths.data_dir` first (the v1.0 audit-correct
        location), falling back to the legacy top-level
        `data_dir` key for backward compatibility with old user
        configs.
        """
        raw = self.get("paths.data_dir") or self.get_str(
            "data_dir", "~/.local/share/unifideck",
        )
        return str(Path(raw).expanduser())

    @property
    def cache_dir(self) -> str:
        """Base cache directory (with ~ expansion).
        Reads `paths.cache_dir` from the audit-correct location.
        Falls back to `<data_dir>/cache` if unset.
        """
        raw = self.get("paths.cache_dir")
        if raw:
            return str(Path(raw).expanduser())
        return str(Path(self.data_dir) / "cache")

    def path(self, *parts: str) -> str:
        """Return data_dir/<parts> as absolute path.
        Special-cased: `path("cache")` returns the cache_dir
        property so the CacheManager honors `paths.cache_dir`.
        """
        if len(parts) == 1 and parts[0] == "cache":
            return self.cache_dir
        return str(Path(self.data_dir).joinpath(*parts))

    # -------- dict-like backward compat --------

    def __getitem__(self, key: str) -> Any:
        val = self.get(key)
        if val is None:
            raise KeyError(key)
        return val

    def __contains__(self, key: str) -> bool:
        return self.get(key) is not None
