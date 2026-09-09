"""infrastructure/ge_marker.py — the GE-Proton state marker.

Split out of ``ge_installer`` (which was pushed over the volumetry file cap
when the marker gained read-modify-write semantics). One small file owning
one JSON document: ``~/.local/share/unifideck/proton_ge_latest.json``.

The marker lets the out-of-process launcher resolve the default Proton
without a network round-trip, and carries the "have we already told the
user their external GE is behind" state. Stdlib only, so both the Decky
backend (bundled Python) and the launcher (system ``python3``) can import
it.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Records the tag the background installer last validated, so the
# launcher can resolve the default without a network round-trip.
_MARKER = Path("~/.local/share/unifideck/proton_ge_latest.json").expanduser()


def read_marker() -> dict[str, Any]:
    """Return the whole marker document, or ``{}`` on any failure."""
    if not _MARKER.is_file():
        return {}
    try:
        data = json.loads(_MARKER.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def read_cached_latest_tag() -> str | None:
    """Return the tag the background installer last validated, if any."""
    tag = read_marker().get("tag")
    return tag or None


def update_marker(**fields: Any) -> None:
    """Merge ``fields`` into the marker document (best effort).

    Read-modify-write rather than overwrite: the marker carries state other
    than the install (``external_warned_tag``), and a plain ``write_text``
    of ``{tag, installed_at}`` silently dropped it on the next install.
    """
    try:
        data = read_marker()
        data.update(fields)
        _MARKER.parent.mkdir(parents=True, exist_ok=True)
        _MARKER.write_text(json.dumps(data))
    except OSError as e:
        logger.warning("[ge_marker] could not write marker: %s", e)


def write_latest_tag(tag: str) -> None:
    """Record ``tag`` as the validated latest GE-Proton (best effort)."""
    update_marker(tag=tag, installed_at=time.time())
