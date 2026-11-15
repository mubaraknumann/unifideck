"""EA install records: what was installed where, read by the store and the launcher.

One JSON file per game under ``~/.local/share/unifideck/ea/installed/``, written
by the store after an install and read by the library (installed state), the
updater (build id) and the launcher (Touchup marker, game exe). Standard library
only.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path


def records_dir() -> Path:
    """``~/.local/share/unifideck/ea/installed``, resolved at call time."""
    return Path.home() / ".local/share/unifideck/ea/installed"


@dataclass(frozen=True)
class InstallRecord:
    """One installed EA game."""

    game_id: str
    offer_id: str
    content_id: str
    install_path: str
    exe: str
    build_id: str
    version: str
    #: The exe last checked for the Origin SDK, and whether it embeds it
    #: ("yes"/"no"): only those games get the Origin client stand-in
    #: (``launcher/ea/standin.py``). Empty until the first launch.
    sdk_exe: str = ""
    origin_sdk: str = ""


_REQUIRED = ("game_id", "offer_id", "content_id", "install_path", "exe", "build_id", "version")


def _path(game_id: str) -> Path:
    return records_dir() / f"{game_id}.json"


def read_record(game_id: str) -> InstallRecord | None:
    """The record for ``game_id``, or None when absent or unreadable."""
    try:
        data = json.loads(_path(game_id).read_text())
        fields = {k: str(data[k]) for k in _REQUIRED}
        fields.update({k: str(data[k]) for k in InstallRecord.__dataclass_fields__ if k not in fields and k in data})
        return InstallRecord(**fields)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def all_records() -> dict[str, InstallRecord]:
    """Every readable record, by game id."""
    folder = records_dir()
    if not folder.is_dir():
        return {}
    records = (read_record(p.stem) for p in folder.glob("*.json"))
    return {r.game_id: r for r in records if r is not None}


def write_record(record: InstallRecord) -> None:
    """Write ``record`` atomically."""
    folder = records_dir()
    folder.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".record-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(asdict(record), handle, indent=1)
        os.replace(tmp, _path(record.game_id))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def delete_record(game_id: str) -> None:
    """Forget ``game_id``; absent is fine."""
    with contextlib.suppress(FileNotFoundError):
        _path(game_id).unlink()
