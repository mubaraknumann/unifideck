"""GOG store sub-package — public entry point.

Re-exports ``GOGStore`` so callers can write
``from unifideck.stores.gog import GOGStore``. The class itself lives
in ``store.py`` and is the only public surface of the entire
sub-package — everything else is internal.

Discovered by ``StoreRegistry.auto_discover()`` via the
``<name>/store.py`` recognition pattern in the registry.
"""

from .store import GOGStore

__all__ = ["GOGStore"]
