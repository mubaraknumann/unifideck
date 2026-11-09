"""stores/microsoft/xodus/ticket.py — the Microsoft ticket behind the one sign-in.

With Xbox PC downloads on, the Xodus sign-in is the only Microsoft sign-in.
Unifideck's Xbox Live chain (xCloud library, Game Pass, ownership) still
starts from a Microsoft account ticket; this module gets that ticket from
the Xodus credential instead of from an OAuth sign-in.

``xodus-service`` answers ``MSA_TOKEN_REQUEST`` the way Windows answers a
game: with full trust it issues a ``service::user.auth.xboxlive.com::MBI_SSL``
ticket, which ``user.auth.xboxlive.com`` accepts as ``t=<ticket>``. It needs
a Microsoft account app id; our Azure AD app id is refused
(``missing field wst:TokenType``), the Xbox app's id works (measured
2026-10-21: ticket, Xbox Live user token, both XSTS tokens and the xCloud
login, tier ultimate). Tickets last about 24 hours and are kept in memory
only, never written to disk.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from unifideck.launcher.xbox_pc import xml_lite
from unifideck.launcher.xbox_pc.layout import credential_dir

from .update_service import _HEADER, XML_MAGIC, ServiceError, service_session

if TYPE_CHECKING:
    from .cli import XodusCli

logger = logging.getLogger(__name__)

#: The Xbox app's Microsoft account app id (a public client id).
XBOX_APP_CLIENT_ID = "000000004C12AE6F"
MSA_TOKEN_REQUEST = 3
MSA_TOKEN_RESPONSE = 4
#: Get a new ticket when the cached one has less than this left.
REFRESH_MARGIN_S = 3600.0
#: How long to wait for a download to free the credentials.
LOCK_WAIT_S = 3.0
REPLY_TIMEOUT_S = 60.0
_LOG = "[XboxPcTicket]"


def _request() -> bytes:
    body = (
        f"<Request><ClientId>{XBOX_APP_CLIENT_ID}</ClientId>"
        "<AllowUi>false</AllowUi><MSAFullTrust>true</MSAFullTrust></Request>"
    ).encode()
    return _HEADER.pack(XML_MAGIC, MSA_TOKEN_REQUEST, len(body)) + body


def parse_reply(body: bytes) -> tuple[str, float] | None:
    """``(ticket, expires_at)`` from a ``MSA_TOKEN_RESPONSE`` body, or None."""
    if not body:
        return None
    try:
        root = xml_lite.fromstring(body.decode())
    except ValueError:
        return None
    token = root.find("Token")
    expiry = root.find("Expiry")
    ticket = (token.text or "").strip() if token is not None else ""
    try:
        expires_at = float((expiry.text or "").strip()) if expiry is not None else 0.0
    except ValueError:
        expires_at = 0.0
    return (ticket, expires_at) if ticket and expires_at > time.time() else None


async def _ask(socket_path: str) -> bytes:
    reader, writer = await asyncio.open_unix_connection(socket_path)
    try:
        writer.write(_request())
        await writer.drain()
        magic, kind, size = _HEADER.unpack(await reader.readexactly(_HEADER.size))
        if magic != XML_MAGIC or kind != MSA_TOKEN_RESPONSE:
            raise ServiceError(f"unexpected reply magic={magic:#x} type={kind}")
        return await reader.readexactly(size)
    finally:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()


class XodusTicket:
    """Microsoft tickets from the Xodus credential, cached until near expiry."""

    def __init__(self, cli: XodusCli, enabled: Callable[[], bool]) -> None:
        self._cli = cli
        self._enabled = enabled
        self._cached: tuple[str, float] | None = None
        self._lock = asyncio.Lock()
        #: Why the last attempt gave no ticket, or "".
        self.last_error = ""

    def active(self) -> bool:
        """Whether the one sign-in applies: Xbox PC on and Xodus signed in."""
        return self._enabled() and (credential_dir() / "user-session").is_file()

    def forget(self) -> None:
        """Drop the cached ticket (sign-out, account change)."""
        self._cached = None

    async def get(self) -> str | None:
        """A ticket with at least :data:`REFRESH_MARGIN_S` left, or None."""
        async with self._lock:
            if self._fresh(REFRESH_MARGIN_S):
                return self._cached[0] if self._cached else None
            minted = await self._mint_locked()
            if minted is not None:
                self._cached = minted
            elif self._fresh(0.0):
                logger.warning("%s refresh failed (%s); using the cached ticket", _LOG, self.last_error)
            return self._cached[0] if self._fresh(0.0) and self._cached else None

    def _fresh(self, margin: float) -> bool:
        return self._cached is not None and self._cached[1] - margin > time.time()

    async def _mint_locked(self) -> tuple[str, float] | None:
        """Mint under the credential lock. A download that holds it for long
        only delays the refresh while the cached ticket is still valid."""
        lock = self._cli.credential_lock
        try:
            await asyncio.wait_for(lock.acquire(), LOCK_WAIT_S)
        except TimeoutError:
            if self._fresh(0.0):
                self.last_error = "busy"
                return None
            logger.info("%s a download holds the credentials; asking the service anyway", _LOG)
            return await self._mint()
        try:
            return await self._mint()
        finally:
            lock.release()

    async def _mint(self) -> tuple[str, float] | None:
        try:
            paths = await self._cli.backend()
            async with service_session(paths.service, self._cli.credential_dir()) as sock:
                body = await asyncio.wait_for(_ask(sock), REPLY_TIMEOUT_S)
        except (ServiceError, OSError, TimeoutError, asyncio.IncompleteReadError) as e:
            self.last_error = f"service: {type(e).__name__}"
            logger.warning("%s no ticket: %s", _LOG, e)
            return None
        except Exception as e:
            self.last_error = type(e).__name__
            logger.warning("%s no ticket: %s", _LOG, type(e).__name__)
            return None
        ticket = parse_reply(body)
        self.last_error = "" if ticket else "empty_reply"
        if ticket is None:
            logger.warning("%s the service issued no ticket", _LOG)
        return ticket
