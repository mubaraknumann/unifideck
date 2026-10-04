"""Steam account-switch detector + data migrator.

Ported from ``staging:py_modules/unifideck/accounts/account_manager.py``.
On the new (mixin) architecture the runtime ``AccountService``
([services/account_service.py]) handles *live* switch detection (polls
``loginusers.vdf``, emits ``ACCOUNT_SWITCHED``); this class handles the
*startup* modal flow the frontend drives via ``check_account_switch`` /
``migrate_account_data``: it compares the active Steam user against the
``last_known_user_id`` persisted in ``settings.json`` and offers to
migrate shortcuts + artwork from the previous account.

Two adaptations from the staging port:

* Steam helpers — staging's ``get_logged_in_steam_user`` / ``_find_steam_path``
  became :func:`unifideck.steam.steam_user.get_active_steam_user`
  (takes a ``Path``) and :func:`unifideck.steam.library.find_steam_path`.
* Shortcut reconciliation — staging called a synchronous
  ``shortcuts_manager.reconcile_shortcuts_from_games_map()``. The new
  ``ShortcutService.reconcile(games)`` is async and game-list driven, so
  the RPC mixin drives it directly; this class only owns artwork
  migration (pure filesystem) plus the detection/gating helpers.
"""
from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any

from unifideck.steam.library import find_steam_path
from unifideck.steam.steam_user import get_active_steam_user

logger = logging.getLogger(__name__)

SETTINGS_PATH = Path.home() / ".local/share/unifideck/settings.json"

# Auth token file locations (shared across all Steam accounts), relative to
# the user's home. Existence of any one means there's a signed-in store worth
# migrating/clearing.
#
# Relative fragments rather than resolved paths, because a module-level
# ``Path.home()`` is captured at import — before pytest's autouse fixture
# redirects ``HOME`` — and a test then reads (or writes) the developer's real
# credentials. That has already happened once here: a unit test for the nile
# self-heal renamed the live ``~/.config/nile/user.json`` and signed the
# machine out of Amazon. :func:`auth_token_paths` resolves them per call.
_AUTH_TOKEN_FRAGMENTS: dict[str, str] = {
    "epic": ".config/legendary/user.json",
    "gog": ".config/unifideck/gog_token.json",
    "gogdl": ".config/unifideck/gogdl_auth.json",
    "amazon": ".config/nile/user.json",
    "amazon_library": ".config/nile/library.json",
    "amazon_installed": ".config/nile/installed.json",
    "ubisoft": ".local/share/unifideck/ubisoft_token.json",
    "ubisoft_session": ".local/share/unifideck/ubisoft_upc_session.txt",
    "microsoft": ".config/unifideck/microsoft_tokens.json",
    # Battle.net keeps no token file of ours — its credential lives inside
    # the auth prefix, which is also where installed games live, so the
    # prefix is deliberately NOT listed here (clearing it would delete the
    # user's games). The id map is the account-scoped state worth clearing:
    # it holds which uid installed to which prefix for the previous account.
    "battlenet": ".local/share/unifideck/battlenet_id_map.json",
}


def auth_token_paths() -> dict[str, Path]:
    """Every store's auth-token path, resolved against the current home."""
    home = Path.home()
    return {store: home / frag for store, frag in _AUTH_TOKEN_FRAGMENTS.items()}


def registry_path() -> Path:
    """Shared Unifideck shortcut registry (same file as
    ``services/shortcut/registry.py`` ``DEFAULT_REGISTRY_PATH``)."""
    return Path.home() / ".local/share/unifideck/shortcuts_registry.json"


