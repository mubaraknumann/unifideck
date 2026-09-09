"""UI RPC mixin for Plugin class.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from unifideck.rpc.errors import RpcError
from unifideck.rpc.mixins import _metadata_display as _mdisp
from unifideck.utils.device import detect_device_type

logger = logging.getLogger(__name__)

def _resolve_user_path(path: str) -> str:
    """Expand ``~`` and resolve symlinks. Blocking — wrap with to_thread.

    Returns the canonical absolute path. Empty/None input
    falls back to ``/`` so the caller always gets a real path
    to test for ``is_dir``.
    """
    return str(Path(path or "/").expanduser().resolve())

def _collect_subdirs(
    resolved: str, show_hidden: bool, sort_by: str,
) -> list[str]:
    """Return the immediate subdirectory names of ``resolved``.

    Pure synchronous I/O helper extracted from
    ``list_directory`` to:

    * keep the async method under the nesting=4 gate (the
      scandir-loop-isdir branch was nesting=5);
    * make the blocking work atomic so a single
      ``asyncio.to_thread`` call wraps all the filesystem
      touches at once, rather than scattering ``to_thread``
      calls over each ``is_dir`` check.

    Skips dotfiles unless ``show_hidden`` is True. Each
    entry's ``is_dir`` is guarded against transient OSError
    (broken symlink, race with concurrent rm) — that entry
    is dropped silently. Caller handles directory-level
    OSError / PermissionError.
    """
    entries: list[str] = []
    with os.scandir(resolved) as it:
        for entry in it:
            if not show_hidden and entry.name.startswith("."):
                continue
            if _is_dir_safe(entry):
                entries.append(entry.name)
    if sort_by == "name":
        entries.sort(key=str.lower)
    return entries

def _is_dir_safe(entry: os.DirEntry[str]) -> bool:
    """Return True iff ``entry`` is a directory; False on any OSError.

    Tiny wrapper that swallows transient errors (broken
    symlink, race with rm) so the caller's loop doesn't
    need its own try/except — which kept the nesting depth
    of ``list_directory`` past the gate.
    """
    try:
        return entry.is_dir(follow_symlinks=False)
    except OSError:
        return False

class UIRPCMixin:
    """Game-metadata display, language preferences, and directory browsing.

    Two routes were removed as dead (audit §1.2): ``get_game_metadata``,
    superseded by the appid-keyed :meth:`get_game_metadata_display` (which
    does strictly more and avoids a linear scan of every known game), and
    ``inject_hide_css``, superseded by the frontend's own scoped-CSS
    marker. The latter was the sole reachable path into
    ``cdp/cdp_inject.py``, which went with it.
    """

    config: Any
    services: Any
    sync_service: Any  # Required for the metadata.enrich(game) lookup

    cache: Any

    async def get_game_metadata_display(
        self, app_id: int,
    ) -> dict[str, Any] | None:
        """Build the panel's ``GameMetadata`` payload for ``app_id``.

        Looks up the shortcut's ``Game`` via sync, enriches via
        ``MetadataService``, overlays the cached Steam appdetails
        + compat-cache entry, and returns the merged dict. ``None``
        when the shortcut isn't in the sync cache.
        """
        sync = getattr(self, "sync_service", None)
        if sync is None:
            raise RpcError("service_unavailable", service="sync_service")
        info = sync.get_game_info(app_id)
        if not info:
            return None
        game = _mdisp.build_game_from_info(info, app_id)
        metadata = getattr(self.services, "metadata", None)
        enriched = await _mdisp.safe_enrich(metadata, game, app_id)
        steam_app_id, steam_meta = await _mdisp.resolve_steam_payload(
            self.cache, metadata, game, app_id,
        )
        compat_entry = _mdisp.read_compat_entry(
            self.cache, app_id, steam_app_id=steam_app_id,
        )
        return _mdisp.build_payload(
            game, enriched, steam_app_id, steam_meta, compat_entry,
        )

    async def get_language_preference(self) -> Any:
        """Return the current UI locale preference.

        ``locale`` is the stored *preference*: the ``"auto"``
        sentinel (follow the system/UI language) or a concrete
        BCP-47 tag. Defaults to ``"auto"`` so a fresh install
        auto-detects rather than forcing English.
        """
        return {"success": True, "locale": self.config.get("ui.locale", "auto")}

    async def set_language_preference(self, locale: str) -> Any:
        """Persist the UI locale via config."""
        self.config.set("ui.locale", locale)
        return {"success": True, "locale": locale}

    async def get_device_type(self) -> Any:
        """Return the hardware class the UI should label itself after.

        ``device_type`` is ``"deck"``, ``"machine"`` or ``"other"``.
        Read fresh rather than cached: it costs two ``/sys`` reads and
        caching it would only add a staleness mode to a value that
        cannot change without a reboot anyway.
        """
        return {"success": True, "device_type": detect_device_type().value}

    async def list_directory(
        self,
        path: str,
        show_hidden: bool = False,
        sort_by: str = "name",
    ) -> Any:
        """Enumerate immediate subdirectories of ``path``.

        Backs the frontend ``StoragePathPicker`` which
        navigates step-by-step (one ``list_directory`` per
        click) so we never have to ship a tree of the whole
        filesystem.

        Filesystem work (path resolution + scandir) is
        offloaded to ``asyncio.to_thread`` via two helpers
        — ``_resolve_user_path`` and ``_collect_subdirs`` —
        so the event loop is never blocked on slow mounts
        (network shares, SD card, etc.).

        Args:
            path: absolute path to enumerate. ``~`` is
                expanded.
            show_hidden: include dotfile entries.
            sort_by: ``"name"`` (only sort supported today).

        Returns:
            ``{path, directories: [str]}``.

        Raises:
            RpcError: on any OS-level or permission error.
        """
        try:
            resolved = await asyncio.to_thread(_resolve_user_path, path)
            is_dir = await asyncio.to_thread(Path(resolved).is_dir)
            if not is_dir:
                raise RpcError("not_a_directory", path=resolved)
            entries = await asyncio.to_thread(
                _collect_subdirs, resolved, show_hidden, sort_by,
            )
            return {"path": resolved, "directories": entries}
        except PermissionError as e:
            raise RpcError("permission_denied", path=path, detail=str(e)) from e
        except OSError as e:
            raise RpcError("os_error", path=path, detail=str(e)) from e

    async def create_directory(self, path: str) -> Any:
        """Create a new directory at ``path``.

        Used by the frontend ``StoragePathPicker`` new-folder
        feature. Creates parent directories as needed.

        Returns:
            ``{"path": resolved}``.

        Raises:
            RpcError: on ``FileExistsError``, ``PermissionError``,
                or any other ``OSError``.
        """
        resolved = await asyncio.to_thread(_resolve_user_path, path)
        try:
            await asyncio.to_thread(Path(resolved).mkdir, parents=True, exist_ok=False)
        except FileExistsError as e:
            raise RpcError("directory_exists", path=resolved) from e
        except PermissionError as e:
            raise RpcError("permission_denied", path=resolved) from e
        except OSError as e:
            raise RpcError("os_error", path=resolved, detail=str(e)) from e
        return {"path": resolved}
