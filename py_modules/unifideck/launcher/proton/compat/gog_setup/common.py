"""compat/gog_setup/common.py — shared paths, manifest loaders, wine exec.

Standalone helpers for the GOG redistributable-setup port (Heroic's
``setup.ts``). Deliberately imports nothing from ``unifideck.stores`` —
this runs in the slim launcher process where the GOG store's import
chain (auth → security → cryptography) fails. Paths are hardcoded to
match what gogdl writes (same locations staging used).
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from unifideck.launcher.proton.infrastructure.setup_run import run_setup_exe

if TYPE_CHECKING:
    from unifideck.launcher.proton.infrastructure.core import ProtonLaunchPlan

logger = logging.getLogger(__name__)

_CFG = Path("~/.config/unifideck").expanduser()
MANIFESTS_DIR = _CFG / "heroic_gogdl" / "manifests"
REDIST_DIR = _CFG / "gogdl" / "redist"
SUPPORT_DIR = _CFG / "gogdl" / "gog-support"
# MUST match the path the plugin actually writes and every other gogdl call
# passes as ``--auth-config-path`` (``GOGConfig.auth_config_path``): a FLAT
# ``gogdl_auth.json``, NOT a ``gogdl/auth.json`` subdir. The subdir path never
# existed, so ``ensure_redist_downloaded`` bailed with "cannot download redist
# (gogdl=True auth=False)" on every launch and NO GOG game ever got its
# manifest-declared redistributables (MSVC*, UE4REDIST, …) — ``gogdl/redist/``
# stayed empty on every device. Same bug, same file, as the one fixed for Comet
# in ``compat/gog.py``; this is now the single definition both import.
AUTH_CONFIG = _CFG / "gogdl_auth.json"

_LANG_MAP = {
    "en": "english", "de": "german", "fr": "french", "es": "spanish",
    "it": "italian", "pt": "portuguese", "ru": "russian", "pl": "polish",
    "zh": "chinese", "ja": "japanese", "ko": "korean", "nl": "dutch",
    "tr": "turkish",
}


def language_name(lang_code: str) -> str:
    """Map a language label to a GOG ``setup.exe`` language name.

    The GOG installer's ``/Language=`` switch requires a name like
    ``spanish`` — it can't take a raw locale code — so this mapping is
    mandated by that interface, not a substitution of the user's code.
    Normalizes whatever format the marker recorded (``esp`` / ``Spanish``
    / ``es-ES``) to an ISO base first so the lookup actually resolves.
    """
    from unifideck.utils.lang_normalize import normalize_language
    base = normalize_language(lang_code) or lang_code.split("-", maxsplit=1)[0].lower()
    return _LANG_MAP.get(base, "english")


def wait_for_prefix_ready(prefix_path: Path, timeout: int = 30) -> bool:
    """Block until the Wine/Proton prefix has a ``system.reg``."""
    candidates = (prefix_path / "pfx" / "system.reg", prefix_path / "system.reg")
    start = time.time()
    while not any(c.exists() for c in candidates):
        if time.time() - start > timeout:
            logger.warning("[gog_setup] prefix not ready after %ds", timeout)
            return False
        time.sleep(1)
    return True


def load_manifest(game_id: str) -> dict[str, Any] | None:
    """Load gogdl's game manifest, or None."""
    path = MANIFESTS_DIR / game_id
    if not path.is_file():
        logger.info("[gog_setup] no manifest at %s", path)
        return None
    try:
        return cast_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as e:
        logger.warning("[gog_setup] manifest parse failed: %s", e)
        return None


def get_dependencies(manifest: dict[str, Any]) -> list[str]:
    """Extract redistributable dependency IDs from a v1/v2 manifest."""
    raw = (
        _v1_depot_redists(manifest)
        if manifest.get("version") == 1
        else (manifest.get("dependencies", []) or [])
    )
    deps: list[str] = []
    for dep in raw:
        if dep not in deps:
            deps.append(dep)
    return deps


def _v1_depot_redists(manifest: dict[str, Any]) -> list[Any]:
    """The truthy ``redist`` ids from a v1 manifest's product depots."""
    return [
        depot.get("redist")
        for depot in manifest.get("product", {}).get("depots", [])
        if isinstance(depot, dict) and depot.get("redist")
    ]


def load_redist_manifest() -> dict[str, Any] | None:
    """Load gogdl's ``.gogdl-redist-manifest``, or None."""
    path = REDIST_DIR / ".gogdl-redist-manifest"
    if not path.is_file():
        return None
    try:
        return cast_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as e:
        logger.warning("[gog_setup] redist manifest parse failed: %s", e)
        return None


def cast_dict(value: Any) -> dict[str, Any] | None:
    """Return ``value`` if it's a dict, else None."""
    return value if isinstance(value, dict) else None


async def run_wine(
    plan: ProtonLaunchPlan, exe: str, args: list[str],
) -> bool:
    """Run a Windows exe in the game's prefix via umu. True on rc 0.

    Thin ``STORE=gog`` binding of :func:`setup_run.run_setup_exe`, which owns
    the umu spawn for every prefix-setup step and carries the reason none of
    them may call a Proton's ``wine`` directly.

    This is the one setup site with an unbounded wait — no timeout, no retry,
    no wineserver reap — so anything that wedges a helper here wedges the
    launch outright rather than costing a timeout budget. GOG's setup steps
    are full Windows installers and a bounded wait would cut them off inside
    a prefix; the registry writers, which are quick and sit on the launch hot
    path, pass a timeout instead.
    """
    return await run_setup_exe(plan, exe, args, store="gog", label="gog_setup")
