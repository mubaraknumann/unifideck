"""services/download/worker_helpers.py — module-level worker utilities.

Extracted from ``worker.py`` to keep that file under the 550-LOC
volumetry cap. Pure module-level helpers — no mixin/host state.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from unifideck.core.native_entry import gog_linux_start_script
from unifideck.launcher.types.context import is_windows_executable
from unifideck.launcher.wrapper_stores import is_wrapper_store, uses_manual_download_phase

from .models import DownloadItem

logger = logging.getLogger(__name__)

# Strong references to background install tasks so the GC can't
# collect them mid-flight (see RUF006).
_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()


def prefix_warmup_supported(item: DownloadItem, result: Any = None) -> bool:
    """Whether ``item``'s store and depot shape get an install-time warmup.

    Wrapper stores bootstrap their own prefix through the vendor client, so the
    generic warmup must not run over the top of it.

    A GOG *Linux-native* depot (root-level ``start.sh`` — the same signal
    ``GOGExeResolver`` and the native-launch DOSBox dispatch use) never touches
    Proton/Wine, so building a prefix for it is pure waste and, worse, can wedge
    the shared prefix-setup machinery (wineserver locks, the GE-Proton retry
    ladder) for a game that will never use it.

    The same holds for any store whose install reports a native exe in
    ``InstallResult.metadata["exe_path"]`` (GameVault and itch.io native
    builds): the launcher will exec it directly, so no prefix is built.

    An install that sets ``InstallResult.metadata["runtime_managed_prefix"]``
    builds its prefix with its own runtime at first launch (Xbox PC games:
    the Xodus runtime's ``proton run``, never umu or GE-Proton). A GE warmup
    there would build the wrong prefix, so it is skipped. The flag names the
    behaviour, not a store, so the worker does not need to know which store
    sets it.
    """
    if uses_manual_download_phase(item.store):
        return False
    metadata = getattr(result, "metadata", None) or {}
    if metadata.get("runtime_managed_prefix"):
        logger.info(
            "[DownloadWorker] skipping prefix warmup for %s:%s: its runtime "
            "builds the prefix at first launch",
            item.store, item.game_id,
        )
        return False
    if item.store == "gog" and gog_linux_start_script(item.install_path):
        logger.info(
            "[DownloadWorker] skipping prefix warmup for %s:%s — "
            "Linux-native GOG depot (start.sh), no Proton prefix needed",
            item.store,
            item.game_id,
        )
        return False
    exe = metadata.get("exe_path") or ""
    if exe and not is_windows_executable(exe):
        logger.info(
            "[DownloadWorker] skipping prefix warmup for %s:%s: native "
            "executable %s, no Proton prefix needed",
            item.store, item.game_id, exe,
        )
        return False
    return True


def track_task(task: asyncio.Task[Any]) -> None:
    """Register a fire-and-forget task so the GC doesn't collect it early."""
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


# (progress-key, DownloadItem attribute, converter) for the structured
# progress payloads emitted by GOG / Ubisoft. Driven by a table so
# ``apply_dict_progress`` stays flat (no per-field if-cascade).
_PROGRESS_FIELDS: tuple[tuple[str, str, Any], ...] = (
    ("downloaded_bytes", "downloaded_bytes", int),
    ("total_bytes", "total_bytes", int),
    ("eta_seconds", "eta_seconds", int),
    ("phase", "download_phase", str),
    ("phase_message", "phase_message", str),
)


def apply_dict_progress(item: DownloadItem, progress: dict[str, Any]) -> None:
    """Copy a structured progress payload (GOG/Ubisoft) onto *item*."""
    pct = progress.get("percentage") or progress.get("progress_percent")
    if isinstance(pct, (int, float)):
        item.progress = float(pct)
    if "speed_mbps" in progress:
        item.speed_mbps = float(progress["speed_mbps"])
    elif "speed_bps" in progress:
        item.speed_mbps = float(progress["speed_bps"]) / (1024 * 1024)
    for pkey, attr, conv in _PROGRESS_FIELDS:
        if pkey in progress:
            setattr(item, attr, conv(progress[pkey]))


async def pick_new_install_proton(item: DownloadItem, gets_prefix_setup: bool) -> None:
    """Set ``item.proton_tool`` to the latest GE for a fresh install.

    The install-time prefix setup builds the prefix on it, and the frontend
    makes it the game's Steam compatibility tool (Properties > Compatibility),
    which every launch then reads first. Steam's global default on a Deck is
    Valve's Proton, which ships no FFmpeg (32-bit H.264 video crashes). Never
    for an update or an existing game (they keep their Proton), never for a
    wrapper store (client and games share one prefix), and only for a game
    that gets a prefix setup, i.e. one that runs through Proton.
    """
    if item.is_update or not gets_prefix_setup or is_wrapper_store(item.store):
        return
    from unifideck.launcher.proton import select_managed_ge_proton

    try:
        _path, tool = await asyncio.to_thread(select_managed_ge_proton)
    except Exception as e:
        logger.warning("[DownloadWorker] no GE-Proton for %s:%s, Steam's default stays: %s", item.store, item.game_id, e)
        return
    flat = "".join(ch for ch in tool.lower() if ch.isalnum())
    if "geproton" in flat or "protonge" in flat:  # not the Proton Experimental fallback
        item.proton_tool = tool
