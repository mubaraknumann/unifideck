"""Keep a shortcut name the user chose — sync must not undo a rename.

A user renames a game through Steam's own shortcut properties, and Steam
writes that name to ``AppName`` in ``shortcuts.vdf``. Reconcile used to write
``game.title`` over it on every pass, so the rename lasted until the next
sync. That was every sync, not just Force Sync, because the registry-reclaim
path runs for every registered game (see ``_sync_one_game``).

The registry already records the title we last wrote for each game. That is
what tells a rename apart from a stale name:

* ``AppName`` equals the recorded title — nobody touched it since we wrote
  it, so it follows the store (a store-side title fix still reaches Steam).
* ``AppName`` differs from the recorded title — the user changed it, so it
  stays.

The registry keeps recording the *store* title, never the user's name. If it
recorded the user's name, the next sync would see the two match and write the
store title straight back.

With no recorded title there is nothing to compare against, so the store
title wins. That covers a first sync and a registry lost to a reinstall:
a rename may then be lost once, but a stale name is never frozen in place.
"""

from __future__ import annotations

from typing import Any


def resolve_app_name(
    current: Any, last_written: str | None, store_title: str,
) -> str:
    """The ``AppName`` reconcile should leave on a shortcut.

    *current* is the entry's ``AppName`` as Steam left it, *last_written*
    the title the registry recorded on the previous pass, *store_title*
    what the store reports now.
    """
    if not isinstance(current, str) or not current.strip():
        return store_title
    if last_written is None or current == last_written:
        return store_title
    return current


def get_registered_title(
    registry: dict[str, dict[str, Any]], key: str,
) -> str | None:
    """The title the registry recorded for *key*, if it recorded one."""
    entry = registry.get(key)
    if not isinstance(entry, dict):
        return None
    title = entry.get("title")
    return title if isinstance(title, str) and title else None
