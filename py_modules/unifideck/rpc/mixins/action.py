"""ActionRPCMixin — mixin form of the ``unifideck://`` URI dispatcher.

Reaches for ``self.registry`` / ``self.services`` /
``self.sync_service`` directly on the host ``Plugin`` class, which
composes this mixin as a base. That is the only composition style in
the tree: there is no injected-dependency handler class to contrast
it with, and no ``rpc/handlers/`` package.

This mixin is **thin** by design: the heavy lifting lives in
``unifideck.actions.dispatch.dispatch_backend_action`` so the
mixin can be swapped without re-implementing the verb routing.
"""

from __future__ import annotations

from typing import Any


class ActionRPCMixin:
    """``unifideck://`` URI dispatcher exposed as an RPC method."""

    registry: Any
    services: Any

    async def dispatch_unifideck_action(self, uri: str) -> Any:
        """Forward a ``unifideck://`` URI to the dispatch helper.

        Delegates entirely to ``dispatch_backend_action``,
        which handles URI parsing, scope validation, and
        per-verb routing. The mixin's role is just to
        surface the function on the plugin class as an
        RPC method and inject the three collaborators the
        dispatcher needs.

        ``sync_service`` is fetched via ``getattr`` with a
        ``None`` default so the mixin still works when wired
        into a plugin variant without a sync service (rare,
        but supported).

        Args:
            uri: the full ``unifideck://...`` URI from the
                frontend.

        Returns:
            Dispatch result dict (shape determined by the
            target verb handler).
        """
        from unifideck.actions.dispatch import dispatch_backend_action

        return await dispatch_backend_action(
            uri=uri,
            registry=self.registry,
            cloudsave=self.services.cloudsave,
            sync_service=getattr(self, "sync_service", None),
        )
