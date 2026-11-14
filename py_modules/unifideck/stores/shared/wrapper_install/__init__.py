"""Manual (vendor-client-driven) installs, shared by every wrapper store.

py_modules/unifideck/stores/shared/wrapper_install/

:mod:`.watch` owns the loop — timeouts, give-up watchdogs, completion, progress
ticks. :mod:`.probe` is the per-store half: recognising the game arriving in the
prefix. Adding EA App is a probe, not another copy of the loop.
"""

from .probe import InstallFacts, InstallProbe
from .watch import CLIENT_CLOSED, install_alive, watch_manual_install

__all__ = [
    "CLIENT_CLOSED",
    "InstallFacts",
    "InstallProbe",
    "install_alive",
    "watch_manual_install",
]
