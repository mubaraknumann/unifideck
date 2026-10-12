"""Scripted RomM server for tests, installed in place of ``aiohttp``'s session.

Handlers are matched by method and path; each call is recorded so tests can
assert exactly what was sent (headers, query, body, TLS setting). No real
tokens appear here: fixtures use obviously fake values.
"""
from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import aiohttp
import pytest


@dataclass
class Reply:
    status: int = 200
    json: Any = None
    chunks: list[bytes] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)
    # Raised from the body stream after the chunks, to model a dropped connection.
    stream_error: Exception | None = None


@dataclass
class Call:
    method: str
    url: str
    path: str
    params: dict[str, list[str]]
    json: Any
    headers: dict[str, str]
    basic: tuple[str, str] | None
    ssl: Any


class _Content:
    def __init__(self, chunks: list[bytes], error: Exception | None = None) -> None:
        self._chunks = chunks
        self._error = error

    async def iter_chunked(self, _size: int):
        for chunk in self._chunks:
            yield chunk
        if self._error is not None:
            raise self._error


class _Response:
    def __init__(self, reply: Reply) -> None:
        self.status = reply.status
        self.headers = reply.headers
        self._reply = reply
        self.content = _Content(reply.chunks, reply.stream_error)
        length = reply.headers.get("Content-Length")
        self.content_length = int(length) if length else None

    async def json(self, content_type: str | None = None) -> Any:
        if isinstance(self._reply.json, Exception):
            raise self._reply.json
        return self._reply.json


Handler = Callable[[Call], "Reply | Awaitable[Reply]"]


class FakeRomM:
    def __init__(self) -> None:
        self.calls: list[Call] = []
        self._routes: list[tuple[str, str, Handler]] = []
        self.ssl_settings: list[Any] = []

    def on(self, method: str, path: str, handler: Handler | Reply | Exception) -> None:
        if isinstance(handler, Reply):
            reply = handler
            handler = lambda _call: reply  # noqa: E731
        elif isinstance(handler, Exception):
            exc = handler

            def handler(_call: Call) -> Reply:
                raise exc
        self._routes.append((method, path, handler))  # type: ignore[arg-type]

    def calls_to(self, method: str, path: str) -> list[Call]:
        return [c for c in self.calls if c.method == method and c.path == path]

    async def handle(self, call: Call) -> _Response:
        self.calls.append(call)
        for method, path, handler in self._routes:
            if method == call.method and path == call.path:
                result = handler(call)
                if asyncio.iscoroutine(result):
                    result = await result
                return _Response(result)  # type: ignore[arg-type]
        raise AssertionError(f"unscripted RomM request: {call.method} {call.path}")


class _Request:
    def __init__(self, server: FakeRomM, call: Call) -> None:
        self._server = server
        self._call = call

    async def __aenter__(self) -> _Response:
        return await self._server.handle(self._call)

    async def __aexit__(self, *exc: object) -> None:
        return None


def _make_session(server: FakeRomM):
    class Session:
        def __init__(self, connector: Any = None, **_kw: Any) -> None:
            self._ssl = getattr(connector, "ssl", None)

        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        def request(self, method: str, url: str, *, params=None, json=None,
                    headers=None, auth=None, **_kw: Any) -> _Request:
            parts = urlsplit(url)
            query: dict[str, list[str]] = {}
            for key, value in params or []:
                query.setdefault(key, []).append(str(value))
            header = (headers or {}).get("Authorization", "")
            basic = None
            if header.startswith("Basic "):
                user, _, password = base64.b64decode(header[6:]).decode().partition(":")
                basic = (user, password)
            return _Request(server, Call(
                method, url, parts.path, query, json, dict(headers or {}), basic, self._ssl,
            ))

    return Session


class _Connector:
    def __init__(self, *, ssl: Any = True, **_kw: Any) -> None:
        self.ssl = ssl


@pytest.fixture
def romm_server(monkeypatch) -> FakeRomM:
    server = FakeRomM()
    monkeypatch.setattr(aiohttp, "ClientSession", _make_session(server))
    monkeypatch.setattr(aiohttp, "TCPConnector", _Connector)
    return server


def bearer(call: Call) -> str:
    return call.headers.get("Authorization", "").removeprefix("Bearer ")


def basic_header(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
