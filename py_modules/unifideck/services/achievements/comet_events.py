"""services/achievements/comet_events.py — comet's achievement push receiver.

Comet (the bundled GOG Galaxy SDK service, 0.4+) can POST a JSON event to a
configured URL the moment a game unlocks an achievement, online or not:

    {"name": "achievement_unlocked", "client_id": "<GOG client id>",
     "data": {<GOG achievement item: achievement_key, name, ...>}}

This module serves that URL on ``127.0.0.1`` (an ephemeral port) and writes
the ``[[custom_event]]`` entry into the comet config the launcher points
comet at (``core.paths.comet_config_file()``). The URL carries a random token
so no other local process can fake an unlock. Each event is handed to
``on_unlock(client_id, data)``; the achievement watcher owns routing and
dedup against its poll.

Everything is best-effort: if the server can't start, the watcher's poll
still delivers the toasts, just later.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from aiohttp import web

from unifideck.core.paths import comet_config_file

logger = logging.getLogger(__name__)

UnlockHandler = Callable[[str, dict[str, Any]], Awaitable[None]]

_EVENT_PATH = "/comet/event"
_ACHIEVEMENT_UNLOCKED = "achievement_unlocked"


class CometEventReceiver:
    """Localhost endpoint for comet's ``achievement_unlocked`` push events."""

    def __init__(
        self, on_unlock: UnlockHandler, config_file: Path | None = None,
    ) -> None:
        """Initialize the instance."""
        self._on_unlock = on_unlock
        self._config_file = config_file or comet_config_file()
        self._token = secrets.token_urlsafe(24)
        self._runner: web.AppRunner | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self.url = ""

    async def start(self) -> None:
        """Serve on an ephemeral localhost port and publish it to comet's config."""
        app = web.Application()
        app.add_routes([web.post(_EVENT_PATH, self._handle)])
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        try:
            await web.TCPSite(runner, "127.0.0.1", 0).start()
            port = runner.addresses[0][1]
            self.url = f"http://127.0.0.1:{port}{_EVENT_PATH}?token={self._token}"
            await asyncio.to_thread(self._write_config)
        except Exception:
            await runner.cleanup()
            raise
        self._runner = runner
        logger.info(
            "[achievements.comet] push receiver on 127.0.0.1:%s, config %s",
            port, self._config_file,
        )

    async def stop(self) -> None:
        """Stop serving and withdraw the config so comet never posts to a dead port."""
        await asyncio.to_thread(self._config_file.unlink, missing_ok=True)
        if self._runner is not None:
            runner, self._runner = self._runner, None
            await runner.cleanup()
        for task in list(self._tasks):
            task.cancel()

    def _write_config(self) -> None:
        """Atomically write comet's ``config.toml`` with our push URL."""
        self._config_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._config_file.with_suffix(".toml.tmp")
        tmp.write_text(
            "# Written by Unifideck: comet posts achievement unlocks here.\n"
            "[[custom_event]]\n"
            "enabled = true\n"
            f'url = "{self.url}"\n',
            encoding="utf-8",
        )
        tmp.replace(self._config_file)

    async def _handle(self, request: web.Request) -> web.Response:
        """Validate one comet POST and hand an unlock to the watcher."""
        token = request.query.get("token", "")
        if not secrets.compare_digest(token, self._token):
            return web.Response(status=403)
        try:
            event = await request.json()
        except ValueError:
            return web.Response(status=400)
        if not isinstance(event, dict):
            return web.Response(status=400)
        data = event.get("data")
        if event.get("name") == _ACHIEVEMENT_UNLOCKED and isinstance(data, dict):
            # Answer comet at once; routing may need a network lookup.
            task = asyncio.create_task(
                self._dispatch(str(event.get("client_id") or ""), data),
            )
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        return web.Response(status=204)

    async def _dispatch(self, client_id: str, data: dict[str, Any]) -> None:
        """Run the unlock handler; a failure here must never reach comet."""
        try:
            await self._on_unlock(client_id, data)
        except Exception:
            logger.debug("[achievements.comet] unlock handler failed", exc_info=True)
