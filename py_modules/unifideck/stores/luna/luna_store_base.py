"""Shared-state declarations for the Luna store method mixins.

The former single ``luna_store.py`` was one class; its methods are now split
across focused sibling mixins (``luna_store_library_sync``,
``luna_store_recovery``, ``luna_store_install``) that
:class:`~unifideck.stores.luna.luna_store.LunaStore` combines. Each mixin
reaches ``self._*`` attributes and sibling-mixin methods that only exist on the
composed class. :class:`_LunaStoreState` declares those attributes and
cross-mixin method signatures as annotations only — no runtime behaviour — so
each mixin type-checks in isolation under strict mypy, exactly as the
``rpc/mixins`` packages declare their shared ``registry``/``services`` handles.
The real values are assigned by ``LunaStore.__init__`` and the real methods are
provided by the sibling mixins.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from unifideck.event_bus.event_bus import EventBus
    from unifideck.stores.luna.luna_browser import LunaBrowser
    from unifideck.stores.luna.luna_catalog import LunaCatalog
    from unifideck.stores.luna.luna_config import LunaConfig
    from unifideck.stores.luna.luna_session_monitor import LunaSessionMonitor

    #: ``(ws-less) -> awaitable`` action retried by the recovery mixin.
    _RetryAction = Callable[[], Awaitable[Any]]
    #: A zero-arg resume coroutine factory the install mixin may run.
    _Resume = Callable[[], Awaitable[Any]] | None


class _LunaStoreState:
    """Shared attributes + cross-mixin methods the Luna store mixins rely on.

    Annotations only: the attributes are set in ``LunaStore.__init__`` and the
    methods are implemented by sibling mixins. Declaring them here lets each
    mixin reference them under strict mypy without importing its siblings.
    The method handles are declared as callable attributes (not ``def``
    stubs) so this declaration adds no cyclomatic complexity.
    """

    # ── Collaborators assigned in LunaStore.__init__ ──────────────────
    _bus: EventBus
    _luna_config: LunaConfig
    _edge: Any
    _browser: LunaBrowser
    _session: LunaSessionMonitor
    _catalog: LunaCatalog | None
    _last_stable_status: str

    # ── Methods implemented by sibling mixins (declared as callables) ──
    _with_retry: Callable[[str, _RetryAction], Awaitable[Any]]
    _log_operation_failure: Callable[[str, int, Exception], None]
    _emit_recovery_in_progress: Callable[[str, int], Awaitable[None]]
    _restore_stable_state: Callable[[str], Awaitable[None]]
    _previous_luna_entries: Callable[[], list[dict[str, object]]]
    _run_browser_install: Callable[[], Awaitable[str | None]]
    _resume: Callable[[_Resume], Awaitable[Any]]
