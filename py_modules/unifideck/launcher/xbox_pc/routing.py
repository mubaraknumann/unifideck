"""launcher/xbox_pc/routing.py — where a Microsoft title's Play goes.

Called by the dispatcher for every Microsoft launch, right after the
auth/storefront check and before the exe lookup. The exe repair guess and
the Proton/umu path must never see an Xbox PC install (I-017).

====================  ======  =====================================  ===================
games.map row         toggle  cached record has ``xcloud`` (or none)  result
====================  ======  =====================================  ===================
yes                   on      any                                    Xodus launch
yes                   off     yes                                    xCloud stream
yes                   off     no                                     notice ``disabled``
no                    any     yes                                    xCloud stream
no                    any     no                                     notice ``not_installed``
====================  ======  =====================================  ===================

A notice launches nothing: the service shows why and returns a failure. A
PC-only title must never open a stream for a game xCloud does not carry.
The toggle is read from the user's config (``launcher_service._config``),
never from a cold-start helper.

Stdlib only, Python 3.10 safe.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Protocol

from unifideck.launcher.argv_options import env_overrides_from
from unifideck.launcher.browser_games import browser_context, browser_target, cached_game
from unifideck.launcher.launch_flags import resolve_bypass_flag
from unifideck.launcher.types.context import LaunchContext

from .layout import XBOX_PC_ENABLED_KEY

logger = logging.getLogger(__name__)

STORE = "microsoft"
XCLOUD_TAG = "xcloud"


class ShortcutLookup(Protocol):
    """The one ``ShortcutService`` method routing needs (no service import here)."""

    async def get_entry_for_game_key(self, store: str, game_id: str) -> Any:
        """The games.map row of ``store:game_id``, or None."""


def xbox_pc_enabled(config: Any | None) -> bool:
    """Whether the Xbox PC toggle is on. Any read failure counts as off."""
    if config is None:
        return False
    try:
        return config.get_bool(XBOX_PC_ENABLED_KEY) is True
    except Exception as err:
        logger.warning("[xbox_pc.routing] toggle unreadable, treating as off: %s", err)
        return False


def streams(game_id: str) -> bool:
    """Whether the cached record says xCloud carries this title.

    No cached record keeps today's behaviour (stream), so a library cache
    written before Xbox PC support still launches.
    """
    record = cached_game(STORE, game_id)
    if record is None:
        return True
    return XCLOUD_TAG in set(record.get("tags") or [])


def notice_context(
    game_id: str, raw_options: str, reason: str, plugin_dir: Path,
) -> LaunchContext:
    """A launch that runs nothing and shows why (``disabled``, ``not_installed``)."""
    return LaunchContext(
        store=STORE,
        game_id=game_id,
        exe_path=Path("xbox_pc"),
        work_dir=plugin_dir,
        plugin_dir=plugin_dir,
        raw_options=raw_options,
        env_overrides=env_overrides_from(raw_options),
        is_launch_action=True,
        bypass_circuit_breaker=False,
        launch_backend="xodus_notice",
        notice_reason=reason,
    )


def xodus_context(
    game_id: str, raw_options: str, entry: Any, plugin_dir: Path,
) -> LaunchContext:
    """The context of an installed Xbox PC game (its games.map row)."""
    app_id = getattr(entry, "app_id", 0) or 0
    work_dir = str(getattr(entry, "work_dir", "") or "")
    return LaunchContext(
        store=STORE,
        game_id=game_id,
        exe_path=Path(str(getattr(entry, "exe", "") or "xbox_pc")),
        work_dir=Path(work_dir) if work_dir else plugin_dir,
        plugin_dir=plugin_dir,
        raw_options=raw_options,
        env_overrides=env_overrides_from(raw_options),
        is_launch_action=True,
        steam_app_id=str(app_id) if app_id else None,
        bypass_circuit_breaker=resolve_bypass_flag(STORE, game_id, plugin_dir),
        launch_backend="xodus",
    )


def _stream_or_notice(
    game_id: str, raw_options: str, reason: str, plugin_dir: Path,
) -> LaunchContext:
    """The xCloud stream when xCloud carries the title, else the notice."""
    target = browser_target(STORE, game_id) if streams(game_id) else None
    if target is None:
        logger.info("[xbox_pc.routing] %s: notice %s (no stream)", game_id, reason)
        return notice_context(game_id, raw_options, reason, plugin_dir)
    logger.info("[xbox_pc.routing] %s: xCloud stream", game_id)
    return browser_context(STORE, game_id, raw_options, target, plugin_dir)


async def microsoft_context(
    shortcut_svc: ShortcutLookup,
    config: Any | None,
    game_id: str,
    raw_options: str,
    *,
    plugin_dir: Path,
) -> LaunchContext:
    """The launch context of one Microsoft title, by the table above."""
    entry = await shortcut_svc.get_entry_for_game_key(STORE, game_id)
    if entry is not None:
        if xbox_pc_enabled(config):
            logger.info("[xbox_pc.routing] %s: installed, Xodus launch", game_id)
            return xodus_context(game_id, raw_options, entry, plugin_dir)
        return _stream_or_notice(game_id, raw_options, "disabled", plugin_dir)
    return _stream_or_notice(game_id, raw_options, "not_installed", plugin_dir)
