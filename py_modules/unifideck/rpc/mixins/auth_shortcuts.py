"""AuthShortcutsRPCMixin — per-store auth-shortcut context RPCs.

Each registered store has a dedicated Steam shortcut that the
frontend launches via ``SteamClient.Apps.RunGame`` to drive the
auth handshake (browser inside a Wine prefix, CLI in a temp
prefix, etc.). The frontend launchers in
``utils/authShortcutLaunch.ts`` need :

* the **appid** of the persistent shortcut (so they can find
  it in Steam's in-memory state), and
* the **launcher path** to fall back to when the persistent
  shortcut isn't loaded yet (so they can create a temp
  shortcut against the actual ``bin/unifideck-launcher``
  wrapper).

This mixin returns that metadata for each store. The
deterministic appid mirrors what ``ShortcutService.add_auth_shortcut``
wrote into ``shortcuts.vdf`` so both sides agree.

Ubisoft has its own VDF-scan + repair logic in
``UbisoftStore.get_auth_shortcut_context`` ; we just proxy.

``get_compat_tool_for_game`` also lives here. It **reads** the
shortcut's Force-Compatibility tool for the wrapper-store launch
path (``launchWrapperViaShortcut``). It used to be half of a
capture-and-clear dance on the game-details page: the frontend
saved the tool to ``proton_settings.json`` via a
``save_proton_setting`` RPC, then cleared Force Compatibility so
``RunGame`` wouldn't wrap our launcher in Proton. That flow was
deliberately removed — clearing the selection meant the launcher
only ever saw a stale copy, so switching Proton in Steam's dialog
appeared to work or not purely by timing. ``config.vdf``'s
CompatToolMapping is now the single source of truth, read directly
by ``selector.select_proton_version``, and the double-Proton
problem is handled at the umu spawn point by
``container_escape``. The ``save_proton_setting`` RPC was left
with no caller and deleted in the audit §1.2 pass;
``proton_helpers.save_proton_setting`` itself stays live, written
by the launcher's own ``prefix_setup`` and ``ge_fallback``.

Lives in its own file (split from ``StoreRPCMixin``) so each
mixin honours the 200 LOC ceiling.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from unifideck.core.compat_bridge import to_unsigned

logger = logging.getLogger(__name__)

# Fallback plugin dir when ``DECKY_PLUGIN_DIR`` is somehow unset (Decky always
# sets it; this is belt-and-braces). Decky installs to ``$HOME/homebrew`` on
# every distro, so derive it from ``~`` — never bake in the ``deck`` username.
# Computed once at import so async callers don't do path I/O inline (ASYNC240).
_FALLBACK_PLUGIN_DIR = os.path.expanduser("~/homebrew/plugins/Unifideck")

# Default delay used when the frontend has to wait for Steam to
# load the freshly-created shortcut into in-memory state. Lifted
# from the legacy ``waitForShortcut`` timeout in
# ``authShortcutLaunch.ts``.
_AUTH_SHORTCUT_LAUNCH_WAIT_MS = 5000

# Per-store metadata used to compute the auth-shortcut context.
# The title must match what the store object passes to
# ``add_auth_shortcut`` in ``_ensure_auth_shortcut`` so
# ``generate_app_id`` returns the same id on both sides.
_AUTH_SHORTCUT_META: dict[str, dict[str, str]] = {
    "epic":      {"title": "Epic Games Sign-In",   "env": "UNIFIDECK_EPIC_ACTION"},
    "gog":       {"title": "GOG Sign-In",          "env": "UNIFIDECK_GOG_ACTION"},
    "amazon":    {"title": "Amazon Games Sign-In", "env": "UNIFIDECK_AMAZON_ACTION"},
    "microsoft": {"title": "Microsoft Sign-In",    "env": "UNIFIDECK_MICROSOFT_ACTION"},
}

class AuthShortcutsRPCMixin:
    """Per-store auth-shortcut context + compat-tool lookup."""

    registry: Any

    async def get_epic_auth_shortcut_context(self) -> Any:
        """Auth-shortcut context for the Epic Games launcher."""
        return _build_and_log("epic")

    async def get_gog_auth_shortcut_context(self) -> Any:
        """Auth-shortcut context for the GOG launcher."""
        return _build_and_log("gog")

    async def get_amazon_auth_shortcut_context(self) -> Any:
        """Auth-shortcut context for the Amazon Games launcher."""
        return _build_and_log("amazon")

    async def get_microsoft_auth_shortcut_context(self) -> Any:
        """Auth-shortcut context for the Microsoft / xCloud launcher."""
        return _build_and_log("microsoft")

    async def _wrapper_auth_shortcut_context(self, store_id: str) -> Any:
        """Auth-shortcut context for any wrapper store.

        Wrapper stores (Ubisoft Connect, Battle.net, EA App next) sign in
        through a vendor client launched from a Steam shortcut, and each
        store owns its own VDF scan + repair. This resolves the store and
        delegates; it is shared because the delegation is identical and a
        per-store copy would drift.

        Returns a structured error rather than raising when the store is
        absent (test installs, partial deployments) so the frontend can
        fall back to a temporary shortcut.
        """
        tag = f"[AuthShortcuts:{store_id}]"
        logger.info("%s context requested", tag)
        # ``get`` RAISES KeyError for an unregistered store; ``get_store``
        # is the None-returning variant this function's contract needs.
        store = self.registry.get_store(store_id)
        if store is None:
            logger.warning("%s store not registered", tag)
            return {"success": False, "error": "store_not_found"}
        if not hasattr(store, "get_auth_shortcut_context"):
            logger.warning("%s store lacks get_auth_shortcut_context", tag)
            return {"success": False, "error": "auth_shortcut_not_supported"}
        result = await store.get_auth_shortcut_context()
        logger.info(
            "%s context resolved: success=%s appid=%s",
            tag,
            result.get("success"),
            result.get("appid_unsigned"),
        )
        return result

    async def get_ubisoft_auth_shortcut_context(self) -> Any:
        """Auth-shortcut context for Ubisoft Connect.

        A thin named route: the frontend addresses RPC methods by name, so
        each wrapper store needs its own entry point even though the body
        is shared.
        """
        return await self._wrapper_auth_shortcut_context("ubisoft")

    async def get_battlenet_auth_shortcut_context(self) -> Any:
        """Auth-shortcut context for the Battle.net client."""
        return await self._wrapper_auth_shortcut_context("battlenet")

    async def get_compat_tool_for_game(self, store_game_id: str) -> Any:
        """See full doc on the body — logging-wrapped variant."""
        logger.info(
            "[AuthShortcuts] get_compat_tool_for_game(%s)",
            store_game_id,
        )
        result = await self._get_compat_tool_impl(store_game_id)
        logger.info(
            "[AuthShortcuts] compat_tool result: success=%s",
            result.get("success") if isinstance(result, dict) else "?",
        )
        return result

    async def _get_compat_tool_impl(self, store_game_id: str) -> Any:
        """Return the Steam Force-Compatibility tool for a game shortcut.

        Reads the Proton tool currently set as Force Compatibility
        (``config.vdf`` CompatToolMapping) for the shortcut, resolved
        via its appid scanned from ``shortcuts.vdf``.

        Consumed by ``launchWrapperViaShortcut`` (the Ubisoft /
        Battle.net launch path), which needs the shortcut's
        ``appid_unsigned`` and current launch options before it can
        temporarily rewrite them. ``is_linux_runtime`` marks
        Steam-Linux-Runtime entries, which are not real Proton.

        NOT a capture-and-clear helper — see this module's docstring.
        """
        try:
            from unifideck.compatibility.proton_helpers import (
                get_compat_tool_for_app,
                get_global_default_compat_tool,
                is_linux_runtime,
            )

            appid_unsigned, launch_options = self._resolve_shortcut_entry(
                store_game_id,
            )
            tool_name = (
                get_compat_tool_for_app(appid_unsigned)
                if appid_unsigned
                else ""
            )
            # A tool equal to Steam's global default (CompatToolMapping["0"])
            # is a distro/system default (e.g. Bazzite's "Proton-CachyOS
            # Latest") applied to every shortcut — NOT an explicit per-game
            # choice. The frontend must not adopt it as a per-game override
            # (it would be unresolvable and thrash the prefix); it only saves
            # a tool that differs from the global default.
            global_default = get_global_default_compat_tool()
            is_global_default = bool(tool_name) and tool_name == global_default
            logger.info(
                "[AuthShortcuts] compat tool for %s: appid=%s tool=%r "
                "global_default=%r is_global_default=%s",
                store_game_id, appid_unsigned, tool_name,
                global_default, is_global_default,
            )
            plugin_dir = os.environ.get(
                "DECKY_PLUGIN_DIR", _FALLBACK_PLUGIN_DIR,
            )
            launcher_path = str(
                Path(plugin_dir) / "bin" / "unifideck-launcher",
            )
            # ``appid_unsigned`` + ``current_launch_options`` let the
            # frontend RunGame this shortcut directly (Ubisoft install
            # flow) without a separate context RPC: it injects the
            # ``install`` action into the options, launches, then restores
            # ``current_launch_options`` so a later Play still works.
            # ``launcher_path`` is the fallback exe used when the shortcut
            # isn't yet in Steam's in-memory app store (first session after
            # a backend VDF write) — the frontend then creates a temporary
            # shortcut via ``AddShortcut`` instead of RunGame-ing an
            # unregistered appid ("Game configuration unavailable").
            return {
                "success": True,
                "tool_name": tool_name,
                "is_linux_runtime": is_linux_runtime(tool_name),
                "is_global_default": is_global_default,
                "appid_unsigned": appid_unsigned,
                "current_launch_options": launch_options,
                "store_game_id": store_game_id,
                "launcher_path": launcher_path,
            }
        except Exception as e:
            logger.warning(
                "[AuthShortcutsRPCMixin] "
                "get_compat_tool_for_game(%s) failed: %s",
                store_game_id, e,
            )
            return {"success": False, "error": str(e)}

    @staticmethod
    def _resolve_shortcut_appid(store_game_id: str) -> int:
        """Scan shortcuts.vdf for an entry whose LaunchOptions
        contains ``store_game_id`` and return its AppID."""
        return AuthShortcutsRPCMixin._resolve_shortcut_entry(store_game_id)[0]

    @staticmethod
    def _resolve_shortcut_entry(store_game_id: str) -> tuple[int, str]:
        """Find the shortcut whose ``LaunchOptions`` first token equals
        ``store_game_id`` and return ``(appid_unsigned, launch_options)``.

        ``launch_options`` is the shortcut's current ``LaunchOptions``
        string (e.g. ``"ubisoft:<game_id>"`` plus any user wrappers) — the
        Ubisoft install flow needs it so it can temporarily inject the
        ``install`` action and then restore the real options afterwards
        (restoring to ``""`` would wipe the shortcut and break Play).
        Returns ``(0, "")`` when no matching shortcut exists.

        Parses ``shortcuts.vdf`` with the ``vdf`` library and reads each
        entry's ``appid`` directly. The previous implementation byte-scanned
        a fixed 200-byte window *backwards* from the ``LaunchOptions`` tag
        for the appid — but for Ubisoft per-game shortcuts the AppName +
        launcher Exe + icon path push the appid 209-217 bytes away, outside
        that window, so it returned 0 and ``get_compat_tool_for_game`` (the
        Ubisoft install/launch path) found no appid → "Context unavailable",
        no RunGame, UPC never opened (and Play broke for installed titles).
        It also matched ``store_game_id`` as a substring
        (``ubisoft:4`` ⊂ ``ubisoft:46``) and returned the *first* (not the
        nearest) appid in the window.

        Matches ``store_game_id`` via ``get_full_id`` (regex, word-boundary
        anchored) rather than requiring it to be the FIRST token of
        ``LaunchOptions``: a wrapper prefix (a user's own edit, or a
        third-party plugin like decky-proton-launch writing
        ``<wrapper> %command% ubisoft:<id>``, both observed in the wild via
        tester diagnostic bundles) pushes the store id token past position
        0, and RunGame silently never fired because this returned 0.
        """
        from pathlib import Path

        import vdf

        from unifideck.services.shortcut.launch_options import get_full_id
        from unifideck.steam.library import find_steam_path
        from unifideck.steam.steam_user import get_active_steam_user
        # Probe the cross-distro candidate roots (no config here — this is a
        # staticmethod) instead of hardcoding ``~/.steam/steam``, so the
        # Ubisoft install/Play appid lookup reads the SAME shortcuts.vdf the
        # reconcile wrote on Bazzite/CachyOS/Flatpak layouts. ``get_active_steam_user``
        # is the shim over ``current_user.resolve``, which reads the persisted
        # ``steam.active_user`` (frontend-confirmed) config-free from disk — so this
        # resolves the SAME user reconcile targeted, even without a ConfigManager.
        resolved = find_steam_path(None)
        steam_root = Path(resolved) if resolved else Path("~/.steam/steam").expanduser()
        active_user = get_active_steam_user(steam_root) or "0"
        vdf_path = (
            steam_root / "userdata" / active_user / "config" / "shortcuts.vdf"
        )
        if not vdf_path.is_file():
            return 0, ""
        try:
            data = vdf.binary_loads(vdf_path.read_bytes())  # type: ignore[no-untyped-call]  # vdf.binary_loads is untyped
        except Exception:
            logger.warning("[AuthShortcuts] failed to parse shortcuts.vdf")
            return 0, ""
        shortcuts = data.get("shortcuts", {})
        entries = shortcuts.values() if isinstance(shortcuts, dict) else []
        for entry in entries:
            launch_options = entry.get("LaunchOptions", "") or ""
            if get_full_id(launch_options) != store_game_id:
                continue
            appid = entry.get("appid")
            if appid is None:
                continue
            unsigned = to_unsigned(appid)
            return int(unsigned), launch_options
        return 0, ""

# ─── Module-level helpers ─────────────────────────────────────

def _build_and_log(store: str) -> dict[str, Any]:
    """Compute + log the auth-shortcut context for ``store``.

    Wraps :func:`_build_auth_shortcut_context` with INFO-level
    log lines on entry and exit so the user can correlate a
    frontend connect click with a backend response without
    attaching a debugger.
    """
    logger.info("[AuthShortcuts:%s] context requested", store)
    result = _build_auth_shortcut_context(store)
    if result.get("success"):
        logger.info(
            "[AuthShortcuts:%s] context resolved: appid=%s "
            "launcher=%s",
            store,
            result.get("appid_unsigned"),
            result.get("launcher_path"),
        )
    else:
        logger.warning(
            "[AuthShortcuts:%s] context failed: %s",
            store,
            result.get("error"),
        )
    return result

def _build_auth_shortcut_context(store: str) -> dict[str, Any]:
    """Compute the auth-shortcut context for a CLI-driven store.

    Mirrors what the per-store ``_ensure_auth_shortcut`` method
    passes to ``ShortcutService.add_auth_shortcut`` so the
    appid we return matches what the backend actually wrote to
    ``shortcuts.vdf``.
    """
    meta = _AUTH_SHORTCUT_META.get(store)
    if meta is None:
        return {"success": False, "error": "unknown_store"}
    plugin_dir = os.environ.get(
        "DECKY_PLUGIN_DIR", _FALLBACK_PLUGIN_DIR,
    )
    wrapper_path = str(Path(plugin_dir) / "bin" / "unifideck-launcher")
    try:
        from unifideck.services.shortcut.games_map import (
            generate_app_id,
        )
        app_id = generate_app_id(wrapper_path, meta["title"])
    except Exception as e:
        logger.warning(
            "[AuthShortcutsRPCMixin] generate_app_id failed "
            "for %s: %s",
            store, e,
        )
        return {"success": False, "error": "appid_failed"}
    unsigned = to_unsigned(app_id)
    return {
        "success": True,
        "appid_unsigned": unsigned,
        "launcher_path": wrapper_path,
        "launch_options": (
            f"{store}:{'ms' if store == 'microsoft' else store}-auth {meta['env']}=auth"
        ),
        "launch_wait_ms": _AUTH_SHORTCUT_LAUNCH_WAIT_MS,
    }
