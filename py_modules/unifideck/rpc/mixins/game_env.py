"""GameEnvRPCMixin — general per-game environment-variable overrides.

Powers the "Environment variables…" item injected into the native game
context menu, next to "Frame Generation (OptiScaler)…". General-purpose
sibling of ``OptiScalerRPCMixin``'s
``optiscaler_env`` (which only reaches the fgmod patch subprocess) — these
apply to the GAME'S OWN launch, exactly like Steam's ``VAR=value %command%``
convention documented in ``docs/launch-options.md``, but:

* persist across a **Force Sync** (which resets a shortcut's Launch Options
  back to plain ``store:game_id``, wiping any hand-edited ``VAR=value``
  prefix — see that doc's "Persistence across library sync" section);
* don't require editing Steam's Launch Options field (gamepad-unfriendly:
  no on-screen keyboard-friendly way to type ``MANGOHUD=1 DXVK_HUD=fps`` on
  a Deck) at all.

Storage: ``games.<store>:<game_id>.env_overrides`` in ``ConfigManager`` — a
flat JSON ``{NAME: value}`` object. Read by
``launcher.dispatcher._resolve_game_env_overrides`` and merged into
``LaunchContext.env_overrides``, the SAME field the (not-yet-reconnected)
``VAR=value %command%`` launch-options parser would populate — so once that
parser is wired back in, the two sources merge naturally.

Two RPCs:

* ``get_game_env`` — the currently-configured overrides for one game.
* ``set_game_env`` — persist a full replacement dict.
"""
from __future__ import annotations

import logging
from typing import Any

from unifideck.rpc import RpcError

logger = logging.getLogger(__name__)


def _config_key(store: str, game_id: str) -> str:
    return f"games.{store}:{game_id}.env_overrides"


def _load_env(config: Any, store: str, game_id: str) -> dict[str, str]:
    """Read this game's configured env-var overrides.

    Tolerant of malformed config — non-dict or non-string values are
    dropped rather than raised, so a corrupt entry can't break launches
    or the modal that displays this list.
    """
    raw = config.get(_config_key(store, game_id), {})
    if not isinstance(raw, dict):
        return {}
    return {
        str(k): str(v)
        for k, v in raw.items()
        if isinstance(k, str) and k and isinstance(v, (str, int, float))
    }


class GameEnvRPCMixin:
    """General per-game environment-variable-override RPC surface."""

    config: Any

    async def get_game_env(self, store: str, game_id: str) -> Any:
        """Return this game's configured environment-variable overrides."""
        if not store or not game_id:
            raise RpcError("invalid_args", store=store, game_id=game_id)
        return {"env": _load_env(self.config, store, game_id)}

    async def set_game_env(
        self, store: str, game_id: str, env: dict[str, str],
    ) -> Any:
        """Persist this game's environment-variable overrides.

        ``env`` is a flat ``{name: value}`` mapping — applied to the
        game's OWN launch on every future run (via
        ``launcher.dispatcher._resolve_game_env_overrides``), not written
        to any file on disk here. A full replacement, like
        ``OptiScalerRPCMixin.set_optiscaler_env`` — the modal always saves
        the complete edited list, not a diff.
        """
        if not store or not game_id:
            raise RpcError("invalid_args", store=store, game_id=game_id)
        if not isinstance(env, dict):
            raise RpcError("invalid_args", store=store, game_id=game_id)
        clean = {
            str(k).strip(): str(v)
            for k, v in env.items()
            if isinstance(k, str) and k.strip()
        }
        self.config.set(_config_key(store, game_id), clean)
        logger.info(
            "[GameEnv] overrides set for %s:%s → %d var(s)",
            store, game_id, len(clean),
        )
        return {"success": True, "env": clean}
