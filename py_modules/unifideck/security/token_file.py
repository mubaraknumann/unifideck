"""security/token_file.py — One encrypted token file on disk.

The layer between :class:`SecureTokenStore` (which encrypts a payload) and a
store's token manager (which knows what the payload *means*). It owns
everything the two are not: reading bytes, deciding whether what came back is
ciphertext or a legacy plaintext file, writing atomically at 0600, and
emitting the audit events that let a support bundle show credential state.

Extracted from ``stores/gog/tokens/storage.py`` and
``stores/microsoft/tokens/persistence.py``, which had each grown their own
copy of all of it — 272 and 251 lines with the same security-critical core
written twice and already drifting (audit §1.4 c / register item 9). A third
copy was the expected cost of the next store that needs it.

Lives in ``security/`` rather than ``stores/shared/`` — where the register
suggested — because it has no store-specific content: its dependencies are
``SecureTokenStore``, ``secure_io`` and the ``emit_*`` audit helpers, all in
this package. ``stores/`` sits *above* ``security/``, so putting a pure
security primitive there would invert the layering. The store name it takes
is a label for audit events, not a behavioural switch.

What stays with each store: the payload's shape, where its legacy file lived,
and any extra cleanup it owes (GOG's gogdl plaintext mirror). This class never
inspects a payload's keys.

**Never falls back to plaintext.** If encryption is unavailable, ``write``
logs and returns False. Losing the session is the correct outcome; leaking a
refresh token is not.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from .audit_emitter import (
    emit_legacy_plaintext_detected,
    emit_permissions_check,
)
from .secure_token_store import SecureTokenStore, SecureTokenStoreError

if TYPE_CHECKING:
    from unifideck.event_bus.event_bus import EventBus

logger = logging.getLogger(__name__)

#: Owner read/write only. Set at creation time via ``os.open``, not chmod'd
#: afterwards, so the file is never briefly readable by anyone else.
_TOKEN_MODE = 0o600


class EncryptedTokenFile:
    """Read/write one store's encrypted token file.

    Args:
        store: canonical store id, used as the label on audit events.
        secure_store: the crypto layer.
        bus: event bus, or None in contexts without one (audit events are
            then skipped rather than raising).
        log_prefix: bracketed tag for log lines, e.g. ``"[GOGTokens]"``,
            preserved per store so existing log-grep habits keep working.
    """

    def __init__(
        self,
        *,
        store: str,
        secure_store: SecureTokenStore,
        bus: EventBus | None,
        log_prefix: str,
    ) -> None:
        """Initialize the instance."""
        self._store = store
        self._secure_store = secure_store
        self._bus = bus
        self._prefix = log_prefix

    async def read(self, path: str) -> dict[str, Any] | None:
        """Decode the token file at *path*, or None if unusable.

        None covers every failure the caller treats the same way — absent,
        unreadable, undecryptable, unparseable — because all of them mean
        "no usable session", and the distinction is already in the log.
        """
        blob = await asyncio.to_thread(self._read_bytes, path)
        if blob is None:
            return None
        return self._parse(blob, path)

    async def write(self, path: str, payload: dict[str, Any]) -> bool:
        """Encrypt *payload* and write it to *path* atomically at 0600.

        Returns False — having written nothing — when encryption is
        unavailable. There is deliberately no plaintext fallback.
        """
        try:
            blob = self._secure_store.encrypt_payload(payload)
        except SecureTokenStoreError:
            logger.exception(
                "%s cannot encrypt tokens — refusing to write "
                "plaintext fallback", self._prefix,
            )
            return False
        ok = await asyncio.to_thread(self._write_atomic, path, blob)
        if ok:
            await self._emit_permissions(path)
        return ok

    async def remove(self, *paths: str) -> None:
        """Delete each existing path. Never raises."""
        def _remove_sync() -> None:
            """Remove sync."""
            for path in paths:
                try:
                    target = Path(path)
                    if target.is_file():
                        target.unlink()
                        logger.info("%s removed %s", self._prefix, path)
                except OSError as e:
                    logger.warning(
                        "%s could not remove %s: %s", self._prefix, path, e,
                    )

        await asyncio.to_thread(_remove_sync)

    # ── internals ────────────────────────────────────────────────

    def _read_bytes(self, path: str) -> bytes | None:
        """Read the file's bytes, logging and returning None on OSError."""
        try:
            return Path(path).read_bytes()
        except OSError as e:
            logger.warning("%s load failed for %s: %s", self._prefix, path, e)
            return None

    def _parse(self, blob: bytes, path: str) -> dict[str, Any] | None:
        """Decrypt *blob*, or read it as a legacy plaintext token file."""
        if self._secure_store.is_encrypted(blob):
            try:
                return self._secure_store.decrypt_payload(blob)
            except SecureTokenStoreError as e:
                logger.warning(
                    "%s decrypt failed for %s: %s", self._prefix, path, e,
                )
                return None
        logger.info(
            "%s reading legacy plaintext token file at %s — will encrypt "
            "on next save", self._prefix, path,
        )
        if self._bus is not None:
            emit_legacy_plaintext_detected(self._bus, self._store, path)
        try:
            return cast(
                "dict[str, Any] | None", json.loads(blob.decode("utf-8")),
            )
        except (ValueError, UnicodeDecodeError) as e:
            logger.warning(
                "%s legacy JSON parse failed: %s", self._prefix, e,
            )
            return None

    def _write_atomic(self, path: str, blob: bytes) -> bool:
        """Write *blob* to *path* via a 0600 temp file plus rename.

        Verbose on purpose: ``Path.open`` takes no permission mode, so the
        file would exist at the umask default before a chmod could narrow
        it, and ``Path.rename`` is not atomic on every filesystem.

        ``os.fdopen`` takes ownership of the fd, so the ``with`` block closes
        it on both the success and failure paths. Microsoft's copy of this
        function also called ``os.close(fd)`` from its except arm, which
        raised ``EBADF`` over the real error whenever the *write* failed —
        so a disk-full token save was reported as "Bad file descriptor".
        """
        try:
            parent = Path(path).parent
            if str(parent):
                parent.mkdir(parents=True, exist_ok=True)
            tmp = path + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _TOKEN_MODE)
            with os.fdopen(fd, "wb") as f:
                f.write(blob)
            Path(tmp).replace(path)
        except OSError as e:
            logger.warning("%s save failed: %s", self._prefix, e)
            return False
        return True

    async def _emit_permissions(self, path: str) -> None:
        """Audit the mode the token file actually landed at."""
        def _stat_mode() -> int | None:
            """Stat mode."""
            try:
                return Path(path).stat().st_mode & 0o7777
            except OSError:
                return None

        mode = await asyncio.to_thread(_stat_mode)
        if mode is not None and self._bus is not None:
            emit_permissions_check(self._bus, self._store, path, mode)


__all__ = ["EncryptedTokenFile"]
