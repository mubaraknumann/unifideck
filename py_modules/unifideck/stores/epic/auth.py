"""Epic Games OAuth — embedded-browser sign-in flow.

Epic Games requires the user to sign in through the Epic Games web
login. ``EpicAuthFlow`` orchestrates the embedded CDP browser :

* open the Epic OAuth URL with the right query params;
* inject a small script to capture the SID (session identifier)
  produced by Epic after a successful sign-in;
* exchange the SID against access/refresh tokens via Epic's account
  API;
* persist the tokens via the secure token store and forward them
  to ``legendary`` (which keeps its own credentials file separately).

Public methods cover the full lifecycle :

* ``start_auth(progress_cb)``  — open the browser and wait for SID;
* ``exchange_sid(sid)``        — convert SID → tokens;
* ``refresh_if_stale()``       — refresh expired access tokens;
* ``logout()``                 — wipe tokens locally and through
  legendary.

Failure modes (user cancels, network drops, CDP disconnect) are
reported back as ``AuthResult`` envelopes with explicit error codes.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from unifideck.auth.orchestrator import AuthOrchestrator
from unifideck.core.binaries import clean_cli_env
from unifideck.core.types import AuthResult, Events, Result, StoreAuthError
from unifideck.event_bus.event_bus import EventBus
from unifideck.security import audit_auth_flow

logger = logging.getLogger(__name__)

# Only the post-login callback URIs — the URLs that legendary's
# local OAuth redirect server listens on AFTER the user signs in.
# ``www.epicgames.com/id/api/redirect`` is intentionally NOT listed —
# it's the initial OAuth authorize endpoint that the browser hits
# BEFORE the login form even appears; matching it would capture the
# redirect before the user has a chance to sign in, yielding a URL
# with no ``code`` parameter and killing the flow prematurely.
_EPIC_REDIRECT_URIS: list[str] = [
    "https://legendary.epicgames.com/callback",
]

# legendary's `auth` command emits the OAuth URL using its own
# short-link domain (``https://legendary.gl/epiclogin``), which
# redirects to ``epicgames.com``. Accept both so we don't drop
# the URL on the floor and time out the flow.
_AUTH_URL_MARKERS = ("epicgames.com", "legendary.gl")

# Lines `legendary auth` prints when the user is already logged
# in. We detect either marker, terminate the process, and emit
# STORE_AUTH_COMPLETE without showing a browser. Matching is
# substring-based so minor wording changes in future legendary
# versions don't break detection.
_LEGENDARY_ALREADY_AUTHED_MARKERS = (
    "Stored credentials are still valid",
    "Login successful",
)

class EpicAuthFlow:
    """Epic auth flow."""

    def __init__(
        self,
        bus: EventBus,
        orchestrator: AuthOrchestrator,
        cli_path: str | None,
        cli_timeout_seconds: int = 30,
    ) -> None:
        """Initialize the instance."""
        self._bus = bus
        self._orch = orchestrator
        self._cli_path = cli_path
        self._cli_timeout = cli_timeout_seconds

    @audit_auth_flow(store="epic", method="oauth_cli")
    async def start_auth(self) -> AuthResult:
        """Start auth."""
        if not self._cli_path:
            return AuthResult(
                success=False,
                error="legendary_not_found",
                store="epic",
            )
        # Fast path : if legendary already has valid credentials,
        # skip the entire OAuth dance and emit a success event
        # so the frontend updates the badge to "Connected" without
        # opening a browser.
        if await self._is_already_authed():
            logger.info(
                "[epic_auth] credentials still valid — skipping OAuth",
            )
            await self._bus.emit(
                Events.STORE_AUTH_COMPLETE, store="epic",
            )
            return AuthResult(success=True, store="epic")
        return await self._orch.run_flow(
            get_url=self._fetch_login_url,
            allowed_uris=_EPIC_REDIRECT_URIS,
            exchange_code=self._register_code,
            background=True,
            write_url_file=("~/.local/share/unifideck/epic_auth_url.txt"),
            # Content-based fallback: Epic sometimes embeds the
            # ``authorizationCode`` in a JSON blob inside the
            # intermediate ``/id/api/redirect`` page body.
            # If URL-param matching misses the code, this
            # regex extracts it from ``document.body.innerText``.
            content_trigger_url="epicgames.com/id/api/redirect",
            content_regex=r'"authorizationCode"\s*:\s*"([^"]+)"',
        )

    async def _is_already_authed(self) -> bool:
        """Detect whether ``legendary auth`` would no-op.

        Runs ``legendary auth`` (no args), reads up to a few
        lines of stdout, and looks for the
        "credentials still valid" marker. Returns False on any
        I/O / timeout error so the caller falls back to the
        normal OAuth flow.
        """
        assert self._cli_path is not None
        try:
            proc = await asyncio.create_subprocess_exec(
                self._cli_path,
                "auth",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=clean_cli_env(),
            )
        except OSError as e:
            logger.warning("[epic_auth] spawn for auth-check failed: %s", e)
            return False
        try:
            try:
                stdout_bytes, _ = await asyncio.wait_for(
                    proc.communicate(),
                    # legendary's auth-check completes in <1s when
                    # creds are present ; cap so we never hang.
                    timeout=min(self._cli_timeout, 5),
                )
            except TimeoutError:
                return False
            text = stdout_bytes.decode(errors="ignore")
            return any(
                marker in text
                for marker in _LEGENDARY_ALREADY_AUTHED_MARKERS
            )
        finally:
            if proc.returncode is None:
                await self._terminate_legendary(proc)

    async def logout(self) -> Result:
        """Logout."""
        if not self._cli_path:
            await self._bus.emit(
                Events.STORE_LOGOUT,
                store="epic",
            )
            return Result(success=True)
        try:
            proc = await asyncio.create_subprocess_exec(
                self._cli_path,
                "auth",
                "--delete",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=clean_cli_env(),
            )
            await asyncio.wait_for(
                proc.communicate(),
                timeout=self._cli_timeout,
            )
        except (TimeoutError, OSError) as e:
            logger.warning("[epic_auth] logout: %s", e)
        await self._bus.emit(
            Events.STORE_LOGOUT,
            store="epic",
        )
        return Result(success=True)

    async def _fetch_login_url(self) -> str:
        """Fetch login URL."""
        proc = await self._spawn_legendary_auth()
        try:
            url = await self._scrape_url_from_proc(proc)
        finally:
            await self._terminate_legendary(proc)
        if not url:
            raise StoreAuthError(
                "no OAuth URL found in legendary auth output",
                store="epic",
            )
        pretty = url.split("?", 1)[0] if "?" in url else url
        logger.info("[epic_auth] captured URL from legendary: %s", pretty)
        return url

    async def _spawn_legendary_auth(self) -> Any:
        """Spawn LEGENDARY auth."""
        assert self._cli_path is not None, (
            "_spawn_legendary_auth called before CLI is resolved"
        )
        return await asyncio.create_subprocess_exec(
            self._cli_path,
            "auth",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=clean_cli_env(),
        )

    async def _scrape_url_from_proc(self, proc: Any) -> str | None:
        """Scrape URL from proc."""
        assert proc.stdout is not None
        deadline = asyncio.get_event_loop().time() + self._cli_timeout
        while asyncio.get_event_loop().time() < deadline:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                return None
            try:
                line_bytes = await asyncio.wait_for(
                    proc.stdout.readline(),
                    timeout=remaining,
                )
            except TimeoutError:
                return None
            if not line_bytes:
                return None
            text = line_bytes.decode(errors="ignore").strip()
            url = self._extract_url(text)
            if url:
                return url
        return None

    @staticmethod
    async def _terminate_legendary(proc: Any) -> None:
        """Terminate LEGENDARY."""
        # Legendary crashes fast on EOF (stdin is closed under
        # the Decky service), so by the time we get here the
        # process may already be dead. asyncio's
        # ``Process.terminate()`` raises a bare
        # ``ProcessLookupError()`` (empty str) in that case ;
        # left uncaught, it propagates through the ``finally``
        # in ``_fetch_login_url`` and replaces the real
        # ``StoreAuthError`` with a useless empty-message log.
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=2)
        except TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
        except ProcessLookupError:
            pass

    async def _register_code(self, code: str) -> AuthResult:
        """Register code."""
        assert self._cli_path is not None, (
            "_register_code called before CLI is resolved"
        )
        proc = await asyncio.create_subprocess_exec(
            self._cli_path,
            "auth",
            "--code",
            code,
            stdin=asyncio.subprocess.DEVNULL,
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
                store="epic",
            )
        if proc.returncode == 0:
            logger.info("[epic_auth] legendary register succeeded")
            return AuthResult(success=True, store="epic")
        err = (stderr.decode(errors="ignore") or stdout.decode(errors="ignore"))[:200]
        logger.warning("[epic_auth] register failed: %s", err)
        return AuthResult(
            success=False,
            error="register_failed",
            store="epic",
        )

    @staticmethod
    def _extract_url(line: str) -> str | None:
        """Extract URL."""
        if "https://" not in line:
            return None
        for token in line.split():
            if not token.startswith("https://"):
                continue
            if any(m in token for m in _AUTH_URL_MARKERS):
                return token
        return None
