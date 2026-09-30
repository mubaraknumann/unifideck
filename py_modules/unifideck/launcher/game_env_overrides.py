"""launcher/game_env_overrides.py — per-game persisted environment overrides.

Split out of ``dispatcher.py`` when wiring the per-game env feature pushed
that file over the 550-LOC cap. The one function here reads a game's
persisted ``env_overrides`` from config; it belongs beside the dispatcher's
other env helpers but is self-contained and easy to test in isolation.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def resolve_game_env_overrides(
    store: str, game_id: str, plugin_dir: Path,
) -> dict[str, str]:
    """Read this game's persisted per-game environment-variable overrides.

    Stored by ``GameEnvRPCMixin`` under the config key
    ``games.<store>:<game_id>.env_overrides`` as a flat JSON
    ``{NAME: value}`` object — the general-purpose sibling of
    ``optiscaler_env`` (OptiScaler-specific config vars applied only to the
    fgmod patch subprocess). These apply to the GAME'S OWN launch, exactly
    like Steam's ``VAR=value %command%`` convention (see
    ``docs/launch-options.md``), but persist across a Force Sync (which
    resets Launch Options back to plain ``store:game_id``) and don't require
    editing Steam's launch-options field at all.

    Merged into ``LaunchContext.env_overrides``, which
    ``prepare_native_env``/``prepare_proton_env`` fold into the child
    process's environment — the SAME field Steam's own
    ``VAR=value %command%`` tokens populate via
    ``types.options.parse_launch_options`` (not yet wired into the live
    dispatch path — see that module's docstring), so once that parser is
    reconnected these two sources merge naturally with launch-options taking
    precedence (applied later in the pipeline).

    Best-effort: any malformed entry is skipped rather than failing the
    whole launch, and a missing/unreadable config yields an empty dict (no
    overrides).
    """
    try:
        from unifideck.config.config_manager import ConfigManager
        from unifideck.launcher.bootstrap import _user_config_path
        cfg = ConfigManager(
            str(plugin_dir / "defaults" / "config.json"),
            user_path=_user_config_path(),
        )
        raw = cfg.get(f"games.{store}:{game_id}.env_overrides", {})
    except Exception:
        logger.exception(
            "[launcher.game_env] env_overrides read failed for %s:%s",
            store, game_id,
        )
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(k): str(v)
        for k, v in raw.items()
        if isinstance(k, str) and k and isinstance(v, (str, int, float))
    }
