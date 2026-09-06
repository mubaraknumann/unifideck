"""Amazon Games store sub-package — public entry point.

Re-exports ``AmazonStore`` so callers can write
``from unifideck.stores.amazon import AmazonStore``. The class itself
lives in ``amazon_store.py`` and is the only public surface
of the entire sub-package — everything else is internal.

Unlike Epic / GOG / Ubisoft, the Amazon sub-package is **flat** (no
sub-packages) because the surface is smaller : 6 files total, each
covering a single concern (auth, install, library, updates, fuel
parsing). Discovery by ``StoreRegistry.auto_discover()`` therefore
recognises the flat ``amazon_store.py`` pattern.
"""

from .amazon_store import AmazonStore

__all__ = ["AmazonStore"]
