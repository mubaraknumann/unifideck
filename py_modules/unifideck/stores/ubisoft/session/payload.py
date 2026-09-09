"""
UPC payload sync between Wine prefixes.

``_PayloadSync`` copies credentials and auth-cache artifacts from one
Wine prefix to another. Two kinds of payload exist:

* **credentials** (``ConnectSecureStorage.dat``, ``user.dat``) — the
  session vault, copied only between prefixes with a matching machine
  GUID.
* **auth-cache artifacts** (settings, cookies, http2 cache, ownership
  cache) — copied without that guard.

A note on the GUID guard, because its original rationale was wrong and
the wrong version cost a bug reporter a lot of time (GH #435). UPC's
vault is DPAPI-wrapped, but Wine's ``CryptProtectData``
(``dlls/crypt32/protectdata.c``) derives its key from the Windows user
name, a hardcoded constant, and a random salt stored *inside the blob* —
there are no master keys on disk, and the machine GUID is not an input.
A vault therefore decrypts fine in any prefix whose Windows user is the
same ``steamuser``. The guard is kept because refusing to mix credentials
between prefixes of visibly different identity is still the conservative
thing to do, but a GUID match is **not** what makes decryption work, and a
GUID mismatch is not an explanation for a rejected sign-in.

The sync is idempotent: artifacts are hashed before copying so identical
files aren't re-copied. The hash function preserves a strict ordering
(files sorted alphabetically per directory, sub-dirs in filesystem
order) to keep digest stability across runs — caches built before this
ordering policy was applied may produce different hashes and trigger a
one-time re-sync.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .facade import UbisoftSession
logger = logging.getLogger(__name__)
_CSS_MIN_SOURCE_SIZE = 10
_HASH_CHUNK_SIZE = 1024 * 1024
#: The session vault, and the account file whose presence means "signed in".
_CSS_NAME = "ConnectSecureStorage.dat"
_ACCOUNT_NAME = "user.dat"

class _PayloadSync:
    """Payload sync."""

    def __init__(self, parent: UbisoftSession) -> None:
        """Initialize the instance."""
        self._parent = parent

    def sync_payload_to_prefix(
        self,
        source_prefix: str,
        target_prefix: str,
        *,
        payload_sources: dict[str, str],
        apply_dpapi_guard: bool,
        handle_directories: bool,
        log_label: str,
        keep_newer_target: bool = False,
    ) -> int:
        """Sync payload to prefix."""
        if self.should_skip_payload_sync(
            source_prefix,
            target_prefix,
            payload_sources,
            apply_dpapi_guard,
        ):
            return 0
        synced = 0
        for _root, user_home in self._parent._paths.iter_user_homes(target_prefix):
            target_root = str(Path(user_home) / self._parent._config.upc_local_subdir)
            # Decided once per target, from the vault, and applied to the whole
            # payload. ``ConnectSecureStorage.dat`` and ``user.dat`` are one
            # session between them: copying the vault while declining the
            # account file would leave the prefix holding a token from one
            # sign-in and an account from another.
            if keep_newer_target and self._would_clobber_newer(
                payload_sources, target_root, log_label,
            ):
                continue
            for rel_path, src_path in payload_sources.items():
                dst_path = str(Path(target_root) / rel_path)
                if self.copy_payload_entry(
                    src_path,
                    dst_path,
                    handle_directories=handle_directories,
                    log_label=log_label,
                    rel_path=rel_path,
                ):
                    synced += 1
        return synced

    def should_skip_payload_sync(
        self,
        source_prefix: str,
        target_prefix: str,
        payload_sources: dict[str, str],
        apply_dpapi_guard: bool,
    ) -> bool:
        """Check whether skip payload sync."""
        if os.path.realpath(source_prefix) == os.path.realpath(target_prefix):
            return True
        if not payload_sources:
            return True
        if apply_dpapi_guard:
            source_guid = self._parent._read_machine_guid(
                source_prefix,
            )
            target_guid = self._parent._read_machine_guid(
                target_prefix,
            )
            if source_guid and target_guid and source_guid != target_guid:
                logger.warning(
                    "[UbisoftSession] MachineGuid mismatch: "
                    "source=%s… target=%s… — skipping "
                    "DPAPI sync",
                    source_guid[:8],
                    target_guid[:8],
                )
                return True
        return False

    @staticmethod
    def _would_clobber_newer(
        payload_sources: dict[str, str],
        target_root: str,
        log_label: str,
    ) -> bool:
        """True if writing this payload into *target_root* would replace a
        NEWER session.

        The guard this replaced compared file *sizes* and refused any copy
        that shrank the vault, on the theory that "smaller means logged out".
        It did stop the incident it was written for (an SD-card prefix logging
        out and poisoning auth plus every game prefix), but it was also a
        one-way ratchet: Ubisoft rotates the refresh token on every sign-in
        and a rotated vault is routinely a little smaller than the one before
        it, so the largest vault ever written won permanently. The auth prefix
        froze on a server-dead token, and signing into one game then signed
        the user out of every other one (GH #435, reproduced).

        Time is the correct ordering. A stale copy — logged-out or simply
        older — still cannot overwrite a live session, because its mtime is
        older. A newer token flows regardless of size. The destination must
        also actually be signed in: an older-but-signed-in vault must never
        block a fresh session from reaching a signed-out prefix.

        Judged on the vault alone, and the answer covers the whole payload —
        the vault and ``user.dat`` are one session between them.
        """
        src_vault = payload_sources.get(_CSS_NAME)
        if not src_vault:
            return False
        dst_vault = Path(target_root) / _CSS_NAME
        if not (dst_vault.is_file() and Path(src_vault).is_file()):
            return False
        try:
            src_mtime = Path(src_vault).stat().st_mtime
            dst_mtime = dst_vault.stat().st_mtime
        except OSError:
            return False
        if dst_mtime <= src_mtime:
            return False
        if not (Path(target_root) / _ACCOUNT_NAME).is_file():
            return False
        logger.info(
            "[UbisoftSession] %s: keeping %s — it holds a NEWER signed-in "
            "session (%.0f > %.0f)",
            log_label,
            target_root,
            dst_mtime,
            src_mtime,
        )
        return True

    def copy_payload_entry(
        self,
        src_path: str,
        dst_path: str,
        *,
        handle_directories: bool,
        log_label: str,
        rel_path: str,
    ) -> bool:
        """Copy payload entry."""
        if Path(dst_path).exists():
            try:
                same = self.hash_artifact(src_path) == self.hash_artifact(dst_path)
            except OSError:
                same = False
            if same:
                return False
        try:
            parent = str(Path(dst_path).parent)
            if parent:
                Path(parent).mkdir(parents=True, exist_ok=True)
            if handle_directories:
                if Path(dst_path).is_dir():
                    shutil.rmtree(
                        dst_path,
                        ignore_errors=True,
                    )
                elif Path(dst_path).exists():
                    Path(dst_path).unlink()
                if Path(src_path).is_dir():
                    shutil.copytree(src_path, dst_path)
                else:
                    shutil.copy2(src_path, dst_path)
            else:
                shutil.copy2(src_path, dst_path)
            return True
        except OSError as e:
            logger.warning(
                "[UbisoftSession] %s copy failed for %s: %s",
                log_label,
                rel_path,
                e,
            )
            return False

    def purge_credentials_from_prefix(self, target_prefix: str) -> int:
        """Delete UPC credentials + auth-cache artifacts from a prefix.

        The inverse of :meth:`sync_credentials_to_prefix` /
        :meth:`sync_auth_artifacts_to_prefix`: removes the same entries
        (``upc_credential_files`` + ``upc_auth_cache_artifacts``) so a
        signed-out prefix can no longer be picked up as a credential
        fallback source by
        :meth:`_CredentialReader.find_best_credential_source` (which would
        otherwise silently re-authenticate the user on the next launch).
        Returns the number of entries removed.
        """
        config = self._parent._config
        rel_entries = (
            *config.upc_credential_files,
            *config.upc_auth_cache_artifacts,
        )
        removed = 0
        for _root, user_home in self._parent._paths.iter_user_homes(
            target_prefix,
        ):
            local_root = Path(user_home) / config.upc_local_subdir
            for rel in rel_entries:
                if self._remove_credential_path(local_root / rel, rel):
                    removed += 1
        return removed

    @staticmethod
    def _remove_credential_path(target: Path, rel: str) -> bool:
        """Delete ``target`` (directory or file); True if it removed
        something, False if it was absent or removal failed."""
        try:
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
                return True
            if target.exists():
                target.unlink()
                return True
        except OSError as e:
            logger.warning(
                "[UbisoftSession] purge failed for %s: %s", rel, e,
            )
        return False

    def sync_credentials_to_prefix(
        self,
        source_prefix: str,
        target_prefix: str,
    ) -> int:
        """Sync credentials to prefix."""
        return self.sync_payload_to_prefix(
            source_prefix=source_prefix,
            target_prefix=target_prefix,
            payload_sources=self.collect_credential_sources(
                source_prefix,
            ),
            apply_dpapi_guard=True,
            handle_directories=False,
            log_label="credential",
            keep_newer_target=True,
        )

    def collect_credential_sources(
        self,
        source_prefix: str,
    ) -> dict[str, str]:
        """Collect credential sources."""
        source_files: dict[str, str] = {}
        for _root, user_home in self._parent._paths.iter_user_homes(
            source_prefix,
            pfx_first=True,
        ):
            for fname in self._parent._config.upc_credential_files:
                if fname in source_files:
                    continue
                src = str(Path(user_home) / self._parent._config.upc_local_subdir / fname)
                if self._parent._is_valid_css(
                    src,
                    _CSS_MIN_SOURCE_SIZE,
                ):
                    source_files[fname] = src
        return source_files

    def sync_auth_artifacts_to_prefix(
        self,
        source_prefix: str,
        target_prefix: str,
    ) -> int:
        """Sync auth artifacts to prefix."""
        return self.sync_payload_to_prefix(
            source_prefix=source_prefix,
            target_prefix=target_prefix,
            payload_sources=self.collect_artifact_sources(
                source_prefix,
            ),
            apply_dpapi_guard=False,
            handle_directories=True,
            log_label="auth cache artifact",
        )

    def collect_artifact_sources(
        self,
        source_prefix: str,
    ) -> dict[str, str]:
        """Collect artifact sources."""
        artifacts: dict[str, str] = {}
        for _root, user_home in self._parent._paths.iter_user_homes(
            source_prefix,
            pfx_first=True,
        ):
            local_root = str(Path(user_home) / self._parent._config.upc_local_subdir)
            for rel_path in self._parent._config.upc_auth_cache_artifacts:
                if rel_path in artifacts:
                    continue
                candidate = str(Path(local_root) / rel_path)
                if Path(candidate).is_file() or Path(candidate).is_dir():
                    artifacts[rel_path] = candidate
        return artifacts

    @staticmethod
    def hash_artifact(path: str) -> str:
        """Check whether artifact."""
        digest = hashlib.sha256()
        if Path(path).is_dir():
            _PayloadSync._hash_directory_into(digest, path)
        elif Path(path).is_file():
            _PayloadSync._hash_file_into(digest, path)
        return digest.hexdigest()

    @staticmethod
    def _hash_directory_into(digest: hashlib._Hash, path: str) -> None:
        """Hash directory into."""
        for root, _dirs, files in os.walk(path):
            files.sort()
            for name in files:
                file_path = str(Path(root) / name)
                rel_path = os.path.relpath(file_path, path)
                digest.update(rel_path.encode("utf-8"))
                _PayloadSync._hash_file_into(digest, file_path)

    @staticmethod
    def _hash_file_into(digest: hashlib._Hash, path: str) -> None:
        """Hash file into."""
        with (
            contextlib.suppress(OSError),
            Path(path).open("rb") as f,
        ):
            for chunk in iter(
                lambda: f.read(_HASH_CHUNK_SIZE),
                b"",
            ):
                digest.update(chunk)
