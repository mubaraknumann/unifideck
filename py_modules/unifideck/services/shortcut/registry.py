"""Persistent shortcuts registry — ``{store:game_id → appid}`` across restarts.

A single JSON file at ``~/.local/share/unifideck/shortcuts_registry.json``
maps each Unifideck-managed game (``"<store>:<game_id>"``) to the
deterministic Steam AppID we assigned when the shortcut was first
created. The file lives in user data, so it survives plugin
uninstall/reinstall — that's what lets us *reclaim* an orphaned
``shortcuts.vdf`` entry after Steam mangled its LaunchOptions or
tags: we look up the registered AppID and reuse it, preserving the
artwork Steam already cached for that ID.

Ported from ``staging:py_modules/unifideck/shortcuts/shortcuts_manager.py``
(``load_shortcuts_registry`` / ``save_shortcuts_registry`` /
``register_shortcut`` family).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from unifideck.core.compat_bridge import to_unsigned

logger = logging.getLogger(__name__)

DEFAULT_REGISTRY_PATH = Path(
    "~/.local/share/unifideck/shortcuts_registry.json",
).expanduser()


def _read_json_dict(p: Path) -> dict[str, dict[str, Any]] | None:
    """Parse *p* as a JSON object, or ``None`` if it is unusable."""
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("[ShortcutsRegistry] load failed (%s): %s", p, e)
        return None
    return data if isinstance(data, dict) else None


def load_registry(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """Return the parsed registry, falling back to the ``.bak`` copy.

    Degrading a corrupt registry straight to ``{}`` was quietly
    destructive: the next :func:`save_registry` then wrote that empty
    dict over the still-recoverable file, and with it every recorded
    appid. Losing those does not just forget a mapping — it makes every
    existing shortcut unreclaimable, so reconcile re-adds each game
    under a freshly generated id and the user's artwork and playtime
    stay stranded on the old ones.

    Mirrors ``core/cache_manager.CacheStore._load``.
    """
    p = path or DEFAULT_REGISTRY_PATH
    if not p.exists():
        return {}
    data = _read_json_dict(p)
    if data is not None:
        return data

    bak = p.with_suffix(p.suffix + ".bak")
    if not bak.exists():
        return {}
    recovered = _read_json_dict(bak)
    if recovered is None:
        return {}
    logger.warning(
        "[ShortcutsRegistry] restored %d entries from backup after a "
        "corrupt read", len(recovered),
    )
    return recovered


def save_registry(
    registry: dict[str, dict[str, Any]], path: Path | None = None,
) -> bool:
    """Persist ``registry`` atomically, keeping one backup generation.

    tmp + ``os.replace`` so a crash mid-write cannot truncate the file
    (a truncated registry reads back as corrupt, which used to mean
    total loss — see :func:`load_registry`).
    """
    p = path or DEFAULT_REGISTRY_PATH
    tmp = p.with_suffix(p.suffix + ".tmp")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists():
            bak = p.with_suffix(p.suffix + ".bak")
            bak.write_bytes(p.read_bytes())
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(registry, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(p)
    except OSError as e:
        logger.warning("[ShortcutsRegistry] save failed (%s): %s", p, e)
        with contextlib.suppress(OSError):
            tmp.unlink()
        return False
    return True


def register(
    registry: dict[str, dict[str, Any]],
    launch_options: str,
    appid: int,
    title: str,
) -> dict[str, Any]:
    """Add or update an entry; returns the entry dict written.

    Mutates ``registry`` in place. Persistence is the caller's
    responsibility (batch writes amortise the JSON cost).

    ``created`` is set once and preserved on later calls. It used to be
    restamped every time, which made it mean "last registered" — and
    since reconcile re-registers on nearly every sync, every row in the
    file carried the same timestamp, erasing the one signal that could
    date a shortcut. ``last_seen`` carries that information now.
    """
    appid_unsigned = to_unsigned(appid)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    previous = registry.get(launch_options)
    created = now
    if isinstance(previous, dict) and isinstance(previous.get("created"), str):
        created = previous["created"]
    entry: dict[str, Any] = {
        "appid": appid,
        "appid_unsigned": appid_unsigned,
        "title": title,
        "created": created,
        "last_seen": now,
    }
    registry[launch_options] = entry
    return entry


def unregister(
    registry: dict[str, dict[str, Any]], appid: int,
) -> list[str]:
    """Drop every row pointing at ``appid``; return the keys removed.

    Without this the registry only ever grew, and each dead row stayed
    a live seed for the reclaim path — reconcile looks a game's old
    appid up here and rewrites whichever shortcut carries it.
    """
    keys = [
        key for key, entry in registry.items()
        if isinstance(entry, dict) and entry.get("appid") == appid
    ]
    for key in keys:
        del registry[key]
    return keys


def get_registered_appid(
    registry: dict[str, dict[str, Any]], launch_options: str,
) -> int | None:
    """Look up the AppID previously assigned to ``launch_options``."""
    entry = registry.get(launch_options)
    if not isinstance(entry, dict):
        return None
    appid = entry.get("appid")
    return appid if isinstance(appid, int) else None


__all__ = [
    "DEFAULT_REGISTRY_PATH",
    "get_registered_appid",
    "load_registry",
    "register",
    "save_registry",
    "unregister",
]
