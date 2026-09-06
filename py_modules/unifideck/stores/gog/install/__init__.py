"""Install sub-package — exposes nothing publicly.

This ``__init__`` is intentionally empty. Install components are
internal to the GOG sub-package — callers reach them through
``GOGInstaller`` exposed by ``store.py``.
"""

from .installer import GOGInstaller

__all__ = ["GOGInstaller"]
