"""infrastructure/proton_health.py — spawn-time Proton sanity check.

Split out of ``umu_runtime`` (which the check pushed over the volumetry
file cap). One question, asked at the last possible moment: is the Proton
we are about to hand umu still intact?
"""
from __future__ import annotations

import logging
from pathlib import Path

from unifideck.launcher.types.errors import ProtonUnavailableError

from . import ge_installer

logger = logging.getLogger(__name__)


def assert_proton_still_complete(env: dict[str, str] | None) -> None:
    """Re-check the selected Proton immediately before spawning umu.

    ``selector`` validates a Proton when it *picks* one, which was enough
    while every Proton we could pick was one we had installed ourselves
    into an immutable ``<tag>`` directory. Since 0.7.5 the default may be a
    tool an external manager owns and, per its own documentation, "updates
    in-place" — so the tree can be torn between selection and spawn.

    This is the failure ``is_proton_install_complete`` exists for: its
    docstring cites a broken *auto-updated* Proton wedging every
    ``umu-run`` operation's wineserver forever, and rests on the premise
    that "we download GE-Proton ourselves". External adoption removes that
    premise. Failing here costs a clear error instead of a hung wineserver.

    Deliberately unconditional rather than only for externally managed
    tools: it is a handful of stats plus two small reads, it needs no extra
    state threaded through ``proton_prepare``, and it equally covers an
    official Proton that Steam updated underneath a queued install.
    """
    proton_dir = (env or {}).get("PROTONPATH")
    if not proton_dir:
        return
    script = Path(proton_dir) / "proton"
    if not script.exists():
        # Not a Proton layout (umu resolves some PROTONPATH values itself).
        return
    if not ge_installer.is_proton_install_complete(script):
        raise ProtonUnavailableError(
            f"Proton at {proton_dir} is incomplete or corrupt at launch time "
            "(an external manager may have updated it mid-flight)",
        )
