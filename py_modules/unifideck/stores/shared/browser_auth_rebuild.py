"""The post-injection auth rebuild, shared by the browser-auth stores.

py_modules/unifideck/stores/shared/browser_auth_rebuild.py

Auto-discovery builds every store before the service container exists, so a
store that authenticates through the CDP browser flow is constructed with
``browser_monitor=None`` and cannot build its auth flow yet.
``services/bootstrap/store_injector`` fills ``_browser_monitor`` afterwards
and then looks up ``_rebuild_auth_after_injection`` **by name** on the
instance, calling it if it exists.

Epic, GOG, Amazon and Microsoft each wrote that method out. Normalised, the
four were statement-for-statement identical apart from a store-name literal,
a log prefix, the auth-flow constructor, and one extra call in GOG's copy
(audit §3.4). This is the single body; the four differences are the two hooks
below.

**Ubisoft implements the same injector hook with a genuinely different body**
and is deliberately not a consumer of this mixin. It has no browser monitor —
it signs in through the vendor client in its own prefix — and uses the hook
to propagate the just-injected shortcut service into its auth facade. The
audit counted it as a fifth copy of this method; it is not one.

**Applied to the four stores, never to** :class:`StoreBase`. The injector's
only test for "does this store need an auth rebuild" is whether the attribute
exists, so putting it on the base class would start calling it for Battle.net
and Ubisoft too.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from unifideck.auth.orchestrator import AuthOrchestrator

if TYPE_CHECKING:
    from unifideck.core.types import StoreInfo
    from unifideck.event_bus.event_bus import EventBus

logger = logging.getLogger(__name__)


class BrowserAuthRebuildMixin:
    """Rebuilds a CDP-browser auth flow once the monitor has been injected.

    Consumers must set ``self._auth = None`` before this can run, and list
    the mixin **first** in their bases so it wins the MRO over
    :class:`StoreBase`.
    """

    # Provided by StoreBase / the concrete store. Declared for mypy only —
    # the mixin never assigns them.
    _bus: EventBus
    _auth: Any
    store_info: StoreInfo

    def _rebuild_auth_after_injection(self) -> None:
        """(Re-)build this store's auth flow once a browser monitor is set.

        Idempotent: the injector may call it more than once, and a flow that
        is already built is left alone.
        """
        if self._auth is not None:
            return
        monitor = getattr(self, "_browser_monitor", None)
        if monitor is None:
            logger.debug(
                "[%s] no browser_monitor; auth disabled",
                type(self).__name__,
            )
            return
        orchestrator = AuthOrchestrator(
            bus=self._bus,
            browser_monitor=monitor,
            # Was a hardcoded literal in all four copies. ``StoreInfo.name``
            # is the same string and is already machine-checked against the
            # store's directory name (validate_architecture check 3).
            store_name=self.store_info.name,
        )
        self._auth = self._build_auth_flow(orchestrator)
        self._after_auth_flow_built()
        logger.info("[%s] auth flow wired", type(self).__name__)

    def _build_auth_flow(self, orchestrator: AuthOrchestrator) -> Any:
        """Construct this store's auth flow around *orchestrator*.

        The one genuinely per-store part: each flow takes different
        collaborators (a CLI path, a token manager, success markers, …).
        """
        raise NotImplementedError

    def _after_auth_flow_built(self) -> None:
        """Optional follow-up once ``_auth`` exists. Default: nothing.

        Only GOG needs it — its gogdl-driven submodules capture the token
        manager, which the auth flow may have refreshed, so they are rebuilt
        here. Without this the store keeps an empty ``_gogdl_bin``,
        ``is_available`` refuses, and every install dies at spawn.
        """
