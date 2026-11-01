"""launcher/xbox_pc/prefix_webview2.py — the runtime's WebView2 in a game prefix.

Ad-supported Microsoft Casual Games stop at start when no WebView2 runtime is
installed (ISSUES.md I-041). A runtime with ``webview2-fixed-v1`` carries one
copy of Microsoft's Fixed Version runtime in ``files/share/webview2/<version>``.
Each prefix gets a link to it where the Evergreen installer would put it, and
the EdgeUpdate values the game's ``WebView2Loader.dll`` reads to find it:

* ``Program Files (x86)/Microsoft/EdgeWebView/Application`` -> the runtime's
  ``files/share/webview2``. A real folder there is a real install, left alone.
* ``EdgeUpdate\\Clients\\{WebView2}``: ``pv`` and ``location``.
* ``EdgeUpdate\\ClientState\\{WebView2}``: ``EBWebView``, the version folder.

The loader ignores ``WEBVIEW2_BROWSER_EXECUTABLE_FOLDER`` and the
``BrowserExecutableFolder`` policy when it detects the runtime, so the
values are the only way in.

Stdlib only, Python 3.10 safe.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from .errors import XboxPcLaunchError
from .runtime import Runtime

logger = logging.getLogger(__name__)

WEBVIEW2_FEATURE = "webview2-fixed-v1"
APPLICATION_DIR = "drive_c/Program Files (x86)/Microsoft/EdgeWebView/Application"
_WINDOWS_APPLICATION = "C:\\Program Files (x86)\\Microsoft\\EdgeWebView\\Application"
_PRODUCT = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
_EDGE_UPDATE = ("Software", "Wow6432Node", "Microsoft", "EdgeUpdate")
_VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+){3}\Z")

#: ``(key path under HKLM, value name, value)``.
RegistryValue = tuple[tuple[str, ...], str, str]


def shared_webview2(runtime: Runtime) -> tuple[str, Path] | None:
    """``(version, folder holding <version>/)`` the runtime ships, or None.

    Raises ``runtime_missing`` when the runtime declares WebView2 but its
    folder does not hold exactly one complete version.
    """
    if WEBVIEW2_FEATURE not in runtime.features:
        return None
    root = runtime.files / "share" / "webview2"
    try:
        versions = sorted(
            entry.name for entry in root.iterdir()
            if _VERSION_RE.match(entry.name) and (entry / "msedgewebview2.exe").is_file()
        )
    except OSError as err:
        raise XboxPcLaunchError("runtime_missing", f"WebView2: {root}: {err}") from err
    if len(versions) != 1:
        raise XboxPcLaunchError("runtime_missing", f"WebView2: {root} holds {versions or 'no version'}")
    return versions[0], root


def link_webview2(prefix: Path, shared: Path) -> bool:
    """Point the prefix's WebView2 folder at ``shared``. False when a real install is there."""
    link = prefix / APPLICATION_DIR
    if link.is_symlink():
        if Path(os.readlink(link)) == shared:
            return True
        temporary = link.with_name(f"Application.unifideck-{os.getpid()}")
        temporary.symlink_to(shared)
        temporary.replace(link)
        return True
    if link.exists():
        logger.info("[xbox_pc.prefix] %s is a real WebView2 install; left alone", link)
        return False
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(shared)
    return True


def wanted_values(version: str) -> list[RegistryValue]:
    """The EdgeUpdate values that make the loader find ``version``."""
    clients = (*_EDGE_UPDATE, "Clients", _PRODUCT)
    state = (*_EDGE_UPDATE, "ClientState", _PRODUCT)
    return [
        (clients, "pv", version),
        (clients, "location", _WINDOWS_APPLICATION),
        (state, "EBWebView", f"{_WINDOWS_APPLICATION}\\{version}"),
    ]


def _section(registry: str, key: tuple[str, ...]) -> str | None:
    """The text of ``key``'s section in a ``system.reg``, or None."""
    # system.reg doubles each backslash in a key name and ignores case.
    header = "[" + "\\\\".join(key) + "]"
    start = registry.lower().find("\n" + header.lower())
    if start < 0:
        return None
    end = registry.find("\n[", start + 1)
    return registry[start: end if end >= 0 else None]


def stale_values(prefix: Path, version: str) -> list[RegistryValue]:
    """The wanted values ``system.reg`` lacks or holds with another value."""
    try:
        registry = (prefix / "system.reg").read_text(errors="replace")
    except OSError:
        registry = ""
    stale: list[RegistryValue] = []
    for key, name, value in wanted_values(version):
        section = _section(registry, key)
        line = f'\n"{name}"="' + value.replace("\\", "\\\\") + '"'
        if section is None or line + "\n" not in section + "\n":
            stale.append((key, name, value))
    return stale


def reg_file_text(values: list[RegistryValue]) -> str:
    """A ``regedit`` import for ``values``."""
    lines = ["Windows Registry Editor Version 5.00", ""]
    for key in dict.fromkeys(key for key, _, _ in values):
        lines.append("[HKEY_LOCAL_MACHINE\\" + "\\".join(key) + "]")
        lines += [
            f'"{name}"="' + value.replace("\\", "\\\\") + '"'
            for k, name, value in values if k == key
        ]
        lines.append("")
    return "\r\n".join(lines) + "\r\n"
