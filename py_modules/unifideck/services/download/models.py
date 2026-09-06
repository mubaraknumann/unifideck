"""Download data models — the per-item record + error classification.

``DownloadItem`` is the frozen dataclass describing one queued
download : store, game_id, target path, language, current state
(queued / running / paused / done / failed), progress (bytes done
+ total, ETA), failure code if any.

``classify_download_error`` is the helper that turns an
``InstallResult.error`` string from a store-side installer into a
typed enum value the UI can render.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from time import time
from typing import Any

# How many recently-finished downloads to keep (in memory, on disk,
# and in the QuickAccess "Recently finished" list). Persisted to
# ``download_history.json`` so the list survives restarts + plugin
# reinstalls.
MAX_FINISHED_HISTORY = 10

@dataclass
class DownloadItem:
    """One queued or running download entry.

    Not actually frozen — the worker mutates ``progress``,
    ``status`` and ``error`` as the install proceeds. Persistence
    serialises through ``to_dict`` / ``from_dict`` so a stable
    on-disk format is preserved across schema changes (extra fields
    in the dict are ignored, missing fields fall back to dataclass
    defaults).

    Attributes:
        store: store identifier (``"epic"`` / ``"gog"`` / etc.).
        game_id: store-specific game id.
        install_path: target install directory on disk.
        title: human-readable game name (used by the UI).
        progress: percentage 0.0-100.0 (mutated by the worker).
        status: one of ``"queued"`` / ``"running"`` /
            ``"complete"`` / ``"failed"``.
        error: failure code (empty string when status isn't
            ``"failed"``).
    """

    store: str
    game_id: str
    install_path: str
    title: str = ""
    # User-picked install language (GOG multi-language games),
    # verbatim store language code. Empty = use the store default.
    language: str = ""
    download_dir: str = ""
    progress: float = 0.0
    status: str = "queued"
    error: str = ""
    # Frontend-facing fields
    downloaded_bytes: int = 0
    total_bytes: int = 0
    speed_mbps: float = 0.0
    eta_seconds: int = 0
    added_time: float = field(default_factory=time)
    start_time: float | None = None
    end_time: float | None = None
    storage_location: str = "internal"
    download_phase: str = "downloading"
    #: A wrapper store's own account of why nothing is moving — e.g.
    #: Battle.net's "queued behind the Agent's self-update", read out of the
    #: Agent's log rather than guessed. Currently **computed and discarded**:
    #: no frontend renders it (audit register item 49). Kept because the
    #: signal is real and measured. GOG's decorative producers of this field,
    #: which restated the localized phase label in hardcoded English, were
    #: deleted instead (item 45).
    phase_message: str = ""
    # Operation type, recorded by the enqueue path — NOT inferred.
    # ``install_game`` enqueues ``False``; ``update_game`` enqueues
    # ``True``. Drives (a) the UI label ("Update Queued" /
    # "Downloading Update" vs "Download Queued" / "Downloading"),
    # (b) worker dispatch to ``store.update_game`` vs
    # ``store.install_game``, and (c) the cancel guardrail (an
    # update must not delete the pre-existing install).
    is_update: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-friendly dict.

        Emits dual-key output so both backend-internal consumers
        (``title``, ``progress``, ``error``) and the frontend
        (``game_title``, ``progress_percent``, ``error_message``)
        receive matching keys without field-name translation.
        """
        return {
            # Identifiers
            "id": f"{self.store}:{self.game_id}",
            "store": self.store,
            "game_id": self.game_id,
            # Title (both names)
            "title": self.title,
            "game_title": self.title,
            # Install language (verbatim store code; "" = default)
            "language": self.language,
            # Path
            "install_path": self.install_path,
            # Progress (both names)
            "progress": self.progress,
            "progress_percent": self.progress,
            # Status
            "status": self.status,
            # Error (both names)
            "error": self.error,
            "error_message": self.error,
            # Stats
            "downloaded_bytes": self.downloaded_bytes,
            "total_bytes": self.total_bytes,
            "speed_mbps": self.speed_mbps,
            "eta_seconds": self.eta_seconds,
            # Timing
            "added_time": self.added_time,
            "start_time": self.start_time,
            "end_time": self.end_time,
            # Misc
            "storage_location": self.storage_location,
            "download_phase": self.download_phase,
            "phase_message": self.phase_message,
            "is_update": self.is_update,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> DownloadItem:
        """Reconstruct a ``DownloadItem`` from a dict.

        Extra keys in ``d`` (from forward-compatibility schemas)
        are silently ignored. Missing keys fall back to the
        dataclass defaults — a queue persisted on an older plugin
        version is still loadable on a newer version that added
        fields.

        Args:
            d: dict (typically from ``to_dict`` or a JSON load).

        Returns:
            A fresh ``DownloadItem`` populated from the dict.
        """
        d = dict(d)
        # Back-compat: queues persisted by an older build keyed the
        # operation flag as ``was_previously_installed``. Map it onto
        # the current ``is_update`` field so an in-flight queue still
        # loads with the right label after upgrade.
        if "is_update" not in d and "was_previously_installed" in d:
            d["is_update"] = d["was_previously_installed"]
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

def classify_download_error(exc: Exception) -> str:
    """Map an exception's message to a typed error code.

    Used by the worker when an install raises an unhandled
    exception (typically a subprocess error). Pattern-matches the
    exception's string for known substrings and falls back to
    ``"unknown_error"`` on no match.

    The classification is intentionally simple and English-only —
    finer-grained classification (e.g. distinguishing transient
    network errors from a permanent DNS failure) lives in the
    store-side installer which produces structured ``InstallResult``
    errors that bypass this fallback.

    Args:
        exc: the exception caught by the worker.

    Returns:
        One of ``"permission_denied"`` / ``"disk_full"`` /
        ``"timeout"`` / ``"network_error"`` / ``"not_found"`` /
        ``"cli_prompt_blocked"`` / ``"unknown_error"``.
    """
    msg = str(exc).lower()
    if "eoferror" in msg or "sdl_prompt" in msg:
        # A store CLI asked an interactive question we can't answer
        # (legendary's Selective Downloads prompt is not suppressed by
        # --yes). Distinct from a generic failure because the fix is
        # always "pass the answer as a flag", never "retry" — see
        # stores/epic/sdl.py.
        return "cli_prompt_blocked"
    if "permission" in msg or "denied" in msg:
        return "permission_denied"
    if "no space" in msg or "disk full" in msg or "disk space" in msg:
        # legendary phrases it "Not enough available disk space!"
        return "disk_full"
    if "timeout" in msg:
        return "timeout"
    if (
        "network" in msg
        or "connection" in msg
        or "failed to establish" in msg
        or "temporary failure in name resolution" in msg
    ):
        return "network_error"
    if (
        "not found" in msg
        or "404" in msg
        # legendary: game/asset missing for the account or platform.
        or "no app asset found" in msg
        or "in list of available games" in msg
    ):
        return "not_found"
    return "unknown_error"
