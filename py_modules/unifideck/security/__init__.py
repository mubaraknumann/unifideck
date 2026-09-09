"""py_modules/unifideck/security/__init__.py — Security package.

Groups every module that deals with hardware-derived encryption,
device fingerprinting, and token confidentiality. The companion
SecurityService (in services/security_service.py) consumes events
emitted by these modules to build an in-memory audit log and
enforce centralised policies (brute-force detection, permission
auto-repair, device freshness checks).

Public API re-exported here so callers don't have to know the
internal file layout. Import as:

    from ...security import SecureTokenStore, DeviceIdentity

Or for the exception types:

    from ...security import SecureTokenStoreError, DeviceIdentityError
"""
from .audit_emitter import (
    audit_auth_flow,
    emit_auth_completed,
    emit_auth_failed,
    emit_auth_started,
    emit_external_auth_check_failed,
    emit_legacy_plaintext_detected,
    emit_permissions_check,
    emit_token_age_exceeded,
    emit_token_file_migrated,
)
from .device_fingerprint import (
    DeviceFingerprint,
    FingerprintState,
)
from .device_identity import (
    DeviceIdentity,
    DeviceIdentityError,
    FakeDeviceIdentity,
)
from .redaction import redact_for_audit
from .secure_io import (
    SecureIOError,
    secure_read_bytes,
    secure_write_atomic,
)
from .secure_token_store import (
    SecureTokenStore,
    SecureTokenStoreError,
)
from .token_file import EncryptedTokenFile

__all__ = [
    "DeviceFingerprint",
    "DeviceIdentity",
    "DeviceIdentityError",
    "EncryptedTokenFile",
    "FakeDeviceIdentity",
    "FingerprintState",
    "SecureIOError",
    "SecureTokenStore",
    "SecureTokenStoreError",
    "audit_auth_flow",
    "emit_auth_completed",
    "emit_auth_failed",
    "emit_auth_started",
    "emit_external_auth_check_failed",
    "emit_legacy_plaintext_detected",
    "emit_permissions_check",
    "emit_token_age_exceeded",
    "emit_token_file_migrated",
    "redact_for_audit",
    "secure_read_bytes",
    "secure_write_atomic",
]
