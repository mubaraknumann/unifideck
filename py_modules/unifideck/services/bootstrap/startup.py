"""services/bootstrap/startup.py — Async start hooks + post-boot self-heal.

Calls ``start()`` on services that need async initialisation,
each wrapped in its own try/except so one broken service can't
block the others. Then runs a post-boot self-heal that restores
the +x bit on launcher entry points.
"""
from __future__ import annotations

import logging
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .container import ServiceContainer

logger = logging.getLogger(__name__)

# Services with async init hooks. First three open DBs or spawn
# poll loops; ``security`` runs device-fingerprint verification;
# ``launch_history`` doesn't truly need async but is listed here
# for uniformity; ``proton`` background-installs the latest GE-Proton
# (non-blocking — it spawns a detached task and returns immediately).
# Other services don't implement ``start`` and are skipped by the
# getattr probe below.
_ASYNC_START_SERVICES: tuple[str, ...] = (
    # First, so its series covers the rest of boot — the window in which
    # caches fill and every background loop is armed.
    "memory_sampler",
    "download",
    "account",
    "playtime",
    "playtime_sync",
    "security",
    "launch_history",
    "proton",
)


async def start_async_services(container: ServiceContainer) -> None:
    """Await ``start`` on each entry in ``_ASYNC_START_SERVICES``.

    Missing service (None slot) → skip. Missing ``start`` method
    → skip. Failed start → log WARNING + continue (broken DB open
    or fingerprint check leaves that service disabled but plugin
    still boots). Always runs the executable-bit self-heal at
    the end.
    """
    for service_name in _ASYNC_START_SERVICES:
        instance = getattr(container, service_name, None)
        if instance is None:
            continue

        start_method = getattr(instance, "start", None)
        if not callable(start_method):
            continue

        try:
            await start_method()
            logger.info("[Startup] started %s", service_name)
        except Exception as e:
            logger.warning(
                "[Startup] failed to start %s: %s",
                service_name, e,
            )

    if container.shortcut is not None:
        try:
            await _self_heal_auth_shortcuts(container.shortcut)
        except Exception as e:
            logger.warning("[Startup] failed to run self-heal auth shortcuts: %s", e)

    _self_heal_executable_bits()


def _self_heal_executable_bits() -> None:
    """Restore +x on launcher entry points after Decky Loader unzip.

    Decky Loader's unzip doesn't always preserve the
    ``external_attr`` field, so ``dispatcher.py`` can land
    without +x → execve fails with "Permission denied" even
    though the shebang is correct. Runs BEFORE the shortcut
    migration so when shortcuts are rewritten to point at the
    dispatcher it's already executable. Best-effort — failure
    logged but plugin continues to boot (recoverable via manual
    chmod +x).
    """
    try:
        # Get path to the bin directory relative to this file
        # This file is at py_modules/unifideck/services/bootstrap/startup.py
        base_dir = str(Path(str(Path(str(Path(str(Path(str(Path(__file__).parent)).parent)).parent)).parent)).parent)
        bin_dir = str(Path(base_dir) / "bin")

        if not Path(bin_dir).is_dir():
            return

        for filename in [entry.name for entry in Path(bin_dir).iterdir()]:
            path = str(Path(bin_dir) / filename)
            if Path(path).is_file():
                st = Path(path).stat()
                # Add executable bit for owner/group/others if not present
                if not (st.st_mode & stat.S_IXUSR):
                    # Adds the +x bit for owner/group/others on shipped
                    # tools. The mask preserves all existing bits and
                    # only adds executability — required for the
                    # bundled helpers to run after unzip strips +x.
                    Path(path).chmod(st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                    logger.info("[Startup] restored +x on %s", path)
    except Exception as e:
        logger.warning("[Startup] failed to self-heal executable bits: %s", e)


async def _self_heal_auth_shortcuts(shortcut_svc: Any) -> None:
    """Delete leftover OAuth auth shortcuts from shortcuts.vdf.

    GOG / Epic / Amazon / Microsoft no longer keep a persistent
    auth shortcut — the frontend creates an ephemeral one through
    ``SteamClient.Apps.AddShortcut`` for each connect, used once,
    then removed by the 15-second cleanup. Persistent entries from
    older plugin versions still exist on disk; this sweep removes
    them on startup so every store routes through the temp-shortcut
    path uniformly. Ubisoft's ``upc-auth`` row is excluded — its
    auth flow legitimately reuses a persistent shortcut.
    """
    try:
        await shortcut_svc._load_shortcuts()
        if not isinstance(shortcut_svc._shortcuts, dict) or "shortcuts" not in shortcut_svc._shortcuts:
            return

        shortcuts_dict = shortcut_svc._shortcuts["shortcuts"]
        if not isinstance(shortcuts_dict, dict):
            return

        from unifideck.services.shortcut.write_guard import is_ours

        launcher = getattr(shortcut_svc, "_launcher_path", "") or ""
        stale_auth_tags = {"auth-gog", "auth-epic", "auth-amazon", "auth-microsoft"}
        keys_to_delete: list[str] = []

        for key, entry in shortcuts_dict.items():
            # Every auth shortcut we ever wrote targets our launcher, so
            # the Exe gate loses nothing here — while a tag-value match
            # alone would delete any shortcut of the user's that happens
            # to carry one of these four strings, on every single boot.
            if not isinstance(entry, dict) or not is_ours(entry, launcher):
                continue
            tags = entry.get("tags", {})
            if not isinstance(tags, dict):
                continue
            tag_values = list(tags.values())
            matched_tag = next(
                (t for t in tag_values if t in stale_auth_tags), None,
            )
            if matched_tag is None:
                continue
            logger.info(
                "[Startup] Removing leftover auth shortcut %s (key=%s, AppName=%r)",
                matched_tag, key, entry.get("AppName"),
            )
            keys_to_delete.append(key)

        for key in keys_to_delete:
            del shortcuts_dict[key]

        if keys_to_delete:
            await shortcut_svc._save_all()
            logger.info(
                "[Startup] Removed %d leftover auth shortcut(s) from shortcuts.vdf",
                len(keys_to_delete),
            )
    except Exception:
        logger.exception("[Startup] Failed to sweep leftover auth shortcuts")

