"""
Auth sub-package — public exports.

Re-exports the three top-level classes of the auth facade :

* ``UbisoftAuth`` — orchestration class;
* ``UbisoftAuthState`` — frozen dependencies (config, paths, binaries…);
* ``UbisoftAuthServices`` — frozen Layer-5 service references.

The split between ``State`` and ``Services`` makes the auth facade
trivially testable: hand it two stubs and you've isolated it from the
entire plugin.
"""

from .facade import UbisoftAuth, UbisoftAuthServices, UbisoftAuthState

__all__ = ["UbisoftAuth", "UbisoftAuthServices", "UbisoftAuthState"]
