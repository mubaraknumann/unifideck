"""RomM connection state: pairing attempts and the committed config.

A connection is built in steps so the user can choose platform variants
before anything is committed. The credential is a client token, obtained by
RomM's device pairing flow (the user approves a short code in the RomM web
UI, so no password ever reaches this device) or pasted in directly.
Dismissing the modal at any point discards the attempt and leaves no
connection behind.

Unifideck never revokes a token: the one it obtains by pairing needs an
account-write scope to delete itself, and asking for that scope would make a
leaked token far more dangerous than the read-only one it requests instead.
The pairing names a device in RomM, which the user can remove there.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from unifideck.core.types import AuthResult, Result

from .client import RomMAPIError, RomMClient, normalize_server_url

logger = logging.getLogger(__name__)

STORE_NAME = "romm"
ALLOWED_PLATFORM_SLUGS = frozenset({"linux", "win"})
TOKEN_SCOPES = ["roms.read", "platforms.read"]

# Cancelling an attempt has to outlive the attempt itself: a cancel can arrive
# before its own begin (the user dismisses the modal while the request is still
# in flight), and a begin that arrives later with that ID must be refused. The
# expiry and cap below keep a long session from accumulating them.
_TOMBSTONE_TTL_S = 300.0
_MAX_TOMBSTONES = 64


def eligible_variants(platforms: Any) -> list[dict[str, Any]]:
    """Allowed platforms as ``{id, platform_slug, fs_slug, display_name}``.

    Platform slugs identify supported platform types; numeric IDs are local
    to the server.
    """
    if not isinstance(platforms, list):
        raise RomMAPIError("RomM returned an unexpected platform list", code="malformed")
    variants = []
    for item in platforms:
        if not isinstance(item, dict):
            raise RomMAPIError("RomM returned an unexpected platform list", code="malformed")
        slug = item.get("slug")
        if slug not in ALLOWED_PLATFORM_SLUGS:
            continue
        fs_slug = item.get("fs_slug")
        pid = item.get("id")
        if isinstance(pid, bool) or not isinstance(pid, int) or not isinstance(fs_slug, str) or not fs_slug:
            raise RomMAPIError("RomM returned an unexpected platform list", code="malformed")
        name = item.get("display_name") or item.get("name") or fs_slug
        variants.append({
            "id": pid, "platform_slug": slug, "fs_slug": fs_slug,
            "display_name": str(name),
        })
    return variants


@dataclass
class _Attempt:
    state: str = "starting"  # starting | pairing | ready | committed
    cancelled: bool = False
    polling: bool = False
    token: str = ""
    server_url: str = ""
    verify_ssl: bool = True
    download_dir: str = ""
    identifier: str = ""
    device_code: str = ""
    deadline: float = 0.0
    variants: list[dict[str, Any]] = field(default_factory=list)

    def public_variants(self) -> list[dict[str, str]]:
        return [
            {"platform_slug": v["platform_slug"], "fs_slug": v["fs_slug"], "display_name": v["display_name"]}
            for v in self.variants
        ]


def _device_name() -> str:
    try:
        host = socket.gethostname()
    except OSError:
        host = ""
    return (f"Unifideck ({host})" if host else "Unifideck")[:255]


class RomMAuth:
    def __init__(self, config_file: str) -> None:
        self._config_path = Path(config_file).expanduser()
        self._cfg: dict[str, Any] = {}
        self._attempts: dict[str, _Attempt] = {}
        self._tombstones: dict[str, float] = {}
        self._committed_by: str | None = None
        self._unauthorized = False
        self._lock = asyncio.Lock()
        self._load_config()

    # ── Persistence ────────────────────────────────────────────────

    def _load_config(self) -> None:
        try:
            if self._config_path.exists():
                data = json.loads(self._config_path.read_text())
                self._cfg = data if isinstance(data, dict) else {}
        except (OSError, ValueError) as exc:
            logger.warning("[RomMAuth] Could not read config: %s", type(exc).__name__)
            self._cfg = {}

    def _write_config(self, cfg: dict[str, Any]) -> None:
        """Replace the config atomically; the file never exists with wider mode."""
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=self._config_path.parent, prefix=".romm-")
        temp = Path(name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as stream:
                json.dump(cfg, stream, indent=2)
            temp.replace(self._config_path)
        finally:
            temp.unlink(missing_ok=True)

    def _clear_config(self) -> bool:
        try:
            self._config_path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("[RomMAuth] Could not delete config: %s", type(exc).__name__)
            return False
        self._cfg = {}
        self._unauthorized = False
        self._committed_by = None
        return True

    # ── Committed state ────────────────────────────────────────────

    @property
    def server_url(self) -> str | None:
        return self._cfg.get("server_url") or None

    @property
    def verify_ssl(self) -> bool:
        return bool(self._cfg.get("verify_ssl", True))

    @property
    def download_dir(self) -> str | None:
        return self._cfg.get("download_dir") or None

    @property
    def excluded_fs_slugs(self) -> list[str]:
        value = self._cfg.get("excluded_fs_slugs")
        return [s for s in value if isinstance(s, str)] if isinstance(value, list) else []

    def is_authenticated(self) -> bool:
        return bool(
            self._cfg.get("server_url") and self._cfg.get("token") and not self._unauthorized
        )

    def client(self) -> RomMClient:
        if not self.is_authenticated():
            raise RomMAPIError("Not connected to RomM", status=401, code="unauthorized")
        return RomMClient(self._cfg["server_url"], self._cfg["token"], self.verify_ssl)

    def mark_unauthorized(self) -> None:
        """Mark the connection unavailable after a 401, preserving saved credentials.

        Keep the rejection flag in memory so a proxy's 401 cannot delete the
        saved connection. A successful connection clears the flag.
        """
        self._unauthorized = True

    # ── Attempt lifecycle ──────────────────────────────────────────

    def _prune_tombstones(self) -> None:
        cutoff = time.monotonic() - _TOMBSTONE_TTL_S
        for key in [k for k, t in self._tombstones.items() if t < cutoff]:
            del self._tombstones[key]
        while len(self._tombstones) > _MAX_TOMBSTONES:
            del self._tombstones[next(iter(self._tombstones))]

    async def begin(
        self,
        attempt_id: str,
        *,
        server_url: str,
        token: str = "",
        verify_ssl: bool = True,
        download_dir: str = "",
    ) -> AuthResult:
        """Start a connection: validate a pasted token, or start a pairing."""
        if not attempt_id:
            return self._fail("An attempt ID is required", "invalid_attempt")
        try:
            base = normalize_server_url(server_url)
        except RomMAPIError as exc:
            return self._fail(str(exc), exc.code)
        token = (token or "").strip()

        async with self._lock:
            self._prune_tombstones()
            if attempt_id in self._tombstones:
                return self._fail("Connection cancelled", "cancelled")
            if attempt_id in self._attempts:
                return self._fail("Connection attempt already started", "duplicate_attempt")
            attempt = _Attempt(
                server_url=base, verify_ssl=verify_ssl, download_dir=download_dir or "",
                identifier=str(self._cfg.get("device_identifier") or uuid.uuid4().hex),
            )
            self._attempts[attempt_id] = attempt

        try:
            if token:
                attempt.token = token
                return await self._validated(attempt_id, attempt)
            return await self._start_pairing(attempt_id, attempt)
        except BaseException as exc:
            await self._discard(attempt_id, attempt)
            if isinstance(exc, RomMAPIError):
                return self._fail(str(exc), exc.code)
            raise

    async def _start_pairing(self, attempt_id: str, attempt: _Attempt) -> AuthResult:
        client = RomMClient(attempt.server_url, "", attempt.verify_ssl)
        data = await client.device_init(
            identifier=attempt.identifier, name=_device_name(), scopes=TOKEN_SCOPES,
        )
        path = data["verification_path_complete"]
        if not path.startswith("/") or path.startswith("//"):
            raise RomMAPIError("RomM returned an unexpected pairing address", code="malformed")
        expires_in = data.get("expires_in")
        interval = data.get("interval")
        # Absent or nonsensical values fall back to 600 seconds and a 5 second
        # interval: enough for a server that omits them to still drive the
        # polling loop.
        expires = expires_in if isinstance(expires_in, int) and not isinstance(expires_in, bool) and expires_in > 0 else 600
        wait = interval if isinstance(interval, int) and not isinstance(interval, bool) and interval > 0 else 5
        async with self._lock:
            if attempt.cancelled or self._attempts.get(attempt_id) is not attempt:
                return self._fail("Connection cancelled", "cancelled")
            attempt.device_code = data["device_code"]
            attempt.deadline = time.monotonic() + expires
            attempt.state = "pairing"
        return AuthResult(
            success=True, action="pair", store=STORE_NAME,
            metadata={
                "attempt_id": attempt_id, "user_code": data["user_code"],
                "verification_url": attempt.server_url + path,
                "expires_in": expires, "interval": wait,
            },
        )

    async def poll(self, attempt_id: str) -> AuthResult:
        """Check whether the user has approved the pairing."""
        async with self._lock:
            attempt = self._attempts.get(attempt_id)
            if attempt is None or attempt.cancelled:
                return self._fail("Connection attempt not found", "unknown_attempt")
            if attempt.state == "ready":
                return self._variants_result(attempt_id, attempt)
            if attempt.state != "pairing":
                return self._fail("This connection is not waiting for approval", "not_pairing")
            if attempt.polling:
                return self._pending()
            if time.monotonic() > attempt.deadline:
                self._attempts.pop(attempt_id, None)
                return self._fail("The pairing code expired", "pairing_expired")
            attempt.polling = True
        try:
            client = RomMClient(attempt.server_url, "", attempt.verify_ssl)
            try:
                token = await client.device_poll(attempt.device_code)
            except RomMAPIError as exc:
                if exc.code == "network" or (exc.status or 0) >= 500:
                    return self._pending()
                await self._discard(attempt_id, attempt)
                return self._fail(str(exc), exc.code)
            if self._attempts.get(attempt_id) is not attempt:
                return self._fail("Connection cancelled", "cancelled")
            if token is None:
                return self._pending()
            attempt.token = token
            try:
                return await self._validated(attempt_id, attempt)
            except RomMAPIError as exc:
                await self._discard(attempt_id, attempt)
                return self._fail(str(exc), exc.code)
        finally:
            attempt.polling = False

    @staticmethod
    def _pending() -> AuthResult:
        return AuthResult(success=True, action="pending", store=STORE_NAME)

    async def _validated(self, attempt_id: str, attempt: _Attempt) -> AuthResult:
        """Prove the token can list platforms and ROMs, then wait for variant choice."""
        client = RomMClient(attempt.server_url, attempt.token, attempt.verify_ssl)
        variants = eligible_variants(await client.get_json("api/platforms"))
        await self._probe_read_access(client, variants)
        async with self._lock:
            if attempt.cancelled or self._attempts.get(attempt_id) is not attempt:
                return self._fail("Connection cancelled", "cancelled")
            attempt.variants = variants
            attempt.state = "ready"
        return self._variants_result(attempt_id, attempt)

    @staticmethod
    def _variants_result(attempt_id: str, attempt: _Attempt) -> AuthResult:
        return AuthResult(
            success=True, action="select_platforms", tokens_cached=False, store=STORE_NAME,
            metadata={"attempt_id": attempt_id, "variants": attempt.public_variants()},
        )

    async def _probe_read_access(self, client: RomMClient, variants: list[dict[str, Any]]) -> None:
        """Prove ``roms.read`` before accepting a token.

        With no eligible platforms, the probe requests one ROM without a
        platform filter and discards it. Library queries remain restricted
        to supported platforms.
        """
        params = [("limit", "1"), ("offset", "0")]
        params += [("platform_ids", str(v["id"])) for v in variants]
        data = await client.get_json("api/roms", params)
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise RomMAPIError("RomM returned an unexpected ROM list", code="malformed")

    async def complete(self, attempt_id: str, *, excluded_fs_slugs: list[str]) -> AuthResult:
        """Commit the attempt, once the user has chosen platform variants.

        The config is written before the state changes, so a failure cannot
        commit this attempt; a connection saved earlier is left alone.
        Exclusions are checked against the variants this attempt actually
        offered, and completing twice is a no-op, which covers a double click
        on the modal's last button.
        """
        async with self._lock:
            attempt = self._attempts.get(attempt_id)
            if attempt is None or attempt.cancelled:
                return self._fail("Connection attempt not found", "unknown_attempt")
            if attempt.state == "committed":
                return AuthResult(success=True, action="authenticated", tokens_cached=True, store=STORE_NAME)
            if attempt.state != "ready":
                return self._fail("Connection is still being validated", "not_ready")
            known = {v["fs_slug"] for v in attempt.variants}
            excluded = sorted(set(excluded_fs_slugs or []))
            if not set(excluded) <= known:
                return self._fail("Unknown platform variant", "invalid_exclusion")
            cfg: dict[str, Any] = {
                "server_url": attempt.server_url, "token": attempt.token,
                "verify_ssl": attempt.verify_ssl, "excluded_fs_slugs": excluded,
                "device_identifier": attempt.identifier,
            }
            if attempt.download_dir:
                cfg["download_dir"] = attempt.download_dir
            try:
                await asyncio.to_thread(self._write_config, cfg)
            except OSError as exc:
                logger.warning("[RomMAuth] Could not save config: %s", type(exc).__name__)
                self._attempts.pop(attempt_id, None)
                return self._fail("Could not save the RomM connection", "persist_failed")
            self._cfg = cfg
            self._unauthorized = False
            self._committed_by = attempt_id
            attempt.state = "committed"
            for aid in [k for k, a in self._attempts.items() if a.state == "committed" and k != attempt_id]:
                del self._attempts[aid]
        return AuthResult(success=True, action="authenticated", tokens_cached=True, store=STORE_NAME)

    async def cancel(self, attempt_id: str = "") -> Result:
        """Cancel one attempt, or every pending attempt when no ID is given.

        Cancelling the attempt that committed the current connection deletes
        that connection, and reports failure when the config file could not be
        removed. A cancelled pairing that the user approves afterwards leaves a
        device in RomM that nothing here can remove; the late token is
        discarded.
        """
        async with self._lock:
            if not attempt_id:
                for aid, pending in list(self._attempts.items()):
                    if pending.state != "committed":
                        self._cancel_locked(aid, pending)
                return Result(success=True, store=STORE_NAME)
            target = self._attempts.get(attempt_id)
            if target is None:
                self._prune_tombstones()
                self._tombstones[attempt_id] = time.monotonic()
            elif target.state == "committed":
                if self._committed_by == attempt_id and not self._clear_config():
                    return self._fail("Could not remove the saved RomM connection", "config_not_removed")
                self._attempts.pop(attempt_id, None)
            else:
                self._cancel_locked(attempt_id, target)
        return Result(success=True, store=STORE_NAME)

    def _cancel_locked(self, attempt_id: str, attempt: _Attempt) -> None:
        attempt.cancelled = True
        self._tombstones[attempt_id] = time.monotonic()
        self._attempts.pop(attempt_id, None)

    async def logout(self) -> Result:
        async with self._lock:
            for aid, pending in list(self._attempts.items()):
                if pending.state == "committed":
                    self._attempts.pop(aid, None)
                else:
                    self._cancel_locked(aid, pending)
            removed = self._clear_config()
        if not removed:
            # The token is still on disk and would reconnect on restart.
            return self._fail("Could not remove the saved RomM connection", "config_not_removed")
        return Result(success=True, store=STORE_NAME)

    async def shutdown(self) -> None:
        await self.cancel()

    # ── Helpers ────────────────────────────────────────────────────

    async def _discard(self, attempt_id: str, attempt: _Attempt) -> None:
        async with self._lock:
            if self._attempts.get(attempt_id) is attempt:
                del self._attempts[attempt_id]
        attempt.cancelled = True

    @staticmethod
    def _fail(message: str, code: str) -> AuthResult:
        return AuthResult(success=False, error=message, error_code=code, store=STORE_NAME)
