"""launcher/launch_flags.py — per-launch flags read outside games.map.

Split out of ``dispatcher.py`` to keep that file under its line cap. The
circuit-breaker bypass is the one flag here today: it comes either from the
``UNIFIDECK_BYPASS_CIRCUIT_BREAKER`` launch option or from a one-shot entry
that the plugin left in the launch history.
"""
from __future__ import annotations

import os
from pathlib import Path


def resolve_bypass_flag(store: str, game_id: str, plugin_dir: Path) -> bool:
    """Whether this launch skips the circuit breaker.

    True when the env token is set, or when the launch history holds a
    one-shot bypass for this game (consumed here). Any failure to read the
    history counts as no bypass.
    """
    bypass_raw = os.environ.get(
        "UNIFIDECK_BYPASS_CIRCUIT_BREAKER", "",
    )
    bypass_env = bypass_raw.strip().lower() in (
        "1", "true", "yes",
    )
    try:
        from unifideck.config.config_manager import ConfigManager
        from unifideck.config.defaults_path import resolve_defaults_config_path
        from unifideck.services.launch_history import LaunchHistoryService
        # See ``launcher/bootstrap._load_standalone_config``: the nested
        # ``defaults/`` path does not exist on a Decky CLI build.
        cfg = ConfigManager(resolve_defaults_config_path(plugin_dir))
        lh = LaunchHistoryService(cfg)
        bypass_flag = lh.consume_bypass(f"{store}:{game_id}")
    except Exception:
        bypass_flag = False
    return bypass_env or bypass_flag
