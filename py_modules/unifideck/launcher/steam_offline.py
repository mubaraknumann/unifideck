"""launcher/steam_offline.py — whether Steam is in offline mode.

Steam records "Go Offline" as ``"WantsOfflineMode" "1"`` in
``config/loginusers.vdf``, on the account block of the user who is signed in
(``"MostRecent" "1"``). Stores read it to start a game without asking their
servers first. Only the signed-in account counts: another account in the
file may have been left in offline mode.

Stdlib only (runs in the launcher). Any read error means online.
"""
from __future__ import annotations

import re
from pathlib import Path

_BLOCK = re.compile(r'"\d+"\s*\{([^{}]*)\}')


def _flag(block: str, key: str) -> bool:
    match = re.search(rf'"{key}"\s*"([^"]*)"', block, re.IGNORECASE)
    return match is not None and match.group(1) == "1"


def steam_offline_mode(steam_root: Path | None = None) -> bool:
    """True when the signed-in Steam account is in offline mode."""
    if steam_root is None:
        from unifideck.utils.vdf_compat import find_steam_root
        steam_root = find_steam_root() or Path("~/.steam/steam").expanduser()
    try:
        text = (steam_root / "config" / "loginusers.vdf").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    blocks = _BLOCK.findall(text)
    recent = [b for b in blocks if _flag(b, "MostRecent")]
    return any(_flag(b, "WantsOfflineMode") for b in (recent or blocks))
