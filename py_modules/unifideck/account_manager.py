"""
Account Manager for Unifideck

Detects Steam account switches and provides migration/cleanup options.
When a different account logs in, offers to:
  a) Migrate shortcuts + artwork from the previous account
  b) Clear store auth tokens for a fresh start
  c) Skip (do nothing)

Guest users are handled the same as regular accounts.
"""

import os
import json
import shutil
import logging
from typing import Dict, Any, Optional

try:
    import vdf
    VDF_AVAILABLE = True
except ImportError:
    VDF_AVAILABLE = False

from .steam_utils import get_logged_in_steam_user, _find_steam_path

logger = logging.getLogger(__name__)

SETTINGS_PATH = os.path.expanduser("~/.local/share/unifideck/settings.json")

# Auth token file locations (shared across all Steam accounts)
AUTH_TOKEN_PATHS = {
    'epic': os.path.expanduser("~/.config/legendary/user.json"),
    'gog': os.path.expanduser("~/.config/unifideck/gog_token.json"),
    'gogdl': os.path.expanduser("~/.config/unifideck/gogdl/auth.json"),
    'amazon': os.path.expanduser("~/.config/nile/user.json"),
}

# Shared Unifideck data files
REGISTRY_PATH = os.path.expanduser("~/.local/share/unifideck/shortcuts_registry.json")
GAMES_MAP_PATH = os.path.expanduser("~/.local/share/unifideck/games.map")


