"""Encrypted token persistence — load, persist, clear.

``_TokenStorage`` owns the *meaning* of GOG's token file: which keys the
payload carries and how they map onto :class:`GOGUserInfo`. Everything below
that — reading bytes, decrypting, detecting a legacy plaintext file, the
atomic 0600 write, the permission audit — belongs to
:class:`unifideck.security.EncryptedTokenFile`, which Microsoft's token
manager shares. Those halves used to be one file here and one file there,
duplicated and drifting (audit §1.4 c).

The file lives at ``GOGConfig.token_file_expanded``. Encryption failure is
never papered over with a plaintext fallback: we would rather lose the
session than leak the refresh token.

GOG-specific beyond the shared primitive: the ``GOGUserInfo`` shaping above,
and cleaning up the plaintext mirror gogdl writes into its own config dir
after every subprocess invocation.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from unifideck.security import (
    EncryptedTokenFile,
    SecureTokenStore,
    emit_token_file_migrated,
)

from .user_info import GOGUserInfo

if TYPE_CHECKING:
    from unifideck.stores.gog.config import GOGConfig

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[GOGTokens]"

#: gogdl re-writes this plaintext copy of the credentials into its own
#: config dir on every invocation. Removed after each persist.
_GOGDL_MIRROR_NAME = "gog_credentials.json"

class _TokenStorage:
    """Token storage."""

    def __init__(
        self,
        *,
        config: GOGConfig,
        bus: Any,
        secure_store: SecureTokenStore,
    ) -> None:
        """Initialize the instance."""
        self._config = config
        self._bus = bus
        self._file = EncryptedTokenFile(
            store="gog",
            secure_store=secure_store,
            bus=bus,
            log_prefix=_LOG_PREFIX,
        )

    async def load(self) -> tuple[str, str, GOGUserInfo] | None:
        """Load."""
        path = await self._token_path()
        if not await asyncio.to_thread(lambda: Path(path).is_file()):
            return None
        data = await self._file.read(path)
        if not isinstance(data, dict):
            return None
        access = data.get("access_token")
        refresh = data.get("refresh_token")
        if not access or not refresh:
            return None
        user_info = GOGUserInfo(
            username=str(data.get("username", "")),
            galaxy_user_id=str(data.get("user_id", "")),
        )
        logger.info(
            "%s loaded tokens from disk (user=%s)",
            _LOG_PREFIX, user_info.username or "unknown",
        )
        return access, refresh, user_info

    async def persist(
        self,
        access_token: str,
        refresh_token: str,
        user_info: GOGUserInfo,
    ) -> bool:
        """Persist."""
        path = await self._token_path()
        ok = await self._file.write(path, {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "username": user_info.username,
            "user_id": user_info.galaxy_user_id,
        })
        if not ok:
            return False
        await self._remove_stale_gogdl_mirror()
        logger.info("%s saved tokens (encrypted)", _LOG_PREFIX)
        return True

    async def clear_files(self) -> None:
        """Clear files."""
        await self._file.remove(
            await self._token_path(),
            await self._gogdl_mirror_path(),
        )

    async def _token_path(self) -> str:
        """The encrypted token file's expanded path."""
        return await asyncio.to_thread(
            lambda: str(Path(self._config.token_file).expanduser()),
        )

    async def _gogdl_mirror_path(self) -> str:
        """gogdl's plaintext credential mirror inside its own config dir."""
        return await asyncio.to_thread(
            lambda: str(
                Path(self._config.gogdl_config_dir).expanduser()
                / _GOGDL_MIRROR_NAME,
            ),
        )

    async def _remove_stale_gogdl_mirror(self) -> None:
        """Remove stale GOGDL mirror."""
        stale = await self._gogdl_mirror_path()

        def _remove() -> bool:
            """Remove."""
            if not Path(stale).is_file():
                return False
            try:
                Path(stale).unlink()
                logger.info(
                    "%s removed stale gogdl mirror at %s", _LOG_PREFIX, stale,
                )
                return True
            except OSError as e:
                logger.warning(
                    "%s could not remove stale gogdl mirror %s: %s",
                    _LOG_PREFIX, stale, e,
                )
                return False

        removed = await asyncio.to_thread(_remove)
        if removed:
            emit_token_file_migrated(self._bus, "gog", stale, "")
