"""launcher/xbox_pc/environment.py — the env of an Xbox PC game and its helpers.

The game runs as a child of ``xodus-cli run``, which passes its own env on.
Built in this order, so a later step wins over an earlier one:

1. :func:`clean_env`: the launcher's env without the launch markers a
   parent can leak (``STRIPPED_ENV``), the loader and Python variables of
   the frozen host (``cli_env.SCRUBBED_VARS``) and our own ``UNIFIDECK_*``
   tokens.
2. Wine and vkd3d log levels. ``WINEDEBUG=fixme-all`` silences the fixme
   class; Nexus's ``-fixme`` names a channel and silences nothing (I-020).
3. The game language, when the game declares it.
4. ``XODUS_CONTENT_ROOTS`` for installed DLC folders.
5. The Xodus env (``argv.build_env``): ``XODUS_CREDENTIAL_DIR`` and quiet logs.
6. The Steam window identity, **only** when the shortcut's app id is known.
   With no app id the inherited ``SteamGameId`` from Steam is kept: an
   explicit ``0`` would make the window class ``steam_app_0`` and leave the
   game behind the Gaming Mode loading screen.
7. The ``xodus-service`` socket and bridge, when running.
8. The user's own ``KEY=value`` launch options, last (launcher invariant 7).

Stdlib only, Python 3.10 safe.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path

from unifideck.core.binaries.cli_env import SCRUBBED_VARS

from .argv import build_env

#: Launch-scoped markers a parent may leak into the game (Nexus
#: ``anti-cheat.ts`` and ``game-log.ts``). ``LD_PRELOAD`` is in
#: ``SCRUBBED_VARS``.
STRIPPED_ENV = (
    "EOS_USE_ANTICHEATCLIENTNULL", "PROTON_EAC_LAUNCHER_PROCESS",
    "DESKTOP_STARTUP_ID", "XDG_ACTIVATION_TOKEN", "XDG_DESKTOP_FILE_NAME",
    "GIO_LAUNCHED_DESKTOP_FILE", "GIO_LAUNCHED_DESKTOP_FILE_PID",
    "BAMF_DESKTOP_FILE_HINT", "CHROME_DESKTOP", "APPDIR", "APPIMAGE",
)
_OWN_PREFIX = "UNIFIDECK_"

_LOG_LEVELS = {
    "WINEDEBUG": "fixme-all",
    "VKD3D_DEBUG": "warn",
    "VKD3D_SHADER_DEBUG": "warn",
}
_LOCALE_VARS = (
    "LANG", "LC_ALL", "LC_MESSAGES", "LC_CTYPE", "LC_TIME", "LC_COLLATE",
    "LC_MONETARY", "LC_NUMERIC", "LC_PAPER",
)
_LOCALE_RE = re.compile(r"([A-Za-z]{2,3})-([A-Za-z]{2})")

#: The identity a host-side helper (prefix creation) runs with: explicit
#: zeros, so the shortcut's identity never reaches it.
ZERO_STEAM_IDS = {
    "SteamAppId": "0",
    "SteamGameId": "0",
    "STEAM_COMPAT_APP_ID": "0",
    "UMU_STEAM_GAME_ID": "0",
}


def clean_env(source: Mapping[str, str]) -> dict[str, str]:
    """``source`` without launch markers, loader variables and our tokens."""
    return {
        k: v for k, v in source.items()
        if k not in STRIPPED_ENV and k not in SCRUBBED_VARS and not k.startswith(_OWN_PREFIX)
    }


def pick_locale(requested: str | None, available: Sequence[str]) -> str | None:
    """The declared language closest to ``requested``, or None.

    An exact match first, then the same base language (``fr-CA`` picks
    ``fr-FR``). None when nothing was requested or nothing matches; the
    caller tells the user in the second case.
    """
    if not requested:
        return None
    lowered = requested.lower()
    for candidate in available:
        if candidate.lower() == lowered:
            return candidate
    base = lowered.split("-")[0]
    for candidate in available:
        if candidate.lower().split("-")[0] == base:
            return candidate
    return None


def locale_env(env: dict[str, str], locale: str | None) -> dict[str, str]:
    """Set the package and Unix locale variables for ``locale``. Returns ``env``."""
    if not locale:
        return env
    env.update({"XODUS_PACKAGE_LOCALE": locale, "WINEUSERLOCALE": locale})
    match = _LOCALE_RE.fullmatch(locale)
    if match:
        lang, region = match[1].lower(), match[2].upper()
        for key in _LOCALE_VARS:
            env[key] = f"{lang}_{region}.UTF-8"
        env["LANGUAGE"] = f"{lang}_{region}:{lang}"
    return env


def content_roots_value(roots: Sequence[str | Path]) -> str:
    """``XODUS_CONTENT_ROOTS``: Windows paths on drive ``Z:``, ``;``-separated."""
    return ";".join(
        "Z:" + str(Path(r).resolve()).replace("/", "\\") for r in roots
    )


def build_game_env(
    base: Mapping[str, str],
    *,
    credential_dir: Path,
    locale: str | None = None,
    content_roots: Sequence[str | Path] = (),
    window_env: Mapping[str, str] | None = None,
    service_env: Mapping[str, str] | None = None,
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The env for ``xodus-cli run`` and the game, in the order above."""
    env = clean_env(base)
    for key, value in _LOG_LEVELS.items():
        env.setdefault(key, value)
    locale_env(env, locale)
    if content_roots:
        env["XODUS_CONTENT_ROOTS"] = content_roots_value(content_roots)
    env = build_env(env, credential_dir)
    env.update(window_env or {})
    env.update(service_env or {})
    env.update(overrides or {})
    return env
