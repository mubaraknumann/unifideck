"""support_bundle/env_report.py — Assemble the environment report.

Thin orchestrator over the four probe modules. Its only real job is
isolation: every block runs inside :func:`_safe`, so one probe raising
on an unusual device records an ``error`` for that block and leaves the
rest of the report intact. A diagnostics capture that dies while
describing a broken machine would be worthless precisely when it
matters most.

Emits two renderings of the same data: ``environment.json`` for
machine reading, and flat ``a.b.c: value`` lines for the human-readable
``diagnostics.txt``, so an engineer can grep without reaching for jq.
"""
from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import (
    probe_conflicts,
    probe_device,
    probe_plugin_logs,
    probe_protontricks,
    probe_stack,
    probe_storage,
)
from .spec import BundleContext

logger = logging.getLogger(__name__)

_MAX_RENDER_LIST = 40


def _safe(name: str, probe: Callable[[], Any]) -> Any:
    """Run one probe, converting any failure into an error record."""
    try:
        return probe()
    except Exception as err:
        logger.debug("[support_bundle] probe %s failed", name, exc_info=True)
        return {"error": repr(err)}


def _ui_locale(data_dir: str | None) -> str:
    """Read the configured UI language out of ``settings.json``."""
    if not data_dir:
        return ""
    path = Path(data_dir) / "settings.json"
    try:
        parsed = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return ""
    if not isinstance(parsed, dict):
        return ""
    return str(parsed.get("locale") or parsed.get("language") or "")


def _device_blocks(ctx: BundleContext) -> dict[str, Callable[[], Any]]:
    """Hardware, OS and session probes."""
    data = ctx.root("data")
    return {
        "device": probe_device.device_block,
        "os": probe_device.os_block,
        "kernel": probe_device.kernel_block,
        "cpu": probe_device.cpu_block,
        "gpu": probe_device.gpu_block,
        "memory": probe_device.memory_block,
        "plugin_memory": probe_device.plugin_memory_block,
        "session": probe_device.session_block,
        "python": probe_device.python_block,
        "time": probe_device.time_block,
        "locale": lambda: probe_device.locale_block(_ui_locale(data)),
    }


def _stack_blocks(ctx: BundleContext) -> dict[str, Callable[[], Any]]:
    """Steam, Decky, our install, the runtime, and the caches."""
    data = ctx.root("data")
    steam = ctx.root("steam")
    plugin = ctx.root("plugin")
    return {
        "identity": probe_stack.identity_block,
        "decky": probe_stack.decky_block,
        "plugin": lambda: probe_stack.plugin_block(plugin),
        "steam": lambda: probe_stack.steam_block(
            steam, ctx.root_sources.get("steam", "unknown"),
        ),
        "runtime": lambda: probe_stack.runtime_block(steam, data),
        # Caches live under Decky's per-plugin *runtime* dir, which is a
        # different location from the install dir the plugin is served
        # from — passing the latter found nothing.
        "caches": lambda: probe_stack.caches_block(
            data, os.environ.get("DECKY_PLUGIN_RUNTIME_DIR"),
        ),
        "artwork": lambda: probe_stack.artwork_block(
            _paths_attr(ctx, "grid_dir"),
        ),
        "playtime": lambda: probe_stack.playtime_block(
            _paths_attr(ctx, "playtime_db"),
        ),
        "storage": lambda: probe_storage.storage_block(ctx.config),
    }


def _conflict_blocks(ctx: BundleContext) -> dict[str, Callable[[], Any]]:
    """Third-party interference and stale live state."""
    return {
        "plugins_installed": probe_conflicts.plugins_block,
        # Which sibling plugin logs the archive carries. Names and versions
        # alone can only ever make a neighbour a suspect; their logs are what
        # convict or clear one.
        "plugin_logs": probe_plugin_logs.plugin_logs_block,
        # External Wine tooling is a conflict surface too: it reads the same
        # prefixes and resolves the same Proton, by its own rules.
        "protontricks": probe_protontricks.protontricks_block,
        "scheduled_writers": probe_conflicts.scheduled_writers_block,
        "processes": probe_conflicts.processes_block,
        "session_env": probe_conflicts.session_env_block,
        "wine_locks": lambda: probe_conflicts.wine_locks_block(ctx.root("data")),
        "shortcuts_race": lambda: probe_conflicts.shortcuts_race_block(
            _paths_attr(ctx, "shortcuts_path"),
        ),
        # Ours-vs-theirs split of shortcuts.vdf plus the backup inventory.
        # The measurement that makes a "my non-Steam games vanished"
        # report answerable instead of a guess.
        "shortcuts_census": lambda: probe_conflicts.shortcuts_census_block(
            _paths_attr(ctx, "shortcuts_path"),
            ctx.root("data"),
            _paths_attr(ctx, "launcher_path"),
        ),
    }


def _blocks(ctx: BundleContext) -> dict[str, Callable[[], Any]]:
    """Every probe, bound as a thunk.

    Thunks rather than inline calls so :func:`_safe` can wrap each one
    individually — that is what keeps a single bad probe from taking the
    whole report down. Grouped across three builders to keep each one's
    fan-out inside the project's complexity cap.
    """
    return {
        **_device_blocks(ctx),
        **_stack_blocks(ctx),
        **_conflict_blocks(ctx),
    }


def _paths_attr(ctx: BundleContext, name: str) -> str | None:
    """Read one :class:`ServicePaths` field as a string."""
    raw = getattr(ctx.paths, name, None) if ctx.paths is not None else None
    return str(raw) if raw else None


def build_environment_report(ctx: BundleContext) -> dict[str, Any]:
    """Build the full environment report for ``ctx``."""
    report: dict[str, Any] = {
        "roots": dict(ctx.roots),
        "root_sources": dict(ctx.root_sources),
        "frontend": ctx.extra or {},
    }
    for name, probe in _blocks(ctx).items():
        report[name] = _safe(name, probe)
    return report


def render_text(report: dict[str, Any]) -> str:
    """Flatten the report to greppable ``a.b.c: value`` lines."""
    lines: list[str] = []
    for key in sorted(report):
        _render_value(f"{key}", report[key], lines)
    return "\n".join(lines) + "\n"


def _render_value(prefix: str, value: Any, lines: list[str]) -> None:
    """Append one value, recursing into dicts and short lists."""
    if isinstance(value, dict):
        if not value:
            lines.append(f"{prefix}: {{}}")
            return
        for key in sorted(value, key=str):
            _render_value(f"{prefix}.{key}", value[key], lines)
        return
    if isinstance(value, list):
        _render_list(prefix, value, lines)
        return
    lines.append(f"{prefix}: {value}")


def _render_list(prefix: str, value: list[Any], lines: list[str]) -> None:
    """Render a list, indexing dict members and inlining scalars."""
    if not value:
        lines.append(f"{prefix}: []")
        return
    shown = value[:_MAX_RENDER_LIST]
    if all(not isinstance(item, (dict, list)) for item in shown):
        lines.append(f"{prefix}: {', '.join(str(item) for item in shown)}")
    else:
        for index, item in enumerate(shown):
            _render_value(f"{prefix}[{index}]", item, lines)
    if len(value) > _MAX_RENDER_LIST:
        lines.append(f"{prefix}: ...{len(value) - _MAX_RENDER_LIST} more omitted")
