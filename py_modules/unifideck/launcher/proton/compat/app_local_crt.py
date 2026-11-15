"""Give an exe the activation context its bundled VC++ 2005/2008 runtime needs.

Some games ship the VC++ 2005/2008 runtime next to the exe (``msvcr90.dll`` plus
``Microsoft.VC90.CRT.manifest``) and load it by full path from a DLL, while the
exe itself declares no manifest. That runtime refuses to start outside an
activation context that names it, so the game dies with R6034 ("An application
has made an attempt to load the C runtime library incorrectly"). DLL overrides
cannot help: a full-path load bypasses them.

An external ``<exe>.manifest`` declaring the bundled runtime's exact identity
makes Wine create that context at process start, and the bundled copy then loads
inside it. Written only when the game ships such a runtime manifest, the exe
embeds no manifest, and no external one exists, so a working game is never
touched. Measured on an EA title (2026-11-15): R6034 without, game window with.

Standard library only: the launcher runs on the system Python.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

_RUNTIME_MANIFESTS = ("Microsoft.VC90.CRT.manifest", "Microsoft.VC80.CRT.manifest")
_IDENTITY_RE = re.compile(r"<assemblyIdentity\b[^>]*>", re.IGNORECASE | re.DOTALL)
_ATTR_RE = re.compile(r'(\w+)\s*=\s*"([^"]*)"')
_IDENTITY_KEYS = ("type", "name", "version", "processorArchitecture", "publicKeyToken")
#: How far into the exe to look for an embedded manifest (resources sit near the end of small exes).
_EMBEDDED_SCAN_BYTES = 64 * 1024 * 1024

_TEMPLATE = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
  <dependency>
    <dependentAssembly>
      <assemblyIdentity {identity}></assemblyIdentity>
    </dependentAssembly>
  </dependency>
</assembly>
"""


def _runtime_identity(manifest: Path) -> dict[str, str] | None:
    """The bundled runtime's assembly identity, or None when it cannot be read."""
    try:
        match = _IDENTITY_RE.search(manifest.read_text(errors="replace"))
    except OSError:
        return None
    if match is None:
        return None
    attrs = dict(_ATTR_RE.findall(match.group(0)))
    if not all(attrs.get(key) for key in _IDENTITY_KEYS):
        return None
    return {key: attrs[key] for key in _IDENTITY_KEYS}


def _embeds_manifest(exe: Path) -> bool:
    try:
        with exe.open("rb") as handle:
            return b"<assembly" in handle.read(_EMBEDDED_SCAN_BYTES)
    except OSError:
        return True  # unreadable: leave it alone


def ensure_crt_activation_manifest(exe: Path) -> bool:
    """Write ``<exe>.manifest`` when the conditions above hold; True when written."""
    external = exe.with_name(exe.name + ".manifest")
    if external.exists():
        return False
    runtime = next((exe.parent / name for name in _RUNTIME_MANIFESTS if (exe.parent / name).is_file()), None)
    if runtime is None:
        return False
    identity = _runtime_identity(runtime)
    if identity is None or _embeds_manifest(exe):
        return False
    attrs = " ".join(f'{key}="{identity[key]}"' for key in _IDENTITY_KEYS)
    try:
        external.write_text(_TEMPLATE.format(identity=attrs))
    except OSError as e:
        logger.warning("[compat.app_local_crt] could not write %s: %s", external, e)
        return False
    logger.info("[compat.app_local_crt] wrote %s for the bundled %s %s", external.name, identity["name"], identity["version"])
    return True
