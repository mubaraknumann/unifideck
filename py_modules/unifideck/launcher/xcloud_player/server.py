"""The player's local server: serves the page and makes every xCloud call.

Bound to 127.0.0.1 on a free port for one stream. The page asks it to start
the session, reports its state, and passes its WebRTC offer and candidates
through; tokens never leave this process. Failures reach the page as a
message the user sees (the page shows ``message``), never only a log line.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from aiohttp import web

from .api import XcloudApi, sdp_body

logger = logging.getLogger(__name__)
WEB = Path(__file__).resolve().parent / "web"
_STATE_MESSAGES = {
    "Failed": "Xbox Cloud Gaming could not start this game. Try again in a moment.",
}


class PlayerServer:
    """One stream's page + API, on ``http://127.0.0.1:<port>/``."""

    def __init__(self, api: XcloudApi, title_id: str, ticket: Callable[[], str]) -> None:
        self._api = api
        self._title_id = title_id
        self._ticket = ticket
        self._session = ""
        self._runner: web.AppRunner | None = None
        self.url = ""

    async def start(self) -> str:
        """Serve; returns the page URL."""
        app = web.Application()
        app.add_routes([
            web.get("/", self._file("index.html")),
            web.get("/bundle.js", self._file("bundle.js")),
            web.post("/api/start", self._start), web.get("/api/state", self._state),
            web.post("/api/connect", self._connect), web.post("/api/sdp", self._sdp),
            web.post("/api/ice", self._ice), web.post("/api/keepalive", self._keepalive),
            web.post("/api/log", self._log),
        ])
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = self._runner.addresses[0][1]
        self.url = f"http://127.0.0.1:{port}/"
        return self.url

    async def stop(self) -> None:
        """End the cloud session, then stop serving. Never raises."""
        if self._session:
            session, self._session = self._session, ""
            status, _ = await asyncio.to_thread(self._api.call, "DELETE", "/" + session)
            logger.info("[xcloud_player] session ended -> HTTP %s", status)
        if self._runner is not None:
            runner, self._runner = self._runner, None
            await runner.cleanup()

    @staticmethod
    def _file(name: str) -> Callable[[web.Request], Any]:
        async def handler(_req: web.Request) -> web.FileResponse:
            return web.FileResponse(WEB / name)
        return handler

    @staticmethod
    def _error(message: str, status: int = 502) -> web.Response:
        logger.warning("[xcloud_player] %s", message)
        return web.json_response({"message": message}, status=status)

    async def _start(self, _req: web.Request) -> web.Response:
        self._session = await asyncio.to_thread(self._api.start, self._title_id)
        if not self._session:
            return self._error("Xbox Cloud Gaming would not start this game. "
                               "Check your Game Pass subscription and try again.")
        return web.json_response({"ok": True})

    async def _state(self, _req: web.Request) -> web.Response:
        status, body = await asyncio.to_thread(self._api.call, "GET", f"/{self._session}/state")
        state = str(body.get("state") or "")
        if state == "Failed":
            logger.warning("[xcloud_player] session failed: %s", body.get("errorDetails"))
        return web.json_response({"state": state, "message": _STATE_MESSAGES.get(state, "")},
                                 status=200 if status else 502)

    async def _connect(self, _req: web.Request) -> web.Response:
        ticket = await asyncio.to_thread(self._ticket)
        if not ticket:
            return self._error("Your Microsoft sign-in could not start the stream. "
                               "Sign in to Microsoft again from the Unifideck menu.")
        status, _ = await asyncio.to_thread(
            self._api.call, "POST", f"/{self._session}/connect", {"userToken": ticket})
        logger.info("[xcloud_player] connect -> HTTP %s", status)
        return web.json_response({"status": status})

    async def _exchange(self, kind: str, body: dict[str, Any]) -> web.Response:
        answer = await asyncio.to_thread(self._api.exchange, self._session, kind, body)
        if not answer:
            return self._error("The stream could not connect. Try again.", 504)
        return web.json_response(answer)

    async def _sdp(self, req: web.Request) -> web.Response:
        return await self._exchange("sdp", sdp_body(str((await req.json()).get("sdp", ""))))

    async def _ice(self, req: web.Request) -> web.Response:
        return await self._exchange("ice", {"candidates": (await req.json()).get("candidates", [])})

    async def _keepalive(self, _req: web.Request) -> web.Response:
        status, _ = await asyncio.to_thread(
            self._api.call, "POST", f"/{self._session}/keepalive", {})
        return web.json_response({"status": status})

    @staticmethod
    async def _log(req: web.Request) -> web.Response:
        logger.info("[xcloud_player] page: %s", (await req.text())[:300])
        return web.json_response({})