class AccountManager:
    """Manages account switch detection, migration, and auth cleanup."""

    def __init__(self):
        self.steam_path = _find_steam_path()
        self.account_switch_detected = False
        self.previous_user_id: Optional[str] = None
        self.current_user_id: Optional[str] = None

    def detect_account_switch(self) -> bool:
        """Compare current user to last_known_user_id in settings.json.

        Returns True if a switch was detected AND there's data to act on
        (auth tokens exist OR shortcuts registry has entries).
        Guest users are treated the same as regular accounts.
        """
        self.current_user_id = get_logged_in_steam_user(self.steam_path)
        if not self.current_user_id:
            logger.warning("[AccountSwitch] Could not detect current Steam user")
            return False

        last_known = self._load_last_known_user()

        if last_known is None:
            # First install or settings cleared — no switch
            logger.info(f"[AccountSwitch] First run, recording user {self.current_user_id}")
            self.account_switch_detected = False
            return False

        if last_known == self.current_user_id:
            # Same user — no switch
            logger.debug(f"[AccountSwitch] Same user {self.current_user_id}, no switch")
            self.account_switch_detected = False
            return False

        # Different user detected
        self.previous_user_id = last_known
        logger.info(
            f"[AccountSwitch] Account switch detected: {last_known} -> {self.current_user_id}"
        )

        # Only flag if there's something to act on
        if self.has_active_auth_tokens() or self.has_registry_entries():
            self.account_switch_detected = True
            return True

        # User logged out of all stores and has no registry — nothing to do
        logger.info("[AccountSwitch] Switch detected but no auth tokens or registry entries — skipping modal")
        self.account_switch_detected = False
        return False

    def should_show_modal(self) -> bool:
        """True if account switch was detected and there's actionable data."""
        return self.account_switch_detected

    def has_active_auth_tokens(self) -> bool:
        """Check if any store auth token files exist on disk."""
        for store, path in AUTH_TOKEN_PATHS.items():
            if os.path.exists(path):
                logger.debug(f"[AccountSwitch] Found auth token for {store}: {path}")
                return True
        return False

    def has_registry_entries(self) -> bool:
        """Check if shortcuts_registry.json has any entries."""
        try:
            if os.path.exists(REGISTRY_PATH):
                with open(REGISTRY_PATH, 'r') as f:
                    registry = json.load(f)
                return len(registry) > 0
        except Exception as e:
            logger.error(f"[AccountSwitch] Error reading registry: {e}")
        return False

    def save_current_user(self):
        """Write current_user_id to settings.json as last_known_user_id."""
        if not self.current_user_id:
            return
        try:
            settings_dir = os.path.dirname(SETTINGS_PATH)
            os.makedirs(settings_dir, exist_ok=True)

            settings = {}
            if os.path.exists(SETTINGS_PATH):
                with open(SETTINGS_PATH, 'r') as f:
                    settings = json.load(f)

            settings['last_known_user_id'] = self.current_user_id

            with open(SETTINGS_PATH, 'w') as f:
                json.dump(settings, f, indent=2)

            logger.info(f"[AccountSwitch] Saved current user {self.current_user_id} to settings")
        except Exception as e:
            logger.error(f"[AccountSwitch] Error saving current user: {e}")

    def reconcile_shortcuts_from_registry(self, shortcuts_manager) -> Dict[str, Any]:
        """Create shortcuts in the new user's shortcuts.vdf from the shared registry.

        Uses shortcuts_registry.json (shared) to recreate shortcuts with
        their original appids (preserves artwork mapping). Then copies
        artwork from the previous user's grid folder.

        Args:
            shortcuts_manager: The ShortcutsManager instance from main.py

        Returns:
            dict: {'created': int, 'errors': list}
        """
        result = {'created': 0, 'errors': []}

        if not self.current_user_id or not self.steam_path:
            result['errors'].append("No current user or steam path")
            return result

        try:
            # Use the existing reconcile method which reads games.map + shortcuts_registry
            reconcile = shortcuts_manager.reconcile_shortcuts_from_games_map()
            result['created'] = reconcile.get('created', 0)
            result['errors'] = reconcile.get('errors', [])

            logger.info(f"[AccountSwitch] Reconciled {result['created']} shortcuts for user {self.current_user_id}")
        except Exception as e:
            logger.error(f"[AccountSwitch] Error reconciling shortcuts: {e}")
            result['errors'].append(str(e))

        return result

    def migrate_artwork(self) -> Dict[str, Any]:
        """Copy grid artwork from the previous user's folder to the current user.

        Returns:
            dict: {'copied': int, 'errors': list}
        """
        result = {'copied': 0, 'errors': []}

        if not self.previous_user_id or not self.current_user_id or not self.steam_path:
            result['errors'].append("Missing user IDs or steam path")
            return result

        source_grid = os.path.join(
            self.steam_path, "userdata", self.previous_user_id, "config", "grid"
        )
        target_grid = os.path.join(
            self.steam_path, "userdata", self.current_user_id, "config", "grid"
        )

        if not os.path.isdir(source_grid):
            logger.info(f"[AccountSwitch] No artwork folder for previous user {self.previous_user_id}")
            return result

        os.makedirs(target_grid, exist_ok=True)

        try:
            for filename in os.listdir(source_grid):
                source_file = os.path.join(source_grid, filename)
                target_file = os.path.join(target_grid, filename)

                if not os.path.isfile(source_file):
                    continue

                # Don't overwrite existing artwork
                if os.path.exists(target_file):
                    continue

                try:
                    shutil.copy2(source_file, target_file)
                    result['copied'] += 1
                except Exception as e:
                    result['errors'].append(f"Failed to copy {filename}: {e}")

            logger.info(f"[AccountSwitch] Copied {result['copied']} artwork files from user {self.previous_user_id} to {self.current_user_id}")
        except Exception as e:
            logger.error(f"[AccountSwitch] Error migrating artwork: {e}")
            result['errors'].append(str(e))

        return result

    def clear_all_auth_tokens(self) -> Dict[str, Any]:
        """Delete all store auth tokens and clear shared registry/games.map.

        Returns:
            dict: {'deleted_tokens': list, 'cleared_files': list, 'errors': list}
        """
        result = {'deleted_tokens': [], 'cleared_files': [], 'errors': []}

        # Delete auth token files
        for store, path in AUTH_TOKEN_PATHS.items():
            if os.path.exists(path):
                try:
                    os.remove(path)
                    result['deleted_tokens'].append(store)
                    logger.info(f"[AccountSwitch] Deleted {store} auth token: {path}")
                except Exception as e:
                    result['errors'].append(f"Failed to delete {store} token: {e}")

        # Clear shortcuts registry (shared)
        if os.path.exists(REGISTRY_PATH):
            try:
                os.remove(REGISTRY_PATH)
                result['cleared_files'].append('shortcuts_registry.json')
                logger.info("[AccountSwitch] Cleared shortcuts registry")
            except Exception as e:
                result['errors'].append(f"Failed to clear registry: {e}")

        # Clear games.map (shared)
        if os.path.exists(GAMES_MAP_PATH):
            try:
                os.remove(GAMES_MAP_PATH)
                result['cleared_files'].append('games.map')
                logger.info("[AccountSwitch] Cleared games.map")
            except Exception as e:
                result['errors'].append(f"Failed to clear games.map: {e}")

        logger.info(
            f"[AccountSwitch] Auth cleanup: {len(result['deleted_tokens'])} tokens deleted, "
            f"{len(result['cleared_files'])} files cleared"
        )
        return result

    # --- Private helpers ---

    def _load_last_known_user(self) -> Optional[str]:
        """Read last_known_user_id from settings.json. Returns None if not set."""
        try:
            if os.path.exists(SETTINGS_PATH):
                with open(SETTINGS_PATH, 'r') as f:
                    settings = json.load(f)
                return settings.get('last_known_user_id')
        except Exception as e:
            logger.error(f"[AccountSwitch] Error reading settings: {e}")
        return None
