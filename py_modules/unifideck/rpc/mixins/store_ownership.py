"""StoreOwnershipRPCMixin: the Steam Store "already owned elsewhere" ribbon.

One RPC, called by ``src/lib/steam-bridge/store-ownership-ribbon.ts`` each
time the Gaming Mode store starts and finishes loading an app page. It answers "does
the user already have this game on another store?" from the live library
and, only when the answer is yes, draws the ribbon into the store page over
CDP. Every other store page costs one in-memory join and no CDP traffic.
"""
from __future__ import annotations

import logging
from typing import Any

from unifideck.cdp.store_ribbon import RibbonInjectOutcome, inject_store_ribbon
from unifideck.core.cross_store_ownership import OwnedCopy, find_owned_copies
from unifideck.core.game_grouping import load_owned_steam_apps
from unifideck.rpc.mixins._store_ownership_payload import (
    RibbonStrings,
    build_ribbon_payload,
    parse_steam_app_id,
    read_steam_name,
    sanitize_ribbon_strings,
)
from unifideck.utils.config_helpers import get_cfg

logger = logging.getLogger(__name__)


class StoreOwnershipRPCMixin:
    """Steam Store ownership ribbon (cheap join; CDP only when owned)."""

    cache: Any
    config: Any
    services: Any
    sync_service: Any

    async def show_store_ownership(
        self, steam_app_id: Any, strings: Any = None,
    ) -> dict[str, Any]:
        """Draw the ribbon on the store page of *steam_app_id* if it is owned.

        Args:
            steam_app_id: the real Steam AppID parsed from the store URL.
            strings: translated ribbon text from the frontend
                (``tag_owned``, ``tag_cloud``, ``message_owned``,
                ``message_cloud``, ``installed``, ``via``, ``dir``,
                ``store_labels``).

        Returns:
            ``{"shown", "reason", "stores"}``. ``reason`` is ``bad_appid``,
            ``not_owned`` (returned before any CDP work), ``bad_strings``,
            or the draw outcome: ``shown``, ``no_target``,
            ``stale_document``, ``cdp_unavailable``, ``eval_failed``.
            Never raises.
        """
        try:
            appid = parse_steam_app_id(steam_app_id)
            if appid is None:
                return _result(shown=False, reason="bad_appid")
            copies = self._owned_copies(appid)
            if not copies:
                return _result(shown=False, reason="not_owned")
            ribbon_strings = sanitize_ribbon_strings(strings)
            if ribbon_strings is None:
                logger.warning(
                    "[StoreOwnership] ribbon strings missing or invalid; "
                    "not drawing the ribbon for Steam app %d", appid,
                )
                return _result(shown=False, reason="bad_strings", copies=copies)
            outcome = await self._draw(appid, copies, ribbon_strings)
            return _result(shown=outcome.shown, reason=outcome.reason, copies=copies)
        except Exception as exc:  # an RPC on every store page must never raise
            logger.warning("[StoreOwnership] show_store_ownership failed: %s", exc)
            return _result(shown=False, reason="error")

    def _owned_copies(self, appid: int) -> list[OwnedCopy]:
        """Non-Steam copies of *appid* in the live library; [] when unavailable."""
        sync_service = getattr(self, "sync_service", None)
        if sync_service is None:
            return []
        try:
            games = sync_service.get_all_games()
        except Exception as exc:
            logger.warning("[StoreOwnership] get_all_games failed: %s", exc)
            return []
        cache = getattr(self, "cache", None)
        return find_owned_copies(
            games, cache, appid, self._purchase_indexes(),
            owned_steam=load_owned_steam_apps(getattr(self, "config", None)),
            steam_name=read_steam_name(cache, appid),
        )

    def _purchase_indexes(self) -> dict[str, Any]:
        """Authenticated purchase lists (Xbox), and a nudge if they are stale.

        A missing or broken ownership service means today's behaviour: the
        Xbox line is the neutral CLOUD one, never OWNED.
        """
        service = getattr(getattr(self, "services", None), "microsoft_ownership", None)
        if service is None:
            return {}
        try:
            indexes = service.purchase_indexes()
            service.nudge()
        except Exception as exc:
            logger.warning("[StoreOwnership] purchase index unavailable: %s", exc)
            return {}
        return indexes if isinstance(indexes, dict) else {}

    async def _draw(
        self, appid: int, copies: list[OwnedCopy], strings: RibbonStrings,
    ) -> RibbonInjectOutcome:
        """Build the payload and draw it into the matching store page(s)."""
        cache = getattr(self, "cache", None)
        payload = build_ribbon_payload(appid, copies, strings, read_steam_name(cache, appid))
        port = int(get_cfg(getattr(self, "config", None), "cdp.port", 8080))
        outcome = await inject_store_ribbon(port, appid, payload)
        logger.info(
            "[StoreOwnership] Steam app %d owned on %s: %s (%d page(s))",
            appid, ",".join(c.store for c in copies), outcome.reason, outcome.targets,
        )
        return outcome


def _result(
    *, shown: bool, reason: str, copies: list[OwnedCopy] | None = None,
) -> dict[str, Any]:
    return {
        "shown": shown,
        "reason": reason,
        "stores": [c.store for c in copies or ()],
    }