class AccountManager:
    """Manages account-switch detection + migration for the modal flow."""

    def __init__(self) -> None:
        """Resolve the Steam path; init empty switch state."""
        self.steam_path: str | None = find_steam_path()
        self.account_switch_detected = False
        self.previous_user_id: str | None = None
        self.current_user_id: str | None = None

    def detect_account_switch(self) -> bool:
        """Compare the active user to ``last_known_user_id`` in settings.

        Returns ``True`` only when a switch is detected **and** there's
        data to act on (auth tokens exist or the shortcuts registry has
        entries). Guest users are treated like regular accounts.
        """
        self.current_user_id = (
            get_active_steam_user(Path(self.steam_path))
            if self.steam_path
            else None
        )
        if not self.current_user_id:
            logger.warning("[AccountSwitch] Could not detect current Steam user")
            return False

        last_known = self._load_last_known_user()

        if last_known is None:
            logger.info(
                "[AccountSwitch] First run, recording user %s",
                self.current_user_id,
            )
            self.account_switch_detected = False
            return False

        if last_known == self.current_user_id:
            logger.debug(
                "[AccountSwitch] Same user %s, no switch", self.current_user_id
            )
            self.account_switch_detected = False
            return False

        self.previous_user_id = last_known
        logger.info(
            "[AccountSwitch] Account switch detected: %s -> %s",
            last_known,
            self.current_user_id,
        )

        if self.has_active_auth_tokens() or self.has_registry_entries():
            self.account_switch_detected = True
            return True

        logger.info(
            "[AccountSwitch] Switch detected but nothing to migrate — "
            "skipping modal",
        )
        self.account_switch_detected = False
        return False

    def should_show_modal(self) -> bool:
        """True if a switch was detected and there's actionable data."""
        return self.account_switch_detected

    def has_active_auth_tokens(self) -> bool:
        """True if any store auth-token file exists on disk."""
        for store, path in auth_token_paths().items():
            if path.exists():
                logger.debug(
                    "[AccountSwitch] Found auth token for %s: %s", store, path
                )
                return True
        return False

    def has_registry_entries(self) -> bool:
        """True if ``shortcuts_registry.json`` has any entries."""
        try:
            path = registry_path()
            if path.exists():
                with path.open() as f:
                    registry = json.load(f)
                return len(registry) > 0
        except Exception:
            logger.exception("[AccountSwitch] Error reading registry")
        return False

    def save_current_user(self) -> None:
        """Persist ``current_user_id`` as ``last_known_user_id``.

        Called after :meth:`detect_account_switch` so the *next* launch
        compares against this user (the modal shows once per switch).
        """
        if not self.current_user_id:
            return
        try:
            SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            settings: dict[str, Any] = {}
            if SETTINGS_PATH.exists():
                with SETTINGS_PATH.open() as f:
                    settings = json.load(f)
            settings["last_known_user_id"] = self.current_user_id
            with SETTINGS_PATH.open("w") as f:
                json.dump(settings, f, indent=2)
            logger.info(
                "[AccountSwitch] Saved current user %s to settings",
                self.current_user_id,
            )
        except Exception:
            logger.exception("[AccountSwitch] Error saving current user")

    def migrate_artwork(self) -> dict[str, Any]:
        """Copy grid artwork from the previous user's folder to the current.

        Never overwrites existing artwork. Returns
        ``{'copied': int, 'errors': list}``.
        """
        result: dict[str, Any] = {"copied": 0, "errors": []}

        if not (self.previous_user_id and self.current_user_id and self.steam_path):
            result["errors"].append("Missing user IDs or steam path")
            return result

        source_grid = (
            Path(self.steam_path)
            / "userdata"
            / self.previous_user_id
            / "config"
            / "grid"
        )
        target_grid = (
            Path(self.steam_path)
            / "userdata"
            / self.current_user_id
            / "config"
            / "grid"
        )

        if not source_grid.is_dir():
            logger.info(
                "[AccountSwitch] No artwork folder for previous user %s",
                self.previous_user_id,
            )
            return result

        target_grid.mkdir(parents=True, exist_ok=True)
        try:
            for source_file in source_grid.iterdir():
                target_file = target_grid / source_file.name
                if not source_file.is_file():
                    continue
                if target_file.exists():
                    continue
                try:
                    shutil.copy2(source_file, target_file)
                    result["copied"] += 1
                except Exception as e:
                    result["errors"].append(
                        f"Failed to copy {source_file.name}: {e}"
                    )
            logger.info(
                "[AccountSwitch] Copied %d artwork files from %s to %s",
                result["copied"],
                self.previous_user_id,
                self.current_user_id,
            )
        except Exception as e:
            logger.exception("[AccountSwitch] Error migrating artwork")
            result["errors"].append(str(e))

        return result

    def _load_last_known_user(self) -> str | None:
        """Read ``last_known_user_id`` from settings; ``None`` if unset."""
        try:
            if SETTINGS_PATH.exists():
                with SETTINGS_PATH.open() as f:
                    settings = json.load(f)
                value = settings.get("last_known_user_id")
                return value if isinstance(value, str) else None
        except Exception:
            logger.exception("[AccountSwitch] Error reading settings")
        return None
