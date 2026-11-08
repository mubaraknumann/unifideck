"""core/secure_token_store.py — Encrypted on-disk token storage.

Provides authenticated symmetric encryption for OAuth tokens
persisted by store modules (currently GOG and Microsoft). Built
on cryptography's AESGCM primitive with a key derived via scrypt
from the device's machine-id and a static domain-separation salt.

Threat model addressed
----------------------
This layer protects against ONE specific scenario: accidental
exfiltration of the encrypted token file from the device. A user
who shares a bug report archive, syncs ~/.config to a cloud
backup, or uploads a file to a forum will not leak usable tokens
because the recipient cannot decrypt the file without the
machine-id of the original Steam Deck.

Threat model NOT addressed
--------------------------
This layer does NOT protect against:

- A local attacker with read access to the user's home (they
  can read /etc/machine-id and reconstruct the key)
- A malicious process running under the same uid as Unifideck
- Memory dumps while tokens are in use (plaintext in memory)
- Compromise of the OAuth token at the network level

These are out of scope for a Decky plugin. A real defence-in-
depth solution would require a TPM-backed key, kernel-enforced
process isolation, or a hardware security module — none of which
are accessible from a userspace Python plugin on SteamOS.

Format
------
Encrypted file layout (binary):

    [4 bytes] magic header "UFD1"
    [12 bytes] AES-GCM nonce (random per encryption)
    [N bytes] ciphertext + 16-byte GCM auth tag

Total overhead: 32 bytes per file. The magic header lets us
detect non-encrypted files from a previous Unifideck version
and trigger a one-shot migration without crashing.

KDF parameters
--------------
scrypt(N=2**14, r=8, p=1) — the same parameters as the original
scrypt paper for "interactive" use. ~50 ms on a Steam Deck APU
which is acceptable on the auth path (called once per save) but
slow enough to make brute-forcing impractical.

The salt is a fixed 16-byte constant baked into this module. It
is NOT a secret — its only purpose is domain separation, so that
a key derived for Unifideck cannot be reused on another piece of
software that happens to derive keys from the same machine-id.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from .device_identity import DeviceIdentity, DeviceIdentityError

logger = logging.getLogger(__name__)

# Magic header that identifies a Unifideck-encrypted file.
# "UFD" + format version 1. If we ever change the format
# (different KDF, different cipher), bump the digit and add a
# branch in _decrypt to handle legacy versions.
_MAGIC = b"UFD1"
_NONCE_SIZE = 12 # AES-GCM standard nonce size
_KEY_SIZE = 32 # AES-256

# Metadata key embedded by encrypt_payload before encryption.
# The leading underscore + ``unifideck_`` namespace is chosen
# to be vanishingly unlikely to collide with any field a store
# might legitimately persist, so callers can pass arbitrary
# domain payloads without renaming. The key sits inside the
# encrypted blob — protected by GCM auth — so an attacker
# can't tamper with it to bypass rotation policies.
_METADATA_ENCRYPTED_AT_KEY = "_unifideck_encrypted_at"

# Static salt for the KDF. Different from any other software
# that might derive keys from the same machine-id. NOT secret.
# 16 bytes is the cryptography library's recommended size.
_KDF_SALT = b"unifideck-secure-token-store-v1!" # 32 bytes
assert len(_KDF_SALT) == 32, "salt must be exactly 32 bytes"

# scrypt parameters — interactive use, ~50ms on Steam Deck APU.
_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1


class SecureTokenStoreError(RuntimeError):
    """Raised when encryption or decryption fails irrecoverably.

    Distinct from DeviceIdentityError so callers can decide whether
    to wipe the file (corrupt ciphertext) or escalate (no
    machine-id available — system misconfigured).
    """


class SecureTokenStore:
    """Encrypts and decrypts JSON token payloads on disk.

    Stateless apart from the lazily-derived key, which is cached
    after first use. Safe to instantiate once per store and reuse
    for every load/save cycle. Not thread-safe — callers should
    serialize access if multiple coroutines write concurrently.
    """

    def __init__(
        self,
        device_identity: DeviceIdentity | None = None,
        bus: Any = None,
    ) -> None:
        """Create a store using the given DeviceIdentity reader.

        device_identity: injected for tests (FakeDeviceIdentity).
        Defaults to a real DeviceIdentity() reading /etc/machine-id.

        bus: optional EventBus for emitting SECURITY_* audit events.
        When None (default), the store works exactly as before but
        emits nothing — keeping unit tests minimal. Production code
        in main.py passes the real bus so SecurityService can
        observe every encryption operation.
        """
        self._device_identity = device_identity or DeviceIdentity()
        self._bus = bus
        self._key: bytes | None = None
        # Strong refs to in-flight ``bus.emit`` coroutines so the
        # event loop doesn't GC them mid-delivery (which would log
        # "Task was destroyed but it is pending"). Populated and
        # auto-pruned by ``_emit_security_event``.
        self._background_tasks: set[asyncio.Task[Any]] = set()

    # ── Public API ──────────────────────────────────────────────

    def encrypt_payload(self, payload: dict[str, Any]) -> bytes:
        """Serialise `payload` to JSON, encrypt, return raw bytes.

        Before encryption, the payload is enriched with a
        metadata field ``_unifideck_encrypted_at`` containing the
        current wall-clock timestamp (``time.time()``). This lets
        callers compute the age of stored credentials via
        ``payload_age_seconds()`` after a subsequent decrypt and
        enforce maximum-age policies (forced re-auth after N days
        of inactivity, for example).

        The metadata is added INSIDE the encrypted blob, so:

          - it cannot be tampered with by an attacker (GCM auth
            would catch a flip);
          - it survives intact through the existing migration
            paths (legacy plaintext files have no timestamp,
            which the helper signals as ``None`` so callers can
            decide whether to treat that as "expired" or "fresh
            and stamp on next save").

        Callers that already use a key with the same name in
        their payload have it overwritten — log at debug level
        the rare case to surface accidental collisions during
        development without spamming production.

        The returned bytes are ready to write to disk as a binary
        file. Callers should still set 0o600 permissions on the
        file itself — encryption protects the contents, not the
        metadata (which would still leak which keys exist).
        """
        enriched = dict(payload)
        if _METADATA_ENCRYPTED_AT_KEY in enriched:
            logger.debug(
                "[SecureTokenStore] caller-supplied %s key will "
                "be overwritten by encrypt_payload",
                _METADATA_ENCRYPTED_AT_KEY,
            )
        enriched[_METADATA_ENCRYPTED_AT_KEY] = time.time()
        plaintext = json.dumps(
            enriched, separators=(",", ":"),
        ).encode("utf-8")
        return self._encrypt(plaintext)

    @staticmethod
    def payload_age_seconds(
        payload: dict[str, Any],
        *,
        now: float | None = None,
    ) -> float | None:
        """Return the age of a decrypted payload in seconds.

        Reads the ``_unifideck_encrypted_at`` metadata field
        injected by ``encrypt_payload`` at write time. If absent
        (legacy plaintext file from a pre-rotation Unifideck
        version), returns ``None`` so the caller can apply its
        own policy (typically: treat as expired-but-don't-crash,
        let the next save() stamp it).

        Args:
            payload: The dict returned by ``decrypt_payload``.
            now: Override the current wall-clock time, only
                used by tests. Production callers leave it as
                ``None`` so ``time.time()`` is consulted.

        Returns:
            A non-negative float (age in seconds) when the
            timestamp is present, ``None`` otherwise. A negative
            difference (clock went backwards across reboots or
            NTP fix) is clamped to 0.0 — we never report a
            negative age, which would be confusing for callers
            doing ``age > threshold`` comparisons.
        """
        ts = payload.get(_METADATA_ENCRYPTED_AT_KEY)
        if not isinstance(ts, (int, float)):
            return None
        current = now if now is not None else time.time()
        delta = current - float(ts)
        return max(delta, 0.0)

    def encrypt_bytes(self, plaintext: bytes) -> bytes:
        """Encrypt raw bytes exactly as given, with no metadata added.

        For files another program reads back: the Xodus backend seals its
        credentials in this same format (``ours-encrypted-credentials``).
        """
        return self._encrypt(plaintext)

    def decrypt_payload(self, blob: bytes) -> dict[str, Any]:
        """Decrypt and parse `blob` back into a payload dict.

        Raises SecureTokenStoreError on any failure (corrupted
        file, wrong machine-id, tampered ciphertext, malformed
        JSON inside). Callers should handle this by treating
        the file as unusable and forcing a re-auth.
        """
        plaintext = self._decrypt(blob)
        try:
            data = json.loads(plaintext.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise SecureTokenStoreError(
                f"decrypted payload is not valid JSON: {e}",
            ) from e
        if not isinstance(data, dict):
            raise SecureTokenStoreError(
                f"decrypted payload is not a JSON object "
                f"(got {type(data).__name__})",
            )
        return data

    def is_encrypted(self, blob: bytes) -> bool:
        """Return True if `blob` looks like an encrypted file.

        Used by migration logic: a legacy unencrypted JSON file
        starts with `{` (0x7b), an encrypted file starts with
        the magic header `UFD1`. The check is cheap (4 byte
        comparison) so callers can do it on every load to
        transparently handle both formats during the migration
        window.
        """
        return blob[:4] == _MAGIC

    # ── Private helpers ─────────────────────────────────────────

    def _get_key(self) -> bytes:
        """Derive the AES-256 key from the machine-id, cached.

        Raises SecureTokenStoreError if the machine-id cannot
        be read. The KDF call takes ~50ms on a Steam Deck so
        we cache the result for the instance lifetime.
        """
        if self._key is not None:
            return self._key
        try:
            mid = self._device_identity.read()
        except DeviceIdentityError as e:
            raise SecureTokenStoreError(
                f"cannot derive key without machine-id: {e}",
            ) from e
        kdf = Scrypt(
            salt=_KDF_SALT,
            length=_KEY_SIZE,
            n=_SCRYPT_N,
            r=_SCRYPT_R,
            p=_SCRYPT_P,
        )
        self._key = kdf.derive(mid.encode("utf-8"))
        logger.debug("[SecureTokenStore] derived AES-256 key from machine-id")
        return self._key

    def _encrypt(self, plaintext: bytes) -> bytes:
        """Encrypt `plaintext` and return magic + nonce + ciphertext.

        Generates a fresh random nonce on every call. AES-GCM
        catastrophically fails (key recovery) if a nonce is
        reused with the same key, so we never derive nonces
        from the plaintext or use a counter.

        emits SECURITY_TOKEN_ENCRYPTED on the bus
        if one was injected. Failures during the crypto call
        itself bubble up unchanged (they would be a hard bug,
        not a runtime condition).
        """
        key = self._get_key()
        nonce = os.urandom(_NONCE_SIZE)
        aesgcm = AESGCM(key)
        ciphertext = aesgcm.encrypt(nonce, plaintext, associated_data=None)
        self._emit_security_event(
            "SECURITY_TOKEN_ENCRYPTED",
            byte_count=len(plaintext),
        )
        return _MAGIC + nonce + ciphertext

    def _decrypt(self, blob: bytes) -> bytes:
        """Verify magic, extract nonce, authenticate + decrypt.

        Raises SecureTokenStoreError on any anomaly: short blob,
        wrong magic, GCM auth failure (means tampered or wrong
        key, indistinguishable by design).

        emits SECURITY_TOKEN_DECRYPTED on success
        or SECURITY_DECRYPT_FAILED with a reason string on any
        failure path. The reason never includes ciphertext bytes
        or partial plaintext, only the failure category.
        """
        if len(blob) < len(_MAGIC) + _NONCE_SIZE + 16:
            self._emit_security_event(
                "SECURITY_DECRYPT_FAILED", reason="blob_too_short",
            )
            raise SecureTokenStoreError(
                f"encrypted blob too short: {len(blob)} bytes",
            )
        if not self.is_encrypted(blob):
            self._emit_security_event(
                "SECURITY_DECRYPT_FAILED", reason="bad_magic",
            )
            raise SecureTokenStoreError(
                "blob does not start with UFD1 magic header",
            )
        nonce = blob[len(_MAGIC):len(_MAGIC) + _NONCE_SIZE]
        ciphertext = blob[len(_MAGIC) + _NONCE_SIZE:]
        key = self._get_key()
        aesgcm = AESGCM(key)
        try:
            plaintext = aesgcm.decrypt(nonce, ciphertext, associated_data=None)
        except InvalidTag as e:
            self._emit_security_event(
                "SECURITY_DECRYPT_FAILED", reason="gcm_auth_failed",
            )
            raise SecureTokenStoreError(
                "AES-GCM authentication failed — file is corrupt, "
                "tampered, or was encrypted on a different device",
            ) from e
        self._emit_security_event(
            "SECURITY_TOKEN_DECRYPTED", byte_count=len(plaintext),
        )
        return plaintext

    def _emit_security_event(
        self, event_name: str, **kwargs: Any,
    ) -> None:
        """Emit a SECURITY_* event on the bus if one is configured.

        The event enum is imported lazily to avoid a circular
        import between core.types.events and the security package.
        Failures to emit (bus down, handler raised) are caught and
        logged at debug level — security audit must NEVER block
        the actual crypto operation.

        ``EventBus.emit`` is ``async`` but ``_encrypt``/``_decrypt``
        are sync (their crypto primitives are sync), so we can't
        ``await`` it. Instead we schedule it on the running event
        loop with ``loop.create_task`` and keep a strong reference
        in ``_background_tasks`` until delivery completes — without
        the strong ref the task is GC'd and the event silently
        vanishes. When no event loop is running (CLI utilities,
        sync tests) we drop the emission rather than synthesise
        one with ``asyncio.run``, since blocking the caller for
        the duration of every subscriber would re-introduce the
        "never block the crypto operation" hazard the docstring
        warns about.
        """
        if self._bus is None:
            return
        try:
            from unifideck.core.types.events import Events
            event = getattr(Events, event_name)
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                # No running loop — drop the emission.
                return
            task = loop.create_task(self._bus.emit(event, **kwargs))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
        except Exception as e:
            logger.debug(
                "[SecureTokenStore] failed to emit %s: %s",
                event_name, e,
            )
