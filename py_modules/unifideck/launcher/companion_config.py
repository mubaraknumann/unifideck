"""launcher/companion_config.py — read a game's configured companion exes.

Split out of ``dispatcher.py`` when wiring the companion-executables feature
pushed that file over the 550-LOC cap. Self-contained: reads the persisted
list a :class:`CompanionExecutablesRPCMixin` writes and returns typed tuples
for the launch context.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .types.context import CompanionExecutable

logger = logging.getLogger(__name__)


def resolve_companion_executables(
    store: str, game_id: str, plugin_dir: Path,
) -> tuple[CompanionExecutable, ...]:
    """Read this game's configured companion executables (trainers, etc.).

    Stored by ``CompanionExecutablesRPCMixin`` under the config key
    ``games.<store>:<game_id>.companion_executables`` as a JSON list of
    ``{"path": ..., "delay_seconds": ...}`` objects. Best-effort: any
    malformed entry is skipped rather than failing the whole launch, and
    a missing/unreadable config yields an empty tuple (no companions).
    """
    try:
        from unifideck.config.config_manager import ConfigManager
        from unifideck.launcher.bootstrap import _user_config_path
        cfg = ConfigManager(
            str(plugin_dir / "defaults" / "config.json"),
            user_path=_user_config_path(),
        )
        raw = cfg.get(f"games.{store}:{game_id}.companion_executables", [])
    except Exception:
        logger.exception(
            "[launcher.companion_config] read failed for %s:%s",
            store, game_id,
        )
        return ()
    if not isinstance(raw, list):
        return ()
    result: list[CompanionExecutable] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        if not isinstance(path, str) or not path:
            continue
        try:
            delay = float(entry.get("delay_seconds", 0.0) or 0.0)
        except (TypeError, ValueError):
            delay = 0.0
        result.append(CompanionExecutable(path=path, delay_seconds=delay))
    return tuple(result)
