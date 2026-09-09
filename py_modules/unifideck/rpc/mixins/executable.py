"""ExecutableRPCMixin — user-settable launch executable per game.

Powers the "Change executable…" item injected into the native game context
menu. Lets the user pick a different launch target without hand-editing the
DO-NOT-EDIT ``games.map`` (which collapses its tab-delimited row to v1 and
corrupts ``work_dir`` → broken saves). Three RPCs:

* ``list_game_executables`` — the install dir's ``.exe`` candidates (noise
  filtered), the store's auto-detected default, the currently-active exe, and
  whether an override is set — everything the picker modal renders.
* ``set_game_executable`` — persist the choice (relative to the install dir).
* ``reset_game_executable`` — restore the auto-detected exe ("go back to default").

The executable choice is **fully decoupled from ``work_dir``** — that is the
whole point. ``work_dir`` is never read or written here, so cloud-save
resolution (``work_dir`` + ``game_id``) and achievements (``game_id``) are
unaffected.

Storage is per store's launch mechanism:

* **Direct-launch (gog / amazon)** — the launcher runs the games.map ``exe``
  directly via umu, so the override IS the games.map exe column (``work_dir`` /
  ``app_id`` carried over verbatim). This is also the source of truth, so a
  reinstall (which rewrites the row to the auto-detected exe via
  ``mark_installed``) naturally resets the override — no separate retention.
* **Epic** — launches through legendary, whose ``--override-exe`` reads the
  config override ``games.<id>.executable`` (see ``game_fixes.get_exe_override``).
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from unifideck.rpc import RpcError

logger = logging.getLogger(__name__)

# Stores whose launcher runs the games.map ``exe`` directly via umu — the
# override lives in (and is read back from) the games.map row for these. Other
# stores (Epic → legendary ``--override-exe`` from the config key) use the
# config-override path instead.
_DIRECT_LAUNCH_STORES = frozenset({"gog", "amazon", "gamevault"})

# How deep to scan the install dir for candidate executables. Config tools and
# shipping binaries live a level or two down (``Binaries/Win64/…``); deeper than
# this is almost always engine/redist noise.
_SCAN_DEPTH = 2

# Executable basenames (lowercased substrings) that are never a launch target —
# uninstallers, redistributable installers, crash handlers.
_NOISE_MARKERS = (
    "unins", "redist", "vcredist", "dxsetup", "dxwebsetup", "dotnet",
    "directx", "oalinst", "crashpad", "crashreport", "vc_redist",
)


def _is_noise(name: str) -> bool:
    low = name.lower()
    return any(m in low for m in _NOISE_MARKERS)


def _scan_executables(install_dir: str) -> list[str]:
    """Relative paths of candidate ``.exe`` files under ``install_dir``.

    Bounded to ``_SCAN_DEPTH`` levels and noise-filtered. Blocking (os.walk) —
    callers run it in a thread. Returns POSIX-style relative paths, sorted.
    """
    found: list[str] = []
    base = os.path.normpath(install_dir)
    for root, dirs, files in os.walk(base):
        depth = root[len(base):].count(os.sep)
        if depth >= _SCAN_DEPTH:
            dirs[:] = []  # don't descend further
        for f in files:
            if not f.lower().endswith(".exe") or _is_noise(f):
                continue
            rel = os.path.relpath(os.path.join(root, f), base)
            found.append(rel.replace(os.sep, "/"))
    return sorted(found)


class ExecutableRPCMixin:
    """User-settable launch-executable RPC surface."""

    services: Any
    config: Any
    registry: Any

    async def _install_dir(self, store: str, game_id: str) -> str:
        """Read-only install dir: games.map ``work_dir``, else ask the store.

        The games.map row is the normal answer and stays the first one — it
        records where the user actually installed a game, including custom
        SD-card locations.

        The fallback exists because that row is exactly what goes missing in
        the situation the picker is needed for. A GameVault install whose
        auto-detected executable came back empty wrote no row, and a failed
        library fetch used to prune one; either way the picker answered
        ``install_dir_unresolved``, so the one tool for fixing an unlaunchable
        game was unavailable precisely when it was needed. The store knows
        where it put the files regardless — ``get_installed_path`` is on
        ``StoreBase``, so every store can answer.
        """
        from unifideck.services.cloud_save.save_location_resolver import (
            _install_path_from_games_map,
        )
        from_map = _install_path_from_games_map(store, game_id, self.config)
        if from_map:
            return from_map
        inst = self.registry.get_store(store) if self.registry else None
        hook = getattr(inst, "get_installed_path", None)
        if not callable(hook):
            return ""
        try:
            resolved = await hook(game_id)
        except Exception:
            logger.warning(
                "[Executable] get_installed_path failed for %s:%s",
                store, game_id, exc_info=True,
            )
            return ""
        return resolved if isinstance(resolved, str) and resolved else ""

    async def _default_rel(
        self, store: str, game_id: str, install_dir: str,
    ) -> str | None:
        """The store's auto-detected exe, relative to ``install_dir``."""
        inst = self.registry.get_store(store) if self.registry else None
        finder = getattr(inst, "find_installed_exe", None)
        if not callable(finder):
            return None
        try:
            maybe: Any = finder(install_dir, game_id)
            if asyncio.iscoroutine(maybe):
                maybe = await maybe
        except Exception:
            logger.warning(
                "[Executable] default exe resolve failed for %s:%s",
                store, game_id, exc_info=True,
            )
            return None
        if not isinstance(maybe, str) or not maybe:
            return None
        return _rel_within(install_dir, maybe)

    def _config_override(self, game_id: str) -> str:
        """Epic's stored override (``games.<id>.executable``) or ``""``."""
        return str(self.config.get(f"games.{game_id}.executable") or "")

    async def _current_rel(
        self, store: str, game_id: str, install_dir: str, default_rel: str | None,
    ) -> str | None:
        """The currently-active exe (relative), per the store's ground truth."""
        if store in _DIRECT_LAUNCH_STORES:
            entry = await self.services.shortcut.get_entry_for_game_key(
                store, game_id,
            )
            if entry and entry.exe:
                return _rel_within(install_dir, entry.exe)
            return default_rel
        return self._config_override(game_id) or default_rel

    async def list_game_executables(self, store: str, game_id: str) -> Any:
        """Executable candidates + default/current/override for the picker."""
        if not store or not game_id:
            raise RpcError("invalid_args", store=store, game_id=game_id)
        install_dir = await self._install_dir(store, game_id)
        if not _is_dir(install_dir):
            raise RpcError("install_dir_unresolved", store=store, game_id=game_id)

        default_rel = await self._default_rel(store, game_id, install_dir)
        current_rel = await self._current_rel(
            store, game_id, install_dir, default_rel,
        )
        if store in _DIRECT_LAUNCH_STORES:
            override_active = bool(current_rel) and current_rel != default_rel
        else:
            override_active = bool(self._config_override(game_id))

        rels = await asyncio.to_thread(_scan_executables, install_dir)
        # Always surface the default + current even if the scan/noise-filter
        # missed them (e.g. an override pointing somewhere unusual).
        for extra in (default_rel, current_rel):
            if extra and extra not in rels:
                rels.append(extra)
        rels.sort()

        candidates = [
            {
                "rel": rel,
                "name": os.path.basename(rel),
                "is_current": rel == current_rel,
                "is_default": rel == default_rel,
            }
            for rel in rels
        ]
        return {
            "install_dir": install_dir,
            "override_active": override_active,
            "default_rel": default_rel,
            "current_rel": current_rel,
            "candidates": candidates,
        }

    async def set_game_executable(
        self, store: str, game_id: str, rel: str,
    ) -> Any:
        """Persist a launch-executable override for one game.

        Direct-launch stores rewrite the games.map exe column (work_dir
        untouched); Epic writes the config override legendary reads. Picking the
        auto-detected default is treated as a reset.
        """
        if not store or not game_id or not rel:
            raise RpcError("invalid_args", store=store, game_id=game_id)
        install_dir = await self._install_dir(store, game_id)
        if not _is_dir(install_dir):
            raise RpcError("install_dir_unresolved", store=store, game_id=game_id)

        target = _safe_resolve(install_dir, rel)
        if target is None:
            raise RpcError("invalid_executable", store=store, rel=rel)
        clean_rel = _rel_within(install_dir, target)

        # Picking the default clears the override rather than pinning a
        # redundant one (keeps "is this customised?" honest).
        if clean_rel == await self._default_rel(store, game_id, install_dir):
            return await self.reset_game_executable(store, game_id)

        if store in _DIRECT_LAUNCH_STORES:
            # Pass what a missing row would need to be built from, so a game
            # whose row never existed (or was pruned) is fixable here rather
            # than being permanently unlaunchable.
            if not await self.services.shortcut.set_executable(
                store, game_id, target,
                work_dir=install_dir,
                app_id=await self._registered_app_id(store, game_id),
            ):
                raise RpcError("no_games_map_row", store=store, game_id=game_id)
        else:
            self.config.set(f"games.{game_id}.executable", clean_rel)
        logger.info("[Executable] set %s:%s → %s", store, game_id, clean_rel)
        return {
            "success": True, "store": store, "game_id": game_id,
            "executable": clean_rel,
        }

    async def _registered_app_id(self, store: str, game_id: str) -> int | None:
        """The shortcut AppID already assigned to ``store:game_id``.

        Read from the shortcut registry, which is the durable record of the
        AppID a game's Steam shortcut was created with — not recomputed, so
        a row built here points at the shortcut the user is actually looking
        at rather than at a freshly-derived id.
        """
        from unifideck.services.shortcut.registry import (
            get_registered_appid,
            load_registry,
        )
        try:
            return get_registered_appid(load_registry(), f"{store}:{game_id}")
        except Exception:
            logger.warning(
                "[Executable] registry lookup failed for %s:%s",
                store, game_id, exc_info=True,
            )
            return None

    async def reset_game_executable(self, store: str, game_id: str) -> Any:
        """Restore the store's auto-detected exe ("go back to default")."""
        if not store or not game_id:
            raise RpcError("invalid_args", store=store, game_id=game_id)
        restored: str | None = None
        if store in _DIRECT_LAUNCH_STORES:
            install_dir = await self._install_dir(store, game_id)
            default_rel = (
                await self._default_rel(store, game_id, install_dir)
                if install_dir else None
            )
            if install_dir and default_rel:
                target = _safe_resolve(install_dir, default_rel)
                if target:
                    await self.services.shortcut.set_executable(
                        store, game_id, target,
                    )
                    restored = default_rel
        else:
            self.config.set(f"games.{game_id}.executable", "")
        logger.info(
            "[Executable] reset %s:%s (restored default %s)",
            store, game_id, restored,
        )
        return {
            "success": True, "store": store, "game_id": game_id,
            "executable": restored,
        }


def _rel_within(install_dir: str, abs_path: str) -> str:
    """``abs_path`` relative to ``install_dir`` (POSIX slashes)."""
    try:
        return os.path.relpath(
            os.path.normpath(abs_path), os.path.normpath(install_dir),
        ).replace(os.sep, "/")
    except ValueError:  # different drive (shouldn't happen on Linux)
        return os.path.basename(abs_path)


def _is_dir(path: str) -> bool:
    """Sync existence check (kept out of async bodies for ASYNC240)."""
    return bool(path) and os.path.isdir(path)


def _safe_resolve(install_dir: str, rel: str) -> str | None:
    """Resolve ``rel`` to a real FILE strictly inside ``install_dir``.

    Returns the real absolute path only when it stays inside the install dir
    AND is a regular file; ``None`` otherwise. Guards against path-traversal
    from a crafted RPC argument. Sync (os.path) so async callers stay clean.
    """
    base = os.path.realpath(install_dir)
    target = os.path.realpath(os.path.join(base, rel))
    if target == base:
        return None
    try:
        if os.path.commonpath([base, target]) != base:
            return None
    except ValueError:
        return None
    return target if os.path.isfile(target) else None
