"""Authenticated HTTP access to a RomM server.

Every failure becomes a :class:`RomMAPIError` with a stable code and, for an
HTTP response, the status it carried. Messages never include response bodies or
credentials, because a server error page can echo the Authorization header or
token it was sent.
"""
from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import aiohttp

logger = logging.getLogger(__name__)

_JSON_TIMEOUT = aiohttp.ClientTimeout(total=30)
# No overall deadline for a transfer: a multi-gigabyte archive on a slow link
# is legitimate. A stalled connection is still caught by the socket limits.
_DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=120)
_MAX_REDIRECTS = 5


class RomMAPIError(RuntimeError):
    """A RomM request failed. ``status`` is None for transport failures."""

    def __init__(self, message: str, *, status: int | None = None, code: str = "api_error") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def normalize_server_url(server_url: str) -> str:
    """Return *server_url* without a trailing slash, keeping any proxy path."""
    candidate = (server_url or "").strip()
    parts = urlsplit(candidate)
    if parts.scheme not in ("http", "https") or not parts.netloc or parts.query or parts.fragment:
        raise RomMAPIError("Enter an http:// or https:// server address", code="invalid_url")
    if parts.username or parts.password:
        raise RomMAPIError("Do not put credentials in the server address", code="invalid_url")
    return candidate.rstrip("/")


def _status_error(status: int) -> RomMAPIError:
    if status == 401:
        return RomMAPIError("RomM rejected the credentials", status=status, code="unauthorized")
    if status == 403:
        return RomMAPIError("The RomM token lacks permission for this request", status=status, code="forbidden")
    if status == 404:
        return RomMAPIError("RomM could not find the requested item", status=status, code="not_found")
    return RomMAPIError(f"RomM returned HTTP {status}", status=status, code="http_error")


class RomMClient:
    """HTTP access to one RomM server, with timeouts and a redirect limit."""

    def __init__(self, server_url: str, token: str, verify_ssl: bool = True) -> None:
        self._server_url = normalize_server_url(server_url)
        self._token = token
        self._verify_ssl = verify_ssl

    @property
    def server_url(self) -> str:
        return self._server_url

    def _url(self, path: str) -> str:
        return f"{self._server_url}/{path.lstrip('/')}"

    def _same_origin(self, url: str) -> bool:
        a, b = urlsplit(self._server_url), urlsplit(url)
        return (a.scheme, a.netloc) == (b.scheme, b.netloc)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    def _redirect_target(self, url: str, location: str, *, has_body: bool) -> str:
        target = urljoin(url, location)
        if not location or urlsplit(target).scheme not in ("http", "https"):
            raise RomMAPIError("RomM sent an invalid redirect", code="bad_redirect")
        if has_body and not self._same_origin(target):
            raise RomMAPIError("RomM redirected a pairing request to another server", code="bad_redirect")
        return target

    @contextlib.asynccontextmanager
    async def _session(self) -> AsyncIterator[aiohttp.ClientSession]:
        connector = aiohttp.TCPConnector(ssl=self._verify_ssl)
        try:
            async with aiohttp.ClientSession(connector=connector) as session:
                yield session
        except RomMAPIError:
            raise
        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            raise RomMAPIError(f"Could not reach RomM ({type(exc).__name__})", code="network") from exc

    @contextlib.asynccontextmanager
    async def _open(
        self,
        method: str,
        path: str,
        *,
        params: list[tuple[str, str]] | None = None,
        json_body: Any = None,
        tolerate: frozenset[int] = frozenset(),
        limits: aiohttp.ClientTimeout = _JSON_TIMEOUT,
    ) -> AsyncIterator[aiohttp.ClientResponse]:
        url = self._url(path)
        async with self._session() as session:
            for _ in range(_MAX_REDIRECTS + 1):
                # Credentials are only ever sent to the configured origin.
                same = self._same_origin(url)
                headers = self._headers() if same else {}
                async with session.request(
                    method, url,
                    params=params, json=json_body, headers=headers,
                    timeout=limits, allow_redirects=False,
                ) as resp:
                    if resp.status in (301, 302, 303, 307, 308):
                        url = self._redirect_target(
                            url, resp.headers.get("Location", ""), has_body=json_body is not None,
                        )
                        params = None
                        continue
                    if resp.status >= 400 and resp.status not in tolerate:
                        raise _status_error(resp.status)
                    yield resp
                    return
            raise RomMAPIError("RomM redirected too many times", code="bad_redirect")

    async def get_json(self, path: str, params: list[tuple[str, str]] | None = None) -> Any:
        async with self._open("GET", path, params=params) as resp:
            return await self._read_json(resp)

    async def device_init(
        self, *, identifier: str, name: str, scopes: list[str],
    ) -> dict[str, Any]:
        """Start a device pairing; returns the codes the user must act on."""
        body = {
            "client_device_identifier": identifier, "name": name,
            "client": "unifideck", "platform": "linux", "requested_scopes": scopes,
        }
        async with self._open("POST", "api/auth/device/init", json_body=body) as resp:
            data = await self._read_json(resp)
        fields = ("device_code", "user_code", "verification_path_complete")
        if not isinstance(data, dict) or not all(isinstance(data.get(k), str) and data[k] for k in fields):
            raise RomMAPIError("RomM returned an unexpected pairing response", code="malformed")
        return data

    async def device_poll(self, device_code: str) -> str | None:
        """The access token once the pairing is approved, or None while pending.

        The endpoint reports every non-success state as HTTP 400 with a short
        ``detail`` code; only those fixed codes are interpreted, and the body
        is never echoed.
        """
        async with self._open(
            "POST", "api/auth/device/token",
            json_body={"device_code": device_code}, tolerate=frozenset({400}),
        ) as resp:
            data = await self._read_json(resp)
            status = resp.status
        if status < 400:
            token = data.get("access_token") if isinstance(data, dict) else None
            if not isinstance(token, str) or not token:
                raise RomMAPIError("RomM returned an unexpected token response", code="malformed")
            return token
        detail = data.get("detail") if isinstance(data, dict) else None
        if detail in ("authorization_pending", "slow_down"):
            return None
        if detail == "access_denied":
            raise RomMAPIError("The pairing request was denied in RomM", status=400, code="pairing_denied")
        if detail == "expired_token":
            raise RomMAPIError("The pairing code expired", status=400, code="pairing_expired")
        raise RomMAPIError("RomM rejected the pairing request", status=400, code="pairing_failed")

    @staticmethod
    async def _read_json(resp: aiohttp.ClientResponse) -> Any:
        try:
            return await resp.json(content_type=None)
        except (ValueError, aiohttp.ClientError) as exc:
            raise RomMAPIError("RomM returned an unreadable response", code="malformed") from exc

    @contextlib.asynccontextmanager
    async def open_content(self, rom_id: int, file_name: str) -> AsyncIterator[aiohttp.ClientResponse]:
        """Stream a ROM's complete content (no ``file_ids`` selection)."""
        path = f"api/roms/{int(rom_id)}/content/{quote(file_name, safe='')}"
        async with self._open("GET", path, limits=_DOWNLOAD_TIMEOUT) as resp:
            yield resp
