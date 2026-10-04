"""Microsoft token persistence — load, save, clear.

py_modules/unifideck/stores/microsoft/tokens/persistence.py

``PersistenceMixin`` owns the *meaning* of the token payload — which keys it
carries, how they land in the manager's in-memory state, and where the
pre-migration plaintext file used to live. The on-disk mechanics beneath that
(read, decrypt, legacy-plaintext detection, atomic 0600 write, permission
audit) belong to :class:`unifideck.security.EncryptedTokenFile`, shared with
GOG's token storage. The two used to carry separate copies of all of it
(audit §1.4 c).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from unifideck.security import EncryptedTokenFile, SecureTokenStore

if TYPE_CHECKING:
    from unifideck.event_bus.event_bus import EventBus
    from unifideck.stores.microsoft.microsoft_config import MicrosoftConfig

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[MicrosoftTokens]"

#: Where tokens lived before they were encrypted. Read once on first load,
#: re-saved to the current path, then deleted.
_LEGACY_PLAINTEXT_PATH = "~/.local/share/unifideck/microsoft_tokens.json"

#: The client that issued every token saved before ``client_id`` was
#: recorded (Unifideck <= 0.7.5): the login.live.com Xbox app client.
UNRECORDED_CLIENT_ID = "000000004C12AE6F"


class PersistenceMixin:
    """Persistence mixin."""

    _ms_access_token: str | None
    _ms_refresh_token: str | None
    _token_saved_at: float
    _config: MicrosoftConfig
    _secure_store: SecureTokenStore
    _bus: EventBus | None
    _client_mismatch: bool
    _mismatch_notified: bool
    _loaded_payload: dict[str, Any] | None

    @property
    def _file(self) -> EncryptedTokenFile:
        """The shared encrypted-token-file primitive for this store.

        Built on demand rather than in ``__init__`` because this is a mixin —
        the concrete token manager owns construction, and threading one more
        attribute through it would couple the two for no gain.
        """
        return EncryptedTokenFile(
            store="microsoft",
            secure_store=self._secure_store,
            bus=self._bus,
            log_prefix=_LOG_PREFIX,
        )

    async def load(self) -> bool:
        """Load."""
        self._client_mismatch = False
        resolved = await self._resolve_token_file()
        if resolved is None:
            return False
        target_file, is_legacy = resolved
        data = await self._file.read(target_file)
        if not isinstance(data, dict):
            return False
        if not self._apply_loaded_tokens(data):
            return False
        logger.info(
            "%s loaded tokens from disk (%s)",
            _LOG_PREFIX, "legacy" if is_legacy else "current",
        )
        if is_legacy:
            await self._migrate_legacy_file()
        return True

    async def _resolve_token_file(self) -> tuple[str, bool] | None:
        """The token file to read as ``(path, is_legacy)``, or None if neither
        the current nor the legacy file exists."""
        path = await self._token_path()
        if await asyncio.to_thread(lambda: Path(path).is_file()):
            return path, False
        legacy_path = await self._legacy_path()
        if await asyncio.to_thread(lambda: Path(legacy_path).is_file()):
            return legacy_path, True
        return None

    def _apply_loaded_tokens(self, data: dict[str, Any]) -> bool:
        """Populate the in-memory token state from a parsed blob.

        Returns False (and resets state) when there's no refresh token —
        nothing usable to keep — or when another OAuth client issued the
        tokens. A refresh token only works with the client that issued it,
        so after the configured client changes the old tokens can only
        fail. They stay on disk untouched: putting the old client back in
        the config makes them valid again with no sign-in.
        """
        refresh = data.get("refresh_token")
        issued_by = data.get("client_id") or UNRECORDED_CLIENT_ID
        configured = self._config.client_id
        self._client_mismatch = bool(refresh and configured) and issued_by != configured
        self._mismatch_notified = (
            data.get("notified_for_client") == self._config.client_id
        )
        self._loaded_payload = data if self._client_mismatch else None
        if not refresh or self._client_mismatch:
            if self._client_mismatch:
                logger.info(
                    "%s saved sign-in was issued to client %s, configured "
                    "client is %s — a new sign-in is needed",
                    _LOG_PREFIX, issued_by, self._config.client_id,
                )
            self._ms_access_token = None
            self._ms_refresh_token = None
            self._token_saved_at = 0.0
            return False
        self._ms_access_token = data.get("access_token") or None
        self._ms_refresh_token = refresh
        try:
            self._token_saved_at = float(data.get("saved_at", 0.0))
        except (TypeError, ValueError):
            self._token_saved_at = 0.0
        return True

    async def _migrate_legacy_file(self) -> None:
        """Re-save freshly-loaded legacy tokens to the current (encrypted)
        location, then remove the legacy plaintext file."""
        logger.info(
            "%s migrating legacy token file to %s",
            _LOG_PREFIX, await self._token_path(),
        )
        if not await self.save():
            return
        await self._file.remove(await self._legacy_path())

    async def save(self) -> bool:
        """Save."""
        if (
            self._ms_access_token is None
            and self._ms_refresh_token is None
        ):
            return True
        return await self._file.write(await self._token_path(), {
            "access_token": self._ms_access_token,
            "refresh_token": self._ms_refresh_token,
            "saved_at": self._token_saved_at,
            "scope": self._config.scope,
            "client_id": self._config.client_id,
        })

    @property
    def client_mismatch(self) -> bool:
        """The saved sign-in belongs to a different OAuth client (see load)."""
        return self._client_mismatch

    @property
    def mismatch_notified(self) -> bool:
        """The user was already told this saved sign-in no longer works."""
        return self._mismatch_notified

    async def mark_mismatch_notified(self) -> None:
        """Record, in the kept old-client payload, that the user was told."""
        if self._loaded_payload is None:
            return
        payload = {**self._loaded_payload, "notified_for_client": self._config.client_id}
        if await self._file.write(await self._token_path(), payload):
            self._loaded_payload = payload
            self._mismatch_notified = True

    async def clear(self) -> None:
        """Clear."""
        self._ms_access_token = None
        self._ms_refresh_token = None
        self._token_saved_at = 0.0
        self._client_mismatch = False
        self._mismatch_notified = False
        self._loaded_payload = None
        await self._file.remove(
            await self._token_path(),
            await self._legacy_path(),
        )

    async def _token_path(self) -> str:
        """The current (encrypted) token file's expanded path."""
        return await asyncio.to_thread(
            lambda: str(Path(self._config.token_file).expanduser()),
        )

    @staticmethod
    async def _legacy_path() -> str:
        """The fixed pre-migration plaintext token location."""
        return await asyncio.to_thread(
            lambda: str(Path(_LEGACY_PLAINTEXT_PATH).expanduser()),
        )
