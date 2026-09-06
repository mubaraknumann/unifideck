"""Amazon Games OAuth — embedded-browser sign-in flow.

Amazon Games requires the user to sign in through Amazon's web SSO
form. ``AmazonAuthFlow`` orchestrates the embedded CDP browser :

* open the Amazon Games OAuth URL with the right query params;
* inject a small script to capture the post-login redirect URL
  containing the auth code;
* exchange the code against access/refresh tokens via the Amazon
  Identity API;
* persist the tokens via the secure token store.

Failure modes (user cancels, network drops, CDP disconnect) are
reported back to the auth facade as ``AuthResult`` envelopes with
explicit error codes. Tokens are refreshed transparently when a
subsequent library/install call detects an expired access token.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, cast

from unifideck.auth.orchestrator import AuthOrchestrator
from unifideck.core.binaries import clean_cli_env
from unifideck.core.types import AuthResult, Events, Result, StoreAuthError
from unifideck.event_bus.event_bus import EventBus
from unifideck.security import audit_auth_flow
from unifideck.stores.amazon.nile_lock import (
    nile_cli_lock,
    quarantine_corrupt_user_file,
)

logger = logging.getLogger(__name__)
_AMAZON_REDIRECT_URIS: list[str] = [
    "https://www.amazon.com/ap/maplanding",
    "https://amazon.com/ap/maplanding",
]

# Strings nile prints to stderr when the user is already
# authenticated. Detected as substrings so minor wording
# changes across nile versions don't break the fast path.
_NILE_ALREADY_AUTHED_MARKERS = (
    "You are already logged in",
    "already authenticated",
)

class _NileProbeRefusedError(StoreAuthError):
    """``nile auth --login`` exited non-zero on the login probe.

    Distinguished from the other probe failures (CLI missing, timeout, bad
    JSON) because it is the one that stale on-disk credentials cause — and
    therefore the only one a logout can repair. See
    :meth:`AmazonAuthFlow._fetch_login_url`.
    """

class AmazonAuthFlow:
    """Amazon auth flow."""

    def __init__(
        self,
        bus: EventBus,
        orchestrator: AuthOrchestrator,
        cli_path: str | None,
        success_markers: list[str],
        cli_timeout_seconds: int = 30,
    ) -> None:
        """Initialize the instance."""
        self._bus = bus
        self._orch = orchestrator
        self._cli_path = cli_path
        self._success_markers = tuple(success_markers)
        self._cli_timeout = cli_timeout_seconds
        self._pending_login: dict[str, Any] | None = None

    @audit_auth_flow(store="amazon", method="oauth_cli")
    async def start_auth(self) -> AuthResult:
        """Start auth."""
        if not self._cli_path:
            return AuthResult(
                success=False,
                error="nile_not_found",
                store="amazon",
            )
        self._pending_login = None
        # Fast path : if nile already has valid credentials,
        # skip the OAuth dance and emit a success event so the
        # frontend updates the badge to "Connected" without
        # opening a browser.
        if await self._is_already_authed():
            logger.info(
                "[amazon_auth] already logged in — skipping OAuth",
            )
            await self._bus.emit(
                Events.STORE_AUTH_COMPLETE, store="amazon",
            )
            return AuthResult(success=True, store="amazon")
        return await self._orch.run_flow(
            get_url=self._fetch_login_url,
            allowed_uris=_AMAZON_REDIRECT_URIS,
            exchange_code=self._register_code,
            background=True,
            write_url_file="~/.local/share/unifideck/amazon_auth_url.txt",
        )

    async def _is_already_authed(self) -> bool:
        """Detect whether ``nile auth --login`` would refuse.

        Runs ``nile auth --login --non-interactive`` and looks
        for "already logged in" markers in the stderr. Nile
        returns a non-zero exit code in that case, with an
        empty stdout — the previous code raised "invalid JSON"
        because it tried to ``json.loads`` the empty buffer.
        """
        assert self._cli_path is not None
        try:
            async with nile_cli_lock():
                proc = await asyncio.create_subprocess_exec(
                    self._cli_path,
                    "auth",
                    "--login",
                    "--non-interactive",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=clean_cli_env(),
                )
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=min(self._cli_timeout, 5),
                )
        except (TimeoutError, OSError) as e:
            logger.warning("[amazon_auth] auth-check failed: %s", e)
            return False
        combined = (
            stderr.decode(errors="ignore")
            + "\n"
            + stdout.decode(errors="ignore")
        )
        return any(
            marker in combined
            for marker in _NILE_ALREADY_AUTHED_MARKERS
        )

    async def _run_nile_logout(self) -> bool:
        """Drop nile's stored credentials. No bus event, no state changes.

        The bare CLI half of :meth:`logout`, reused by the stale-credential
        self-heal in :meth:`_fetch_login_url` — which must NOT announce a
        logout, since it is mid-sign-in and about to authenticate again.

        Returns True when the command ran (so a retry is worth attempting).
        """
        if not self._cli_path:
            return False
        try:
            async with nile_cli_lock():
                proc = await asyncio.create_subprocess_exec(
                    self._cli_path,
                    "auth",
                    "--logout",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=clean_cli_env(),
                )
                await asyncio.wait_for(
                    proc.communicate(),
                    timeout=self._cli_timeout,
                )
        except (TimeoutError, OSError) as e:
            logger.warning("[amazon_auth] logout: %s", e)
            return False
        return True

    async def _clear_nile_credentials(self) -> bool:
        """Get nile back to a signable-in state. True if anything changed.

        Two distinct broken states, and only one of them is nile's to fix:

        * **Stale but parseable** creds — ``nile auth --logout`` clears them.
        * **Corrupt** ``user.json`` — nile cannot help. Every subcommand
          parses that file first, so ``--logout`` dies with the same
          ``JSONDecodeError`` (verified: rc=1, file untouched). We move it
          aside ourselves before nile ever runs.

        Quarantine is attempted first: if the file is unparseable the logout
        would only burn a subprocess to fail.
        """
        if await asyncio.to_thread(quarantine_corrupt_user_file):
            return True
        return await self._run_nile_logout()

    async def logout(self) -> Result:
        """Logout."""
        await self._run_nile_logout()
        self._pending_login = None
        await self._bus.emit(
            Events.STORE_LOGOUT,
            store="amazon",
        )
        return Result(success=True)

    async def _fetch_login_url(self) -> str:
        """Fetch the OAuth URL from nile, clearing stale creds if it refuses.

        The wedge this repairs: ``_is_already_authed`` only reports "logged
        in" when nile prints one of ``_NILE_ALREADY_AUTHED_MARKERS``. When
        nile's stored credentials are present but stale it exits non-zero
        WITHOUT those markers, so we fall through to the real flow — which
        runs the very same command and fails again, surfacing
        ``get_url_failed``. Nothing ever cleared nile's state, leaving the
        user stuck until they wiped every store's cache and signed in again.

        So on a refusal we log nile out once (dropping only its local token,
        no bus event — we are mid-auth and about to sign in again) and retry.
        Only ``_NileProbeRefusedError`` triggers this: a missing CLI, a timeout or
        malformed JSON are not credential problems, and throwing away a
        working token over a transient timeout would cause the very bug we
        are fixing.
        """
        try:
            payload = await self._run_nile_login_probe()
        except _NileProbeRefusedError as refused:
            logger.warning(
                "[amazon_auth] nile refused the login probe (%s) — clearing "
                "its stored credentials and retrying once", refused,
            )
            if not await self._clear_nile_credentials():
                raise
            # A second refusal is the real answer; let it propagate with its
            # nile stderr tail intact.
            payload = await self._run_nile_login_probe()
        url = payload.get("url", "")
        if not url:
            raise StoreAuthError(
                "nile JSON has no 'url' field",
                store="amazon",
            )
        self._pending_login = payload
        pretty = url.split("?", 1)[0] if "?" in url else url
        logger.info("[amazon_auth] received login URL from nile: %s", pretty)
        return cast("str", url)

    async def _run_nile_login_probe(self) -> dict[str, Any]:
        """Run NILE login probe."""
        if self._cli_path is None:
            raise StoreAuthError(
                "nile CLI not resolved",
                store="amazon",
            )
        try:
            async with nile_cli_lock():
                proc = await asyncio.create_subprocess_exec(
                    self._cli_path,
                    "auth",
                    "--login",
                    "--non-interactive",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=clean_cli_env(),
                )
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=self._cli_timeout,
                )
        except TimeoutError as e:
            raise StoreAuthError(
                f"nile auth timed out after {self._cli_timeout}s",
                store="amazon",
            ) from e
        if proc.returncode != 0:
            err = stderr.decode(errors="ignore") or stdout.decode(errors="ignore")
            raise _NileProbeRefusedError(
                f"nile auth failed (rc={proc.returncode}): {err[:200]}",
                store="amazon",
            )
        try:
            return cast("dict[Any, Any]", json.loads(stdout.decode(errors="ignore")))
        except json.JSONDecodeError as e:
            raise StoreAuthError(
                f"nile returned invalid JSON: {e}",
                store="amazon",
            ) from e

    async def _register_code(self, code: str) -> AuthResult:
        """Register code."""
        if self._pending_login is None:
            return AuthResult(
                success=False,
                error="no_pending_login",
                store="amazon",
            )
        login = self._pending_login
        args = [
            "register",
            "--code",
            code,
            "--code-verifier",
            login.get("code_verifier", ""),
            "--serial",
            login.get("serial", ""),
            "--client-id",
            login.get("client_id", ""),
        ]
        if self._cli_path is None:
            return AuthResult(
                success=False,
                error="nile_not_found",
                store="amazon",
            )
        proc = await asyncio.create_subprocess_exec(
            self._cli_path,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=clean_cli_env(),
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=self._cli_timeout,
            )
        except TimeoutError:
            return AuthResult(
                success=False,
                error="register_timeout",
                store="amazon",
            )
        combined = (
            stderr.decode(errors="ignore") + "\n" + stdout.decode(errors="ignore")
        )
        if any(m in combined for m in self._success_markers):
            self._pending_login = None
            logger.info(
                "[amazon_auth] nile register succeeded",
            )
            return AuthResult(success=True, store="amazon")
        logger.warning(
            "[amazon_auth] nile register failed: %s",
            combined[:200],
        )
        return AuthResult(
            success=False,
            error="register_failed",
            store="amazon",
        )
