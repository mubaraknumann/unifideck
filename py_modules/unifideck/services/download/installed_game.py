"""Composing the ``Game`` record for a freshly-installed download.

py_modules/unifideck/services/download/installed_game.py

Split out of ``worker.py`` when that file reached its 550-line cap. It
earns its own module rather than an allowlist entry: nothing here touches
the queue, the running map or the event bus, so it is the one part of the
worker that is pure composition and testable without a service around it.

The record it returns is consumed by
``ShortcutService._on_download_complete``, which flips the existing
shortcut's install tag and writes the ``games.map`` row. Artwork is NOT
fetched from here — the shortcut was created (with its cover) during the
library sync, and ``mark_installed`` preserves its appid, so the art is
already in place by the time an install finishes.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from unifideck.core.types import Game

if TYPE_CHECKING:
    from unifideck.core.types import InstallResult
    from unifideck.stores import StoreBase

    from .models import DownloadItem

logger = logging.getLogger(__name__)


async def _resolve_exe(store: StoreBase, install_path: str, game_id: str) -> str | None:
    """The game's executable, store-specific resolver first.

    Falls back to the cross-store ``StoreBase._find_exe`` heuristic, which
    inside a vendor install directory is as likely to pick a launcher or a
    crash handler as the game — hence the preference order.

    Never raises: a missing exe costs a shortcut its target, while an
    exception here would abandon an install that actually succeeded.
    """
    try:
        specific = getattr(store, "find_installed_exe", None)
        if callable(specific):
            # Pass game_id too — store-specific resolvers (Epic's
            # legendary-manifest ``launch_exe`` lookup) need it; the generic
            # ones accept it as an ignored optional arg.
            maybe: Any = specific(install_path, game_id)
            if asyncio.iscoroutine(maybe):
                maybe = await maybe
            return maybe if isinstance(maybe, str) else None
        if hasattr(store, "_find_exe"):
            raw: Any = store._find_exe(install_path)
            return raw if isinstance(raw, str) else None
    except Exception:
        logger.exception(
            "[DownloadWorker] exe resolution failed for %s — leaving null",
            install_path,
        )
    return None


async def build_installed_game(
    item: DownloadItem,
    result: InstallResult,
    store: StoreBase,
    launcher_path: str,
) -> Game | None:
    """Compose a ``Game`` for a freshly-installed item.

    Returns ``None`` when no install path can be derived — the downstream
    listeners then no-op safely rather than writing a shortcut that points
    nowhere.
    """
    install_path = item.install_path or getattr(result, "install_path", None)
    if not install_path:
        logger.warning(
            "[DownloadWorker] cannot build Game for %s:%s — no install_path",
            item.store,
            item.game_id,
        )
        return None

    exe_path = await _resolve_exe(store, install_path, item.game_id)

    # Title fallback: stored on the item; if missing, derive from the install
    # folder name so the shortcut tile reads sensibly.
    title = item.title or Path(install_path).name or item.game_id

    # Cheap: InstallResult carries the size when it knows it.
    size_bytes = int(getattr(result, "size_bytes", 0) or 0)

    # The real launcher-anchored app_id, so the frontend's DOWNLOAD_COMPLETE
    # handler invalidates the right cache entry. Same (launcher, store:game_id)
    # formula as ``SyncService._populate_app_ids`` — no drift possible.
    from unifideck.services.shortcut.games_map import generate_app_id

    computed_app_id = (
        generate_app_id(launcher_path, f"{item.store}:{item.game_id}")
        if launcher_path
        else 0
    )

    return Game(
        app_id=computed_app_id,
        store=item.store,
        store_game_id=item.game_id,
        title=title,
        installed=True,
        install_path=install_path,
        exe_path=exe_path,
        size_bytes=size_bytes,
    )
