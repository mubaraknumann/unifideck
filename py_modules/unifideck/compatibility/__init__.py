"""Per-device compatibility ratings (ProtonDB, Valve) and Proton tools management."""

from __future__ import annotations

from .deck_verified import (
    DECK_CATEGORIES,
    STEAMOS_CATEGORIES,
    TRACK_NAMES,
    TRACKS,
    TrackResult,
    TrackSpec,
    compat_track_for,
    parse_compat_response,
    spec_for,
)
from .legacy import (
    BackgroundCompatFetcher,
    fetch_deck_verified,
    fetch_protondb_rating,
    get_compat_for_title,
    load_compat_cache,
    prefetch_compat,
    save_compat_cache,
    search_steam_store,
)
from .library import (
    CompatLibrary,
    CompatRating,
)
from .proton_helpers import (
    CompatToolResult,
    ProtonToolsManager,
    get_compat_tool_for_app,
    get_saved_proton_tool,
    is_linux_runtime,
    restore_compat_tool,
    save_proton_setting,
    temporarily_clear_compat_tool,
)

__all__ = [
    "DECK_CATEGORIES",
    "STEAMOS_CATEGORIES",
    "TRACKS",
    "TRACK_NAMES",
    "BackgroundCompatFetcher",
    "CompatLibrary",
    "CompatRating",
    "CompatToolResult",
    "ProtonToolsManager",
    "TrackResult",
    "TrackSpec",
    "compat_track_for",
    "fetch_deck_verified",
    "fetch_protondb_rating",
    "get_compat_for_title",
    "get_compat_tool_for_app",
    "get_saved_proton_tool",
    "is_linux_runtime",
    "load_compat_cache",
    "parse_compat_response",
    "prefetch_compat",
    "restore_compat_tool",
    "save_compat_cache",
    "save_proton_setting",
    "search_steam_store",
    "spec_for",
    "temporarily_clear_compat_tool",
]

