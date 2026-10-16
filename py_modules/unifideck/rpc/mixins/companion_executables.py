"""CompanionExecutablesRPCMixin — user-configured extra exes per game.

Powers the "Companion executables…" item injected into the native game
context menu (see ``ChangeExecutableModal``'s sibling modal on the
frontend). Lets the user attach trainers/cheat tools/utilities that get
launched alongside the main game in the same Proton prefix — the feature
that replaces relying on third-party Decky plugins (e.g. CheatDeck) to
inject a companion process via Steam launch options, which breaks the
``store:game_id`` argv the launcher dispatcher depends on.

Storage: ``games.<store>:<game_id>.companion_executables`` in
``ConfigManager`` — a JSON list of ``{"path": abs_path, "delay_seconds":
float}``. Unlike the "Change executable" override, paths are NOT
constrained to the game's install dir (a trainer usually lives in
``~/Downloads``); the only validation is "this is a real file".

Three RPCs:

* ``list_companion_executables`` — the currently-configured list.
* ``add_companion_executable`` — append one (path, delay_seconds).
* ``remove_companion_executable`` — drop one by path.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from unifideck.rpc import RpcError

logger = logging.getLogger(__name__)


def _config_key(store: str, game_id: str) -> str:
    return f"games.{store}:{game_id}.companion_executables"


def _load_list(config: Any, store: str, game_id: str) -> list[dict[str, Any]]:
    """Read the raw companion list for a game, tolerating malformed data."""
    raw = config.get(_config_key(store, game_id), [])
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for entry in raw:
        if isinstance(entry, dict) and isinstance(entry.get("path"), str):
            out.append({
                "path": entry["path"],
                "delay_seconds": _coerce_delay(entry.get("delay_seconds")),
            })
    return out


def _coerce_delay(value: Any) -> float:
    try:
        return max(0.0, float(value or 0.0))
    except (TypeError, ValueError):
        return 0.0


class CompanionExecutablesRPCMixin:
    """User-configured companion-executable RPC surface."""

    config: Any

    async def list_companion_executables(
        self, store: str, game_id: str,
    ) -> Any:
        """Return this game's configured companion executables."""
        if not store or not game_id:
            raise RpcError("invalid_args", store=store, game_id=game_id)
        return {"companions": _load_list(self.config, store, game_id)}

    async def add_companion_executable(
        self, store: str, game_id: str, path: str, delay_seconds: float = 0.0,
    ) -> Any:
        """Attach one companion executable to ``store:game_id``.

        Rejects a path that isn't a real, existing file (path traversal
        is not a concern here the way it is for "Change executable" —
        the path comes from the OS file picker, not a relative-path RPC
        argument — but a nonexistent file would just fail silently at
        every future launch, so it's caught here instead).  Adding the
        same path twice updates its delay rather than duplicating it.
        """
        if not store or not game_id or not path:
            raise RpcError("invalid_args", store=store, game_id=game_id)
        if not await asyncio.to_thread(os.path.isfile, path):
            raise RpcError("invalid_executable", path=path)
        companions = _load_list(self.config, store, game_id)
        delay = _coerce_delay(delay_seconds)
        for entry in companions:
            if entry["path"] == path:
                entry["delay_seconds"] = delay
                break
        else:
            companions.append({"path": path, "delay_seconds": delay})
        self.config.set(_config_key(store, game_id), companions)
        logger.info(
            "[CompanionExecutables] added %s → %s:%s (delay=%.1fs)",
            path, store, game_id, delay,
        )
        return {"success": True, "companions": companions}

    async def remove_companion_executable(
        self, store: str, game_id: str, path: str,
    ) -> Any:
        """Detach one companion executable by its path."""
        if not store or not game_id or not path:
            raise RpcError("invalid_args", store=store, game_id=game_id)
        companions = _load_list(self.config, store, game_id)
        remaining = [entry for entry in companions if entry["path"] != path]
        self.config.set(_config_key(store, game_id), remaining)
        logger.info(
            "[CompanionExecutables] removed %s from %s:%s",
            path, store, game_id,
        )
        return {"success": True, "companions": remaining}
