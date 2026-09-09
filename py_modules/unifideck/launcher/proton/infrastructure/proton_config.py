"""infrastructure/proton_config.py — the ``compat`` block of config.json.

One owner for "read Unifideck's Proton settings off disk". Two readers had
grown up independently — ``selector.get_unifideck_proton_tool`` for
``compat.proton_tool`` and ``external_ge`` for ``compat.external_ge`` — each
with its own hardcoded path and its own ``except (OSError, ValueError)``.
That is two copies of one fact, which is how the path in one drifts from the
path in the other.

Deliberately a guarded raw ``json`` read rather than the config service:
this is imported by the out-of-process launcher running under the *system*
interpreter, so it must stay stdlib-only (no aiohttp, no ``unifideck.config``).
Being a guarded read with its own default also keeps it outside
``check_config_keys``' ``RUNTIME_REQUIRED_KEYS`` regime, the same way
``compat.proton_tool`` always has been.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CONFIG_PATH = Path("~/.local/share/unifideck/config.json").expanduser()


def read_compat_section() -> dict[str, Any]:
    """Return ``config.json``'s ``compat`` block, or ``{}`` on any failure.

    Never raises: a missing, unreadable or malformed config must leave the
    launcher on its defaults, not fail a launch.
    """
    try:
        with CONFIG_PATH.open(encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(cfg, dict):
        # Valid JSON, wrong shape (a bare list or string). ``.get`` would
        # raise AttributeError here, which is not an error class the callers
        # guard — and a hand-edited config is exactly where this happens.
        return {}
    section = cfg.get("compat")
    return section if isinstance(section, dict) else {}


def compat_setting(key: str, default: str = "") -> str:
    """Return ``compat.<key>`` as a string, or ``default`` when unset."""
    value = read_compat_section().get(key)
    return str(value) if value not in (None, "") else default
