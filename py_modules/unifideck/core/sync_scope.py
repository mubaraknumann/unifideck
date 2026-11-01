"""Store scoping for a sync run — which stores a run covers.

A sync used to cover every available store. The Quick Access store rows
can now ask for one store at a time ("Sync games" / "Sync images" on a
single row), so a run carries an optional store scope:

* **Fetch** only the scoped stores' libraries, and keep every other
  store's last-known library as it is (:func:`merge_scoped_libraries`).
  The full merged library is what ``SYNC_COMPLETE`` carries, because the
  shortcut reconcile writes shortcuts for every game it is handed — a
  payload holding just one store would read as "the other stores are
  empty". What protects the other stores' shortcuts from the stale
  sweep is ``stores_synced``, which names only the stores fetched.
* **Post-sync phases** (metadata → artwork → compat) work through only
  the scoped stores' games (:func:`scoped_games`), read from the
  ``scope_stores`` key on the ``SYNC_COMPLETE`` payload that every phase
  forwards as ``sync_kwargs``.

``None`` means "every store" throughout, and is what every pre-existing
caller gets, so an unscoped run behaves exactly as before.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .types import Game


def normalize_stores(stores: Iterable[Any] | None) -> frozenset[str] | None:
    """Coerce a caller-supplied store list into a scope.

    Args:
        stores: store names from an RPC or internal caller, or ``None``.

    Returns:
        ``None`` for "every store" (also for a non-iterable or a string,
        which is never a valid list of stores), otherwise the set of
        non-empty string names. An empty list stays an empty set — it
        names no stores, which is not the same as naming all of them.
    """
    if stores is None or isinstance(stores, (str, bytes)):
        return None
    try:
        return frozenset(s for s in stores if isinstance(s, str) and s)
    except TypeError:
        return None


def scope_of(payload: dict[str, Any] | None) -> frozenset[str] | None:
    """Read the store scope off a ``SYNC_COMPLETE`` payload.

    A missing or malformed ``scope_stores`` reads as ``None`` (every
    store), so a payload from before this key existed keeps the old
    whole-library behaviour.
    """
    raw = (payload or {}).get("scope_stores")
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return None
    return normalize_stores(raw)


def scoped_games(games: list[Game], payload: dict[str, Any] | None) -> list[Game]:
    """Filter ``games`` to the stores the run's payload is scoped to."""
    scope = scope_of(payload)
    if scope is None:
        return games
    return [g for g in games if getattr(g, "store", None) in scope]


def merge_scoped_libraries(
    cached: dict[str, list[Game]],
    fetched: dict[str, list[Game]],
) -> dict[str, list[Game]]:
    """Overlay this run's fetched libraries onto the cached ones.

    Store order follows the cached library, with newly-seen stores
    appended, so the flattened game order stays stable between runs.

    Args:
        cached: the library as of the last run (``SyncService._all_games``).
        fetched: the libraries this run fetched, keyed by store.

    Returns:
        A new mapping; neither input is mutated.
    """
    merged = {name: fetched.get(name, games) for name, games in cached.items()}
    for name, games in fetched.items():
        merged.setdefault(name, games)
    return merged


@dataclass(frozen=True)
class RunScope:
    """What one run covered, handed from the run loop to finalize.

    Attributes:
        stores: the requested scope, ``None`` for every store.
        fetched: the stores whose libraries this run fetched, in fetch
            order. Becomes ``SYNC_COMPLETE.stores_synced`` — the only
            stores the shortcut reconcile may sweep.
        artwork_only: the run re-downloads artwork and fetches nothing.
    """

    stores: frozenset[str] | None = None
    fetched: tuple[str, ...] = ()
    artwork_only: bool = False

    @property
    def is_partial(self) -> bool:
        """Whether the run skipped part of the library's work.

        A partial run must not be recorded as a completed chain over the
        whole library: the next full sync would then skip work that this
        run never did for the stores outside its scope.
        """
        return self.stores is not None or self.artwork_only
