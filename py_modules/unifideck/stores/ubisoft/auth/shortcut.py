"""
Steam shortcut creation for the auth flow — ensures a UPC launcher exists.

``_AuthShortcut`` is responsible for creating (or re-using) a Steam
shortcut that launches UPC inside the auth-dedicated Wine prefix. The
shortcut is named "Ubisoft Connect", uses the UPC icon from SteamGridDB,
and is registered in Unifideck's shortcut registry with a stable
store_id (``ubisoft:upc-auth``) so it can be looked up later.

If a shortcut already exists in the registry, it's reused. If the
appid recorded in the registry doesn't match any actual Steam shortcut
(stale entry after the user reset Steam config), the entry is rebuilt
fresh.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from unifideck.core.compat_bridge import to_unsigned

if TYPE_CHECKING:
    from unifideck.services.shortcut import ShortcutService

    from .facade import UbisoftAuth
logger = logging.getLogger(__name__)
_AUTH_LAUNCH_OPTIONS_TEMPLATE = (
    "{store_id} "
    "UNIFIDECK_UBISOFT_ACTION=auth "
    "UNIFIDECK_UBISOFT_PREFIX_NAME={prefix_name}"
)
_AUTH_SHORTCUT_NAME = "Ubisoft Connect"
_LEGACY_AUTH_LAUNCH_OPTIONS = "ubisoft:.template"
_ORPHAN_SHORTCUT_NAMES = frozenset(
    {"upc.exe", "ubisoft connect"},
)

def _dropped_appids(shortcuts: dict[str, Any], keys: list[str]) -> list[int]:
    """Appids of *keys*, for the write guard's ``allow_foreign_drops``.

    Both prunes below deliberately target rows with an **empty** ``Exe``,
    which the ownership test therefore reads as the user's rather than
    ours. That is the right read — we genuinely cannot prove those rows
    are ours — so instead of loosening the test, the deletions are
    declared to the guard by appid.
    """
    ids = []
    for key in keys:
        appid = shortcuts[key].get("appid")
        if isinstance(appid, int):
            ids.append(appid)
    return ids

def _prune_orphan_shortcuts(shortcuts: dict[str, Any]) -> list[int]:
    """Prune orphan shortcuts; return the appids removed."""
    orphan_ids = [
        idx
        for idx, s in shortcuts.items()
        if s.get("AppName", "").lower() in _ORPHAN_SHORTCUT_NAMES
        and not (s.get("Exe") or s.get("exe") or "").strip('"')
        and not s.get("LaunchOptions", "")
    ]
    dropped = _dropped_appids(shortcuts, orphan_ids)
    for idx in orphan_ids:
        name = shortcuts[idx].get("AppName", "?")
        logger.info(
            "[UbisoftAuth] removing orphaned shortcut [%s] %r",
            idx,
            name,
        )
        del shortcuts[idx]
    return dropped

def _prune_legacy_template_shortcuts(
    shortcuts: dict[str, Any],
) -> list[int]:
    """Prune legacy template shortcuts; return the appids removed."""
    legacy_ids = [
        idx
        for idx, s in shortcuts.items()
        if s.get("LaunchOptions", "") == _LEGACY_AUTH_LAUNCH_OPTIONS
    ]
    dropped = _dropped_appids(shortcuts, legacy_ids)
    for idx in legacy_ids:
        logger.info(
            "[UbisoftAuth] removing legacy .template shortcut [%s]",
            idx,
        )
        del shortcuts[idx]
    return dropped

class _AuthShortcut:
    """Auth shortcut."""

    def __init__(self, parent: UbisoftAuth) -> None:
        """Initialize the instance."""
        self._parent = parent

    def get_launcher_path(self) -> str:
        """Get launcher path."""
        plugin_dir = self._parent._plugin_dir
        if not plugin_dir:
            plugin_dir = str(
                Path(__file__).resolve().parent.parent.parent.parent,
            )
        return str(
            Path(plugin_dir)
            / "bin"
            / "unifideck-launcher",
        )

    def build_auth_launch_options(self) -> str:
        """Build auth launch options."""
        return _AUTH_LAUNCH_OPTIONS_TEMPLATE.format(
            store_id=(self._parent._config.auth_shortcut_store_id),
            prefix_name=self._parent._config.auth_prefix_name,
        )

    async def ensure_auth_shortcut(self) -> int | None:
        """Ensure auth shortcut."""
        if self._parent._shortcut_service is None:
            logger.debug(
                "[UbisoftAuth] no shortcut_service; skipping auth shortcut creation",
            )
            return None
        try:
            sm = self._parent._shortcut_service
            store_id = self._parent._config.auth_shortcut_store_id
            existing_appid = await self.try_existing_shortcut(
                sm,
                store_id,
            )
            if existing_appid is not None:
                return existing_appid
            return await self.create_new_auth_shortcut(
                sm,
                store_id,
            )
        except Exception as e:
            logger.warning(
                "[UbisoftAuth] auth shortcut creation failed: %s",
                e,
            )
            return None

    async def try_existing_shortcut(
        self,
        sm: ShortcutService,
        store_id: str,
    ) -> int | None:
        """Try existing shortcut."""
        registry = await self._parent._load_registry(sm)
        if store_id not in registry:
            return None
        vdf_found = await self.validate_auth_shortcut(sm)
        if vdf_found:
            uid = registry[store_id].get("appid_unsigned")
            if uid:
                # Rewrite the shortcut to VDF so Steam re-discovers
                # it in its in-memory app store. After a plugin
                # reload Steam's app store is empty — RunGame
                # silently no-ops on shortcuts it doesn't know
                # about, even when they exist in shortcuts.vdf.
                appid = registry[store_id].get("appid")
                if appid:
                    await self.add_shortcut_to_vdf(sm, appid)
                await self._parent.fetch_auth_shortcut_artwork(
                    uid,
                )
                return cast("int | None", uid)
        entry = registry[store_id]
        appid = entry.get("appid")
        unsigned_id = entry.get("appid_unsigned")
        if not (appid and unsigned_id):
            return None
        logger.info(
            "[UbisoftAuth] recreating auth shortcut VDF from registry (appid=%d)",
            unsigned_id,
        )
        await self.add_shortcut_to_vdf(sm, appid)
        await self._parent._clear_compat(sm, appid)
        await self._parent.fetch_auth_shortcut_artwork(
            unsigned_id,
            force=True,
        )
        return cast("int | None", unsigned_id)

    async def create_new_auth_shortcut(
        self,
        sm: ShortcutService,
        store_id: str,
    ) -> int | None:
        """Create new auth shortcut."""
        launcher_path = self.get_launcher_path()
        appid = sm.generate_app_id(
            launcher_path,
            _AUTH_SHORTCUT_NAME,
        )
        unsigned_id = to_unsigned(appid)
        shortcuts_data = await sm.read_shortcuts()
        await self._reconcile_vdf(
            sm, shortcuts_data, launcher_path, appid, unsigned_id,
        )
        await self._finalize_new_shortcut(sm, appid, unsigned_id)
        return cast("int | None", unsigned_id)

    async def _reconcile_vdf(
        self,
        sm: ShortcutService,
        shortcuts_data: dict[str, Any],
        launcher_path: str,
        appid: int,
        unsigned_id: int,
    ) -> None:
        """Prune stale rows, add the canonical entry, write only if changed.

        Split out of :meth:`create_new_auth_shortcut` to keep that method
        under the fan-out cap, and because it is the one self-contained unit
        in it: everything here is about getting ``shortcuts.vdf`` into the
        right shape, and nothing else in the caller depends on its locals.
        """
        shortcuts = shortcuts_data.get("shortcuts", {})
        orphans_removed = _prune_orphan_shortcuts(shortcuts)
        legacy_removed = _prune_legacy_template_shortcuts(shortcuts)
        canonical_added = self._add_canonical_if_missing(
            shortcuts,
            launcher_path,
            appid,
            unsigned_id,
        )
        if not (orphans_removed or legacy_removed or canonical_added):
            return
        # Declare the pruned rows so the write guard lets them go —
        # see ``_dropped_appids``.
        await sm.write_shortcuts(
            shortcuts_data,
            allow_foreign_drops=frozenset(orphans_removed + legacy_removed),
        )
        logger.info(
            "[UbisoftAuth] VDF updated: orphans=%d legacy=%d added=%s",
            len(orphans_removed),
            len(legacy_removed),
            canonical_added,
        )

    async def _finalize_new_shortcut(
        self,
        sm: ShortcutService,
        appid: int,
        unsigned_id: int,
    ) -> None:
        """Finalize new shortcut."""
        await self._parent._register_shortcut(
            sm,
            appid,
            _AUTH_SHORTCUT_NAME,
        )
        await self._parent._cleanup_legacy_registry(sm)
        await self._parent._clear_compat(sm, appid)
        await self._parent.fetch_auth_shortcut_artwork(unsigned_id)

    def _add_canonical_if_missing(
        self,
        shortcuts: dict[str, Any],
        launcher_path: str,
        appid: int,
        unsigned_id: int,
    ) -> bool:
        """Add canonical if missing."""
        if self.shortcut_in_vdf(shortcuts):
            return False
        existing_indices = [int(k) for k in shortcuts if k.isdigit()]
        next_idx = max(existing_indices, default=-1) + 1
        shortcuts[str(next_idx)] = {
            "appid": appid,
            "AppName": _AUTH_SHORTCUT_NAME,
            "Exe": f'"{launcher_path}"',
            "StartDir": f'"{Path(launcher_path).parent}"',
            "LaunchOptions": self.build_auth_launch_options(),
            "IsHidden": 1,
            "AllowDesktopConfig": 1,
            "OpenVR": 0,
            "tags": {"0": "Ubisoft"},
        }
        logger.info(
            "[UbisoftAuth] created auth shortcut in VDF (appid=%d)",
            unsigned_id,
        )
        return True

    async def validate_auth_shortcut(self, sm: ShortcutService) -> bool:
        """Validate auth shortcut."""
        try:
            launcher_path = self.get_launcher_path()
            expected_launch_options = self.build_auth_launch_options()
            expected_appid = sm.generate_app_id(
                launcher_path,
                _AUTH_SHORTCUT_NAME,
            )
            shortcuts_data = await sm.read_shortcuts()
            shortcuts = shortcuts_data.get("shortcuts", {})
            vdf_updated = False
            found = False
            from unifideck.services.shortcut.write_guard import is_ours

            for _idx, s in shortcuts.items():
                full_id = self.extract_store_id(
                    s.get("LaunchOptions", ""),
                )
                if full_id != (self._parent._config.auth_shortcut_store_id):
                    continue
                # ``_fix_shortcut_fields`` rewrites Exe/StartDir/appid, so
                # matching on LaunchOptions alone would convert one of the
                # user's own shortcuts into our auth tile. ``is_ours`` is a
                # *basename* test, so the real repair case this exists for —
                # the plugin dir moved, leaving a stale absolute Exe — still
                # matches and still gets fixed.
                if not is_ours(s, launcher_path):
                    continue
                found = True
                if self._fix_shortcut_fields(
                    s,
                    launcher_path,
                    expected_launch_options,
                    expected_appid,
                ):
                    vdf_updated = True
                break
            if vdf_updated:
                await sm.write_shortcuts(shortcuts_data)
            if not found:
                logger.warning(
                    "[UbisoftAuth] auth shortcut not found in VDF during validation",
                )
                return False
            await self._parent._register_shortcut(
                sm,
                expected_appid,
                _AUTH_SHORTCUT_NAME,
            )
            await self._parent._clear_compat(
                sm,
                expected_appid,
            )
            return True
        except Exception as e:
            logger.warning(
                "[UbisoftAuth] auth shortcut validation failed: %s",
                e,
            )
            return True

    def _fix_shortcut_fields(
        self,
        entry: dict[str, Any],
        launcher_path: str,
        expected_launch_options: str,
        expected_appid: int,
    ) -> bool:
        """Fix shortcut fields."""
        changed = False
        if entry.get("LaunchOptions", "") != expected_launch_options:
            logger.info(
                "[UbisoftAuth] auth shortcut launch options outdated, fixing",
            )
            entry["LaunchOptions"] = expected_launch_options
            changed = True
        current_exe = (entry.get("Exe") or entry.get("exe") or "").strip('"')
        if current_exe != launcher_path:
            logger.info(
                "[UbisoftAuth] auth shortcut exe outdated, fixing",
            )
            entry["Exe"] = f'"{launcher_path}"'
            entry["StartDir"] = f'"{Path(launcher_path).parent}"'
            # Clean up the phantom lowercase key a pre-fix version of this
            # code could have written (Steam never reads it) so a stale
            # entry doesn't carry it forever.
            entry.pop("exe", None)
            changed = True
        if entry.get("appid") != expected_appid:
            logger.info(
                "[UbisoftAuth] auth shortcut appid changed, fixing",
            )
            entry["appid"] = expected_appid
            changed = True
        return changed

    async def auth_shortcut_exists_in_vdf(self) -> bool:
        """Auth shortcut exists in VDF."""
        if self._parent._shortcut_service is None:
            return True
        try:
            sm = self._parent._shortcut_service
            shortcuts_data = await sm.read_shortcuts()
            shortcuts = shortcuts_data.get("shortcuts", {})
            target = self._parent._config.auth_shortcut_store_id
            return any(
                self.extract_store_id(
                    s.get("LaunchOptions", ""),
                )
                == target
                for s in shortcuts.values()
            )
        except Exception:
            return True

    async def add_shortcut_to_vdf(
        self,
        sm: ShortcutService,
        appid: int,
    ) -> None:
        """Add shortcut to VDF."""
        launcher_path = self.get_launcher_path()
        launch_options = self.build_auth_launch_options()
        shortcuts_data = await sm.read_shortcuts()
        shortcuts = shortcuts_data.get("shortcuts", {})
        if self.shortcut_in_vdf(shortcuts):
            return
        existing_indices = [int(k) for k in shortcuts if k.isdigit()]
        next_idx = max(existing_indices, default=-1) + 1
        shortcuts[str(next_idx)] = {
            "appid": appid,
            "AppName": _AUTH_SHORTCUT_NAME,
            "Exe": f'"{launcher_path}"',
            "StartDir": f'"{Path(launcher_path).parent}"',
            "LaunchOptions": launch_options,
            "IsHidden": 1,
            "AllowDesktopConfig": 1,
            "OpenVR": 0,
            "tags": {"0": "Ubisoft"},
        }
        await sm.write_shortcuts(shortcuts_data)

    def shortcut_in_vdf(
        self,
        shortcuts: dict[str, Any],
    ) -> bool:
        """Shortcut in VDF."""
        target = self._parent._config.auth_shortcut_store_id
        for s in shortcuts.values():
            full_id = self.extract_store_id(
                s.get("LaunchOptions", ""),
            )
            if full_id == target:
                return True
        return False

    @staticmethod
    def extract_store_id(launch_options: str) -> str:
        """Extract the canonical ``"<store>:<id>"`` token from LaunchOptions.

        Delegates to the shared, wrapper-tolerant regex matcher
        (``services.shortcut.launch_options.get_full_id``) instead of
        assuming the store id is the FIRST whitespace token — a wrapper
        prefix (user-edited, or written by a third-party plugin such as
        decky-proton-launch: ``<wrapper> %command% ubisoft:<id>``) pushes
        the store id token past position 0, which made this auth shortcut
        invisible to VDF validation/lookup and could spawn a duplicate.
        """
        from unifideck.services.shortcut.launch_options import get_full_id

        return get_full_id(launch_options) or ""
