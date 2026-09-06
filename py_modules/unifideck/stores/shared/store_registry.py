import asyncio
import logging
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from unifideck.core.store_capabilities import capability_flags
from unifideck.core.types import Events, Result, StoreError
from unifideck.launcher.wrapper_stores import is_wrapper_store

if TYPE_CHECKING:
    from unifideck.config import ConfigManager
    from unifideck.core.cache_manager import CacheManager
    from unifideck.event_bus import EventBus

    from .store_base import StoreBase
logger = logging.getLogger(__name__)
class StoreRegistry:
    """Store registry."""

    def __init__(self, bus: "EventBus") -> None:
        """Initialize the instance."""
        self._stores: dict[str, StoreBase] = {}
        self._bus = bus
        # Hold strong references to STORE_REGISTERED emit tasks until
        # they finish so the GC doesn't collect them mid-flight.
        self._background_tasks: set[asyncio.Task[Any]] = set()
    def register(
        self, store_id: str, store: "StoreBase",
    ) -> None:
        """Register."""
        self._stores[store_id] = store
        logger.info("[StoreRegistry] Registered: %s", store_id)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug(
                "[StoreRegistry] no running event loop; "
                "STORE_REGISTERED suppressed for %s",
                store_id,
            )
            return
        payload = {
            "store_id": store_id,
            "store_info": asdict(store.store_info),
        }
        task = loop.create_task(
            self._bus.emit(Events.STORE_REGISTERED, **payload),
            name=f"emit_store_registered_{store_id}",
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def auto_discover(
        self,
        stores_dir: str,
        bus: "EventBus",
        cache: "CacheManager",
        plugin_dir: str = "",
        config: "ConfigManager | None" = None,
    ) -> int:
        """Auto discover stores under ``stores_dir`` and register them."""
        import importlib

        from .store_base import StoreBase as _StoreBase
        real_stores = self._validate_stores_dir(
            stores_dir, plugin_dir,
        )
        if real_stores is None:
            return 0
        package_name = "unifideck.stores"
        try:
            importlib.import_module(package_name)
        except ImportError as e:
            logger.warning(
                "[StoreRegistry] Cannot resolve stores "
                "package: %s", e,
            )
            return 0
        registered = 0
        for module_suffix, full_path in self._iter_store_files(
            real_stores,
        ):
            store_cls = self._load_store_class(
                package_name, module_suffix, full_path,
                _StoreBase,
            )
            if store_cls is None:
                continue
            try:
                store = store_cls(
                    bus, cache, plugin_dir, config=config,
                )
                store_id = store.store_info.name
                self.register(store_id, store)
                logger.info(
                    "[StoreRegistry] registered %s (%s) "
                    "from %s",
                    store_id, store_cls.__name__, full_path,
                )
                registered += 1
            except Exception:
                logger.exception("[StoreRegistry] Failed to instantiate %s from %s", store_cls.__name__, full_path)
        logger.info(
            "[StoreRegistry] Auto-discovery: %d stores "
            "from %s",
            registered, real_stores,
        )
        return registered

    @staticmethod
    def _validate_stores_dir(
        stores_dir: str, plugin_dir: str,
    ) -> str | None:
        """Validate stores dir."""
        try:
            real_stores = str(Path(stores_dir).resolve())
        except OSError:
            logger.exception("[StoreRegistry] Cannot resolve stores dir %r", stores_dir)
            return None
        if not Path(real_stores).is_dir():
            logger.warning(
                "[StoreRegistry] stores dir not found: %s",
                real_stores,
            )
            return None
        if plugin_dir:
            real_plugin = str(Path(plugin_dir).resolve())
            confined = (
                real_stores == real_plugin
                or real_stores.startswith(real_plugin + "/")
            )
            if not confined:
                logger.error(
                    "[StoreRegistry] SECURITY: stores dir "
                    "%s is NOT under plugin dir %s — "
                    "refusing to auto-discover.",
                    real_stores, real_plugin,
                )
                return None
        else:
            logger.warning(
                "[StoreRegistry] auto_discover called "
                "without plugin_dir — path confinement "
                "disabled. This is only acceptable in unit "
                "tests; production must always pass "
                "plugin_dir.",
            )
        return real_stores
    @staticmethod
    def _iter_store_files(real_stores: str) -> Iterator[tuple[str, str]]:
        """Iter store files.

        Yields (module_suffix, full_path) tuples where module_suffix
        is the dotted module path relative to ``unifideck.stores``.

        Three layouts are accepted:

        * Flat: a top-level ``<name>_store.py`` file → yields
          ("<name>_store", path).
        * Subpackage with bare ``store.py``: ``<name>/store.py`` →
          yields ("<name>.store", path).
        * Subpackage with prefixed ``<name>_store.py``:
          ``<name>/<name>_store.py`` → yields
          ("<name>.<name>_store", path).

        Symlinks and ``_``-prefixed entries are skipped at every
        level for the same reasons as the flat path: confinement.

        Refactor history (2026-05-14): was at CC=24 (the worst
        in the codebase) — the outer loop's body inlined the
        three layout branches with nested ``if entry.is_dir() /
        for candidate_name / if not is_file / if is_symlink /
        yield / break / if not yielded`` which pushed nesting
        to five levels deep. Pulled both the flat-file branch
        and the subpackage walk into private static helpers so
        the outer loop reads as ``for entry: skip-or-delegate``.
        """
        real_stores_p = Path(real_stores)
        for entry in sorted(real_stores_p.iterdir()):
            name = entry.name
            if name.startswith("_"):
                continue
            if entry.is_symlink():
                logger.warning(
                    "[StoreRegistry] SECURITY: skipping "
                    "symlink %s", str(entry),
                )
                continue
            if entry.is_file() and name.endswith("_store.py"):
                yield from StoreRegistry._yield_for_flat_file(entry)
                continue
            if entry.is_dir():
                yield from StoreRegistry._yield_for_subpackage(entry)

    @staticmethod
    def _yield_for_flat_file(entry: Path) -> Iterator[tuple[str, str]]:
        """Yield the ``(suffix, path)`` tuple for a flat-file store.

        Layout: ``stores/<name>_store.py`` (no subpackage). The
        module suffix is the filename without ``.py``. Single-
        yield generator — kept as a generator for symmetry with
        ``_yield_for_subpackage`` and so the caller stays a
        clean ``yield from``.
        """
        # Strip the ``.py`` extension to obtain the suffix.
        yield entry.name[:-3], str(entry)

    @staticmethod
    def _yield_for_subpackage(entry: Path) -> Iterator[tuple[str, str]]:
        """Yield the ``(suffix, path)`` for a subpackage-layout store.

        Layout: ``stores/<name>/``. Two candidate filenames
        are tried in priority order :

            * ``<name>_store.py`` (prefixed — the canonical
              layout post-Decky-template-v2).
            * ``store.py`` (bare — legacy layout retained for
              third-party stores still on v1).

        First match wins and the search stops. Symlinked
        candidates are skipped with a SECURITY warning for the
        same reason flat-file symlinks are skipped — confinement.
        """
        name = entry.name
        for candidate_name in (f"{name}_store.py", "store.py"):
            candidate = entry / candidate_name
            if not candidate.is_file():
                continue
            if candidate.is_symlink():
                logger.warning(
                    "[StoreRegistry] SECURITY: skipping "
                    "symlinked %s in %s",
                    candidate_name, str(entry),
                )
                continue
            yield f"{name}.{candidate_name[:-3]}", str(candidate)
            return

    @staticmethod
    def _load_store_class(
        package_name: str,
        module_suffix: str,
        full_path: str,
        store_base_cls: type[Any],
    ) -> type[Any] | None:
        """Load store class."""
        import importlib
        module_name = f"{package_name}.{module_suffix}"
        logger.info(
            "[StoreRegistry] loading %s from %s",
            module_name, full_path,
        )
        try:
            mod = importlib.import_module(module_name)
        except Exception as e:
            logger.debug(
                "[StoreRegistry] Skip %s: %s", module_suffix, e,
            )
            return None
        for attr_name in dir(mod):
            attr = getattr(mod, attr_name)
            if (
                isinstance(attr, type)
                and issubclass(attr, store_base_cls)
                and attr is not store_base_cls
                and hasattr(attr, "store_info")
            ):
                return attr
        return None
    def get(self, store_id: str) -> "StoreBase":
        """Get."""
        if store_id not in self._stores:
            raise KeyError(
                f"Store '{store_id}' not registered. "
                f"Available: {list(self._stores.keys())}",
            )
        return self._stores[store_id]
    def get_store(self, store_id: str) -> "StoreBase | None":
        """Get store."""
        return self._stores.get(store_id)
    def all(self) -> list["StoreBase"]:
        """All."""
        return list(self._stores.values())
    def available(self) -> list["StoreBase"]:
        """Available."""
        return [
            s for s in self._stores.values()
            if getattr(s, "_cached_available", False)
        ]
    def store_ids(self) -> list[str]:
        """Store ids."""
        return list(self._stores.keys())
    def has(self, store_id: str) -> bool:
        """Check whether s."""
        return store_id in self._stores
    def get_store_infos(self) -> list[dict[str, Any]]:
        """Static descriptor plus the derived keys, per store.

        Everything derived is injected here rather than declared on
        ``StoreInfo``, for the reason audit §3.1 established when it removed
        ``uses_wine``: a per-store literal is a second copy that drifts, and
        the gate checking it becomes the only thing keeping it alive.

        * ``available`` — live probe state.
        * ``client_runs_in_prefix`` — owned by ``WRAPPER_STORES``.
        * the capability flags — owned by ``core.store_capabilities``, which
          is also what the RPC mixins and services branch on. This is what
          lets the frontend stop hand-maintaining its own per-store lists;
          the audit found sixteen of them with one machine-checked pair
          between them.

        ``supports_cloud_saves`` used to be a declared field and is now
        derived. Only Battle.net ever set it, as ``False``, so GOG and Epic
        both advertised no cloud saves — wiring a UI to the old field would
        have hidden the feature on the only two stores that have it.
        """
        infos = []
        for store in self._stores.values():
            name = store.store_info.name
            info = asdict(store.store_info)
            info["available"] = getattr(
                store, "_cached_available", False,
            )
            info["client_runs_in_prefix"] = is_wrapper_store(name)
            info.update(capability_flags(name))
            infos.append(info)
        return infos

    async def auth_action(
        self, store_id: str, action: str, **kwargs: Any,
    ) -> Result:
        """Auth action."""
        try:
            store = self.get(store_id)
        except KeyError as e:
            return Result(success=False, error=str(e))
        try:
            if action == "start":
                return await store.start_auth(**kwargs)
            if action == "complete":
                return await store.complete_auth(**kwargs)
            if action == "logout":
                result = await store.logout()
                if result.success:
                    await self._bus.emit(
                        Events.STORE_LOGOUT,
                        store=store_id,
                    )
                return result
            if action == "status":
                is_avail = await store.is_available()
                store._cached_available = is_avail
                return Result(success=is_avail)
            return Result(
                success=False,
                error=(
                    f"Unknown auth action: '{action}'. "
                    f"Valid: start, complete, logout, status"
                ),
            )
        except StoreError as e:
            logger.exception("[StoreRegistry] %s.%s failed", store_id, action)
            await self._bus.emit(
                Events.STORE_AUTH_FAILED,
                store=store_id, error=str(e),
            )
            return Result(success=False, error=str(e))
        except Exception as e:
            logger.exception("[StoreRegistry] Unexpected error in %s.", store_id)
            return Result(
                success=False, error=f"Unexpected: {e}",
            )
    async def check_all_status(self) -> list[dict[str, Any]]:
        """Check all status."""
        results: list[dict[str, Any]] = []
        for store in self._stores.values():
            entry: dict[str, Any] = {
                "store_id": store.store_info.name,
                "name": store.store_info.display_name,
                "available": False,
                "error": None,
            }
            try:
                entry["available"] = await store.is_available()
                store._cached_available = entry["available"]
            except Exception as e:
                entry["error"] = str(e)
                logger.warning(
                    "[StoreRegistry] %s availability check "
                    "failed: %s", store.store_info.name, e,
                )
            results.append(entry)
        return results
    async def logout_all(self) -> dict[str, Any]:
        """Logout all."""
        out: dict[str, Any] = {}
        for store_id in self._stores:
            result = await self.auth_action(store_id, "logout")
            out[store_id] = {
                "success": result.success,
                "error": result.error,
            }
        return out
