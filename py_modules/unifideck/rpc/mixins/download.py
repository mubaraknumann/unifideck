"""Download RPC mixin for Plugin class.

Carries two related surfaces:

* per-game lifecycle — ``install_game`` / ``uninstall_game`` /
  ``update_game``;
* download-queue management — ``cancel_download`` /
  ``get_download_queue``.

Storage-location RPCs (``get_storage_locations``,
``set_default_storage_location``, ``set_custom_install_path``)
live in a sibling ``StorageRPCMixin``; this file only
resolves a chosen storage id to a filesystem path.

Two private helpers centralise the null checks:

* ``_require_store`` — store-not-found errors;
* ``_require_download`` — download-service-unavailable errors.

``_validate_pair`` validates identifiers at the RPC boundary
so the rest of the codebase can treat them as already-sanitised.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Any

from unifideck.core import marker_sweep
from unifideck.core.types import Result
from unifideck.core.types.identifiers import (
    InvalidIdentifierError,
    validate_game_id,
    validate_store_id,
)
from unifideck.rpc.errors import RpcError
from unifideck.services import update_check_cache
from unifideck.utils import mount_naming, mounts

logger = logging.getLogger(__name__)

class DownloadRPCMixin:
    """Game install/uninstall, download queue, and storage locations."""

    registry: Any
    services: Any
    sync_service: Any

    @staticmethod
    def _validate_pair(store: str, game_id: str) -> tuple[str, str]:
        """Validate the ``(store, game_id)`` pair at the RPC boundary.

        Both identifiers flow into subprocess argv, filesystem
        paths, and URL templates downstream. Rejecting malformed
        values here means the rest of the codebase can treat them
        as already-sanitised. Raises ``RpcError("invalid_identifier")``
        on failure — the frontend gets a structured error, not a
        stack trace.

        Returns the pair unchanged so the call can be inlined:
        ``store, game_id = self._validate_pair(store, game_id)``.
        """
        try:
            return validate_store_id(store), validate_game_id(game_id)
        except InvalidIdentifierError as e:
            raise RpcError("invalid_identifier", reason=str(e)) from e

    def _require_store(self, store: str) -> Any:
        """Return store adapter or raise ``store_not_found``.

        Uses :meth:`StoreRegistry.get_store` (returns ``None`` on
        miss) rather than :meth:`get` (raises ``KeyError`` on
        miss). The previous code called ``get()`` and checked for
        ``None`` — that check never fired because ``get()`` raises
        instead of returning ``None``, so a missing store
        propagated a cryptic ``KeyError`` to the frontend instead
        of the documented ``store_not_found`` RPC error.
        """
        adapter = self.registry.get_store(store)
        if adapter is None:
            raise RpcError("store_not_found", store=store)
        return adapter

    def _require_download(self) -> Any:
        """Return download service or raise ``service_unavailable``."""
        svc = getattr(self.services, "download", None)
        if svc is None:
            raise RpcError("service_unavailable", service="download")
        return svc

    async def install_game(self, store: str, game_id: str, options: Any = None, **kw: Any) -> Any:
        """Queue a game install via the download service.

        Accepts an optional ``options`` dict as a positional arg
        (the frontend passes ``{storage, language, title}``
        positionally through the RPC bridge). Resolves the
        ``storage`` type (``"internal"|"sdcard"|"custom"``) to a
        filesystem path, extracts the game title for the queue UI,
        and enqueues the install via ``DownloadService.add()``.

        Returns immediately with ``{success: true/false, error: ...}``
        — the actual install runs asynchronously through the
        download worker.
        """
        store, game_id = self._validate_pair(store, game_id)
        opts: dict[str, Any] = dict(kw)
        if isinstance(options, dict):
            opts.update(options)

        storage_type = opts.pop("storage", None)
        base_path = _resolve_storage_path(storage_type, getattr(self, "config", None))
        if not base_path:
            base_path = str(Path.home() / "Games")

        title: str = opts.pop("title", "") or opts.pop("game_title", "")
        # GOG multi-language picker selection (verbatim — it's one of
        # the game's own language codes, matched exactly downstream).
        # Other stores don't send this.
        language = opts.pop("language", None)
        download_dir: str = opts.pop("download_dir", "") or ""

        logger.info(
            "[download] install_game store=%s game_id=%s storage=%s "
            "base_path=%s title=%s language=%s",
            store, game_id, storage_type, base_path, title, language,
        )

        required_bytes = await self._resolve_required_bytes(store, game_id)

        download_svc = self._require_download()
        result = await download_svc.add(
            store=store,
            game_id=game_id,
            install_path=base_path,
            title=title,
            is_update=False,
            language=language,
            required_bytes=required_bytes,
            download_dir=download_dir,
        )
        return {"success": result.success, "error": result.error}

    async def _resolve_required_bytes(
        self, store: str, game_id: str,
    ) -> int | None:
        """Known download size in bytes, for the free-space preflight.

        Read from the persistent :class:`SizeCache` populated when
        App-Details showed the "Space Required" row — warm on the normal
        install-from-details flow. A miss returns ``None`` (the preflight
        degrades to the static floor); we deliberately do NOT shell out
        to the store CLI here, so a cold cache never delays or hangs the
        install. The wrapper stores are manual vendor-client installs with
        no download of ours to measure, so they're exempt — asking for a
        size that cannot exist just makes the preflight fall back to its
        static floor by the slowest possible route.
        """
        from unifideck.launcher.wrapper_stores import uses_manual_download_phase

        if uses_manual_download_phase(store):
            return None
        from unifideck.services.size_cache import get_size_cache
        cache = get_size_cache(_size_cache_file(getattr(self, "config", None)))
        return await cache.get(store, game_id)

    async def update_game(self, app_id: int, **kw: Any) -> Any:
        """Queue an update for an already-installed game.

        Triggered by the Play→Update button, which only appears
        when ``get_available_updates`` listed this game as stale.
        Resolves the Steam ``app_id`` back to its ``(store,
        game_id, install_path)`` via the sync layer, then enqueues
        with ``is_update=True`` so the worker dispatches to
        ``store.update_game`` and the UI labels it an update.
        """
        info = self.sync_service.get_game_info(app_id) if self.sync_service else None
        if not info:
            return {"success": False, "error": "game_not_found"}

        store, game_id = self._validate_pair(
            info.get("store", ""), info.get("store_game_id", ""),
        )
        title = info.get("title", "") or ""
        # The connectors re-resolve their own install path on update;
        # pass the known one when available, else the internal base
        # (same resolution install_game uses for "internal").
        install_path = (
            info.get("install_path")
            or _resolve_storage_path("internal", getattr(self, "config", None))
            or ""
        )

        logger.info("[download] update_game app_id=%s store=%s game_id=%s install_path=%s",
                     app_id, store, game_id, install_path)

        download_svc = self._require_download()
        result = await download_svc.add(
            store=store,
            game_id=game_id,
            install_path=install_path,
            title=title,
            is_update=True,
        )
        if result.success:
            # The cached scan still lists this game as updatable. Drop it so
            # the button stops offering an update that is already queued,
            # and so the post-install re-check sees the new version instead
            # of a stale "yes" for the rest of the TTL.
            update_check_cache.invalidate(store)
        # Return the ``Result`` dataclass, not a bare ``{success, error}``
        # dict — for the same reason ``uninstall_game`` does. A dict that
        # already carries ``success`` is folded INTO the envelope, leaving
        # ``data`` null, and ``useRPC`` hands callers only ``data``. The
        # dataclass lands in ``data`` as ``{success, error, ...}`` so the
        # frontend's mutation hooks see it.
        return Result(success=result.success, error=result.error)

    async def uninstall_game(self, app_id: int, delete_prefix: bool = False) -> Any:
        """Uninstall a game via the responsible store connector.

        The frontend only has the Steam ``app_id`` on hand (the
        trash button / Uninstall pill live on the app details page),
        so — like :meth:`update_game` — we resolve it back to its
        ``(store, game_id)`` via the sync layer and dispatch to the
        store. ``delete_prefix`` is forwarded so connectors that run
        games under Proton can also remove the Wine prefix.

        Earlier this method took ``(store, game_id)`` directly, but
        the frontend sends a single ``app_id``; the numeric value
        landed in the ``store`` slot and ``game_id`` was missing, so
        the call raised before anything was uninstalled.
        """
        info = self.sync_service.get_game_info(app_id) if self.sync_service else None
        if not info:
            return Result(success=False, error="game_not_found")

        store, game_id = self._validate_pair(
            info.get("store", ""), info.get("store_game_id", ""),
        )

        logger.info("[download] uninstall_game app_id=%s store=%s game_id=%s delete_prefix=%s",
                     app_id, store, game_id, delete_prefix)

        # Return the ``Result`` dataclass (not a plain ``{success, error}``
        # dict). The RPC envelope folds a dict that already has a top-level
        # ``success`` key into the envelope and leaves ``data=None`` — the
        # frontend then receives ``null`` and ``result?.success`` is always
        # falsy, so ``useGameActions`` never invalidates the game-info cache
        # and the Play section stays "installed" until a manual reload.
        # A dataclass return lands in ``data`` as ``{success, error, ...}``.
        result = await self._require_store(store).uninstall_game(
            game_id, delete_prefix=delete_prefix,
        )
        # Guarantee the install dir is gone even if the store no-op'd. GOG
        # resolves install dirs by scanning its default download_dir, so a
        # game installed elsewhere (SD/custom) can't be found and its
        # uninstall returns success without deleting anything; nile likewise
        # leaves our manifest marker (a stub dir) behind. The marker proves
        # the folder is ours, so this only ever removes a dir we created.
        await asyncio.to_thread(marker_sweep.sweep_game, store, game_id)
        return result

    async def get_available_updates(self) -> Any:
        """Return ``{store: [game_id, ...]}`` for every store, from cache only.

        The single update-state route. Every Update affordance reads it
        through ``UpdateStore`` / ``useGameUpdate`` — the App-Details
        Play section, the QAM Downloads tab's Installed rows, and the
        pre-launch warning — so they can never disagree.

        **This never blocks on a store round-trip.** It reads whatever
        :class:`~unifideck.services.update_sweep.UpdateSweepService` last
        cached and returns immediately; a store not yet swept is simply
        absent from the mapping rather than holding the whole list up
        behind a scan, and gets a background refresh scheduled instead.
        The button then arrives via ``GAME_UPDATE_AVAILABLE``.

        Answering per-game and inline is what this replaced: a
        ``check_game_update(store, game_id)`` route ran the scan on page
        open, which made the Update button take 5-10 s to appear for
        Epic (legendary logs in and refreshes its asset manifest first)
        — long enough for the user to have pressed Play already. It was
        left declared with no caller and deleted in the audit §1.2 pass.
        Rendering many games at once through it would also have meant
        one RPC per installed game.
        """
        out: dict[str, list[str]] = {}
        for store_id in self.registry.store_ids():
            cached = update_check_cache.peek(store_id)
            if cached is None:
                self._request_update_scan(store_id)
                continue
            out[store_id] = cached
        return out

    def _request_update_scan(self, store: str) -> None:
        """Ask the sweep service to refresh ``store`` in the background.

        A no-op when the service failed to boot — update state then falls
        back to whatever the cache holds, which is the pre-sweep
        behaviour rather than an error.
        """
        sweep = getattr(self, "_update_sweep_service", None)
        if sweep is None:
            logger.debug("[download] no update sweep service; %s stays cold", store)
            return
        sweep.request_refresh(store)

    async def get_gog_game_languages(self, game_id: str) -> Any:
        """Return the install languages available for a GOG game.

        Drives the language-select modal in the GOG install flow
        (``useInstallFlow``). Wraps ``GOGStore.get_available_languages``;
        falls back to ``["en-US"]`` so the frontend can still install
        if the lookup fails. ``game_id`` is the GOG product id
        (the game's ``store_game_id``), not the unifideck app id.

        Lost in the mixin refactor — the frontend route existed with
        no handler, so the call errored (swallowed by a ``.catch``)
        and multi-language GOG titles never prompted.
        """
        try:
            store = self.registry.get_store("gog")
            if store is None:
                return {"success": False, "error": "store_not_found", "languages": ["en-US"]}
            languages = await store.get_available_languages(game_id)
            return {"success": True, "languages": languages}
        except Exception as e:
            logger.exception("[download] get_gog_game_languages(%s) failed", game_id)
            return {"success": False, "error": str(e), "languages": ["en-US"]}

    async def get_epic_game_languages(self, game_id: str) -> Any:
        """Return the install languages available for an Epic game.

        Drives the same language-select modal as GOG. Non-empty only for
        legendary's Selective Downloads titles (Fallout 3 GOTY, Hogwarts
        Legacy, Cyberpunk 2077, …), where the optional language packs are
        separate downloads; every other Epic title returns an empty list
        and the frontend skips the picker.

        ``languages`` are legendary's own SDL option keys and ``labels``
        maps each to legendary's display name, which is richer than the
        bare locale codes GOG reports. Fails open to an empty list — a
        lookup failure must never block an install, since the backend
        falls back to installing the base game only.
        """
        try:
            store = self.registry.get_store("epic")
            if store is None:
                return {"success": False, "error": "store_not_found", "languages": []}
            options = await store.get_install_language_options(game_id)
            return {
                "success": True,
                "languages": list(options.keys()),
                "labels": options,
            }
        except Exception as e:
            logger.exception("[download] get_epic_game_languages(%s) failed", game_id)
            return {"success": False, "error": str(e), "languages": []}

    async def cancel_download(self, store: str, game_id: str) -> Any:
        """Cancel an in-progress download.

        :meth:`DownloadService.cancel` takes ``(store, game_id)`` —
        the queue is keyed by ``"<store>:<game_id>"``. Earlier
        this mixin passed a single ``download_id`` which the
        service interpreted as ``store`` and silently failed to
        find any matching entry.
        """
        store, game_id = self._validate_pair(store, game_id)
        return await self._require_download().cancel(store, game_id)

    async def get_download_queue(self) -> Any:
        """Return the current download queue (sync method, no await)."""
        return self._require_download().get_queue()

    async def clear_download_history(self, item_id: str | None = None) -> Any:
        """Dismiss finished-download history rows (all, or one by id).

        Backs the "clear" action on failed rows in the Downloads tab. History
        only — no installed files, shortcuts or queue entries are touched.
        """
        removed = await self._require_download().clear_history(item_id)
        return {"success": True, "removed": removed}

# ─── Storage type → path resolution ───────────────────────────
#
# External-mount enumeration is delegated to ``utils/mounts.py``
# (shared with ``rpc/mixins/storage.py`` and ``utils/paths.py``) —
# see that module for why a FUSE-mounted external drive (NTFS via
# ntfs-3g, some exFAT setups) needs a demoted subprocess to be
# reachable from this backend's root process at all.

def _size_cache_file(config: Any) -> str:
    """Path to the persistent download-size cache (in the data dir).

    Mirrors ``SyncRPCMixin._size_cache_path`` — kept as a free function
    here rather than reaching across to the sibling mixin's method
    (mypy checks each mixin's ``self`` independently). TODO: both should
    share one helper in ``services/size_cache.py``.
    """
    data_dir = "~/.local/share/unifideck"
    if config is not None:
        with contextlib.suppress(Exception):
            data_dir = (
                config.get("paths.data_dir", None)
                or config.get("data_dir", data_dir)
                or data_dir
            )
    return str(Path(data_dir).expanduser() / "game_sizes.json")

def _resolve_storage_path(storage_type: str | None, config: Any) -> str | None:
    """Convert a storage type string to a filesystem path.

    ``"internal"``      → ``~/Games``,
    ``"sdcard"``         → legacy alias, first eligible external mount,
    ``"ext:<name>"``     → that SPECIFIC external mount only,
    ``"custom"``         → ``download.custom_path`` from config,
    ``None``             → ``None`` (store connector uses its default).
    """
    if not storage_type:
        return None

    if storage_type == "internal":
        path = str(Path.home() / "Games")
        logger.debug("[download] resolved internal → %s", path)
        return path
    if storage_type == "custom":
        return _custom_path(config)
    if storage_type == "sdcard" or storage_type.startswith("ext:"):
        return _external_games_path(storage_type)
    logger.warning("[download] unknown storage type: %s", storage_type)
    return None

def _external_games_path(storage_type: str) -> str | None:
    """Resolve an external-mount storage id to its ``Games/`` dir.

    ``storage_type == "sdcard"`` is the legacy alias (configs/callers
    predating the unique-id fix) — resolved to the first eligible
    external mount, preserving pre-fix behavior. Any other value is a
    unique ``ext:<uuid>`` id from ``mounts.mount_id`` (or the older
    ``ext:<name>`` form, still accepted) — resolved to that SPECIFIC
    mount only; if it's no longer eligible (unplugged, no longer
    writable) this returns ``None`` rather than silently substituting a
    different device (the caller falls back to internal storage — see
    ``install_game``).
    """
    home_dev = mounts.stat_dev(str(Path.home()))
    # Dedupe first so the "first external" (legacy alias) and the unique-id
    # assignment match the storage picker's device list exactly.
    externals = mounts.dedupe_by_device(
        mounts.scan_mounts(home_dev, require_writable=True),
    )
    if storage_type == "sdcard":
        match = externals[0] if externals else None
        if match is not None:
            logger.debug(
                "[download] legacy 'sdcard' resolved to first external mount: %s",
                match.mount_point,
            )
    else:
        match = next(
            (m for loc_id, m in mounts.assign_unique_ids(externals)
             if loc_id == storage_type),
            None,
        )
        if match is None:
            # An id saved before ids moved to the filesystem UUID, e.g.
            # a queued install or a frontend that hasn't re-fetched the
            # picker yet. Resolve it the old way rather than dropping
            # the user's chosen drive and falling back to internal.
            match = next(
                (m for m in externals
                 if mount_naming.legacy_mount_id(m.mount_point) == storage_type),
                None,
            )
            if match is not None:
                logger.info(
                    "[download] legacy name-based id %s resolved to %s",
                    storage_type, match.mount_point,
                )
    if match is None:
        logger.warning("[download] no eligible external mount for storage=%s", storage_type)
        return None
    games_path = mounts.ensure_games_subdir(
        match.mount_point, match.effective_uid, match.effective_gid,
    )
    logger.debug("[download] resolved %s → %s (%s)", storage_type, games_path, match.fstype)
    return games_path

def _custom_path(config: Any) -> str | None:
    """Read ``download.custom_path`` from config; None if unset/invalid."""
    if config is None:
        return None
    try:
        path = config.get("download.custom_path", None)
    except Exception as e:
        logger.warning("[download] custom_path lookup failed: %s", e)
        return None
    if isinstance(path, str) and path:
        logger.debug("[download] resolved custom → %s", path)
        return path
    return None
