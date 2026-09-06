"""
Installer sub-package — public exports.

Re-exports the two classes a caller outside the sub-package needs to
construct or reference: ``UbisoftInstaller`` (the orchestration class)
and ``UbisoftInstallerCache`` (its on-disk cache helper).

The other modules (``cache``, ``launcher``, ``manual_ui``, ``registry``,
``uninstall``, ``update_op``) are internal — callers go through the
installer facade.
"""

from .cache import UbisoftInstallerCache
from .installer import UbisoftInstaller

__all__ = ["UbisoftInstaller", "UbisoftInstallerCache"]
