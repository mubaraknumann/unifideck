"""The Steam app-identity environment a Proton/umu process must carry.

Anything we start under Proton — a game, or a vendor client like Ubisoft
Connect — has to tell the runtime *which Steam app it is*. Without it umu
falls back to appid ``0`` (``GAMEID=umu-0``), and everything downstream that
keys off the identity goes to the wrong place:

* gamescope's WSI layer reports ``steam app id: 0`` for the window, so the
  Deck's Gaming Mode session never adopts it as the launched app's window —
  Steam's launch screen stays in front of a game that is already running and
  audible, with only *Abort* available;
* Fossilize writes the shader cache under the wrong appid;
* the window never associates with the shortcut for overlay/input purposes.

``SteamGameId``/``STEAM_COMPAT_APP_ID``/``SteamAppId`` carry the 32-bit appid;
``UMU_STEAM_GAME_ID`` carries Steam's 64-bit *gameID* encoding for a non-Steam
shortcut, ``(appid << 32) | 0x02000000``.

This module is the single implementation. It started life inside the Ubisoft
installer (as ``_build_steam_window_env``, written so the UPC window would
associate with its shortcut) while the game-launch path had no equivalent at
all — which is how games came to launch behind the loading screen. It lives
under ``unifideck.steam`` so both the in-process store code and the
out-of-process launcher can import it without either depending on the other.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: Low half of Steam's 64-bit shortcut gameID.
_SHORTCUT_GAMEID_FLAG = 0x02000000


def shortcut_game_id(app_id: int) -> int:
    """Steam's 64-bit gameID for the non-Steam shortcut ``app_id``.

    ``app_id`` must already be the *unsigned* 32-bit form — see
    :func:`unifideck.core.compat_bridge.to_unsigned`.
    """
    return (app_id << 32) | _SHORTCUT_GAMEID_FLAG


def build_steam_window_env(app_id: int | str | None, *, log_tag: str) -> dict[str, str]:
    """The four Steam-identity vars for ``app_id``, or all-zero if unknown.

    Accepts the appid in either signed or unsigned form (``games.map`` and
    ``shortcuts.vdf`` store it signed) and normalises before encoding — the
    signed form would produce a negative, meaningless gameID.

    Returning the explicit ``"0"`` block rather than an empty dict is
    deliberate: it overwrites any stale identity inherited from the parent
    environment instead of letting it leak into the child.
    """
    from unifideck.core.compat_bridge import to_unsigned

    unsigned = 0
    if app_id is not None:
        try:
            unsigned = to_unsigned(app_id)
        except (TypeError, ValueError):
            unsigned = 0
    if not unsigned:
        logger.info("[%s] Steam window env: no shortcut appid resolved, using 0", log_tag)
        return {
            "SteamGameId": "0",
            "STEAM_COMPAT_APP_ID": "0",
            "SteamAppId": "0",
            "UMU_STEAM_GAME_ID": "0",
        }
    logger.info("[%s] Steam window env: appid=%d", log_tag, unsigned)
    appid_str = str(unsigned)
    return {
        "SteamGameId": appid_str,
        "STEAM_COMPAT_APP_ID": appid_str,
        "SteamAppId": appid_str,
        "UMU_STEAM_GAME_ID": str(shortcut_game_id(unsigned)),
    }
