"""The sign-in URL file: how the backend tells the auth window what to open.

A store's sign-in runs in a window the launcher opens from a temporary
Steam shortcut. The launcher (``launcher/flows/auth.py``) reads the URL
from a file the backend wrote, so the file must never be read half-written.

Two writers exist: the browser-redirect flow (``AuthOrchestrator``) and the
Microsoft device-code flow, which writes ``microsoft.com/link?otc=…``. One
helper, so the atomic-write rule cannot drift between them.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


async def write_url_file_atomically(path: str, url: str) -> bool:
    """Write ``url`` to ``path`` via a ``.tmp`` sibling and a rename."""
    def _write_sync() -> str:
        expanded = Path(path).expanduser()
        expanded.parent.mkdir(parents=True, exist_ok=True)
        tmp = expanded.with_name(expanded.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            f.write(url)
        tmp.replace(expanded)
        return str(expanded)

    # Logged on failure only; the un-expanded path is just as diagnostic and
    # expanding it here would be a blocking pathlib call (ASYNC240).
    target = path
    try:
        target = await asyncio.to_thread(_write_sync)
        logger.debug("[AuthURLFile] wrote auth URL to %s", target)
        return True
    except OSError:
        logger.exception("[AuthURLFile] failed to write %s", target)
        return False


async def remove_url_file(path: str) -> None:
    """Delete the URL file if present, so a stale URL is never reopened."""
    def _remove_sync() -> None:
        Path(path).expanduser().unlink(missing_ok=True)

    try:
        await asyncio.to_thread(_remove_sync)
    except OSError as e:
        logger.warning("[AuthURLFile] could not remove %s: %s", path, e)
