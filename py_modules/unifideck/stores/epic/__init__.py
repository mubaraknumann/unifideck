"""Epic Games Store sub-package — public entry point.

Re-exports ``EpicStore`` so callers can write
``from unifideck.stores.epic import EpicStore``. The class itself
lives in ``store.py`` and is the only public surface of the
entire sub-package — everything else is internal.

The Epic sub-package is **flat** (no sub-packages) because the surface
is small : 8 modules covering store orchestration, auth, library,
install, updates, filter, exe resolution, and a thin wrapper around
``legendary``. Discovery by ``StoreRegistry.auto_discover()`` recognises
the flat ``store.py`` pattern.
"""

from .store import EpicStore

__all__ = ["EpicStore"]
